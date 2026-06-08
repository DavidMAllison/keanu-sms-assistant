"""
Keanu - iMessage AI assistant
Polls chat.db for new messages, routes to Claude via tool use, replies via AppleScript.
"""

import base64
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import anthropic
import yaml
from dotenv import load_dotenv

from agent import get_reply
from agents import menu_workflow
from groceryagent_bridge import call_receipt_parser
from tools import send_imessage, send_imessage_group, drain_outbox
from agents.menu_agent import save_recipe_idea as _save_recipe_idea_fn, IDEAS_DIR as _RECIPE_IDEAS_DIR

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────

CONFIG_FILE = Path(__file__).parent / "config/settings.yaml"
STATE_FILE = Path(__file__).parent / ".keanu_state.json"
OUTBOX_FILE = Path(__file__).parent / ".outbox.json"
_outbox_lock = threading.Lock()
MENU_PENDING_FILE = Path(__file__).parent / "menu_feedback_pending.json"
MENU_RESPONSE_FILE = Path("/Users/Shared/cooking/menu_feedback_response.json")
MENU_SESSION_FILE = Path("/Users/Shared/cooking/menu_session.json")
CHAT_DB = Path.home() / "Library/Messages/chat.db"
POLL_INTERVAL = 3

# ── Config / state ────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f)

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            if isinstance(data, dict):
                return data
            log.error(f"State file corrupt (got {type(data).__name__}), resetting to defaults")
        except Exception as e:
            log.error(f"Could not read state file: {e}")
    return {"last_rowid": 0}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state))

# ── Outbox ────────────────────────────────────────────────────────────────────
# drain_outbox, send_imessage, send_imessage_group imported from tools



# ── Holiday messages ───────────────────────────────────────────────────────────

_HOLIDAYS = {
    "2026-01-01": "New Year's Day",
    "2026-01-19": "Martin Luther King Jr. Day",
    "2026-02-16": "Presidents' Day",
    "2026-05-25": "Memorial Day",
    "2026-07-04": "Independence Day",
    "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving",
    "2026-12-25": "Christmas Day",
}

_HOLIDAY_SEND_AFTER = 8   # 8 AM
_HOLIDAY_SEND_BEFORE = 10  # 10 AM


def maybe_send_holiday_message(state: dict, config: dict):
    today = date.today()
    now = datetime.now()

    holiday_name = _HOLIDAYS.get(today.isoformat())
    if not holiday_name:
        return
    if not (_HOLIDAY_SEND_AFTER <= now.hour < _HOLIDAY_SEND_BEFORE):
        return

    already_sent = state.get("holiday_messages_sent", {})
    if already_sent.get(today.isoformat()):
        return

    kids = config["security"].get("kids", [])
    handle_to_person = config["security"].get("handle_to_person", {})
    person_to_handle = {v: k for k, v in handle_to_person.items()}

    for person, handle in person_to_handle.items():
        if handle not in kids:
            continue
        try:
            response = _20q_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=120,
                messages=[{
                    "role": "user",
                    "content": (
                        f"You are Keanu, a friendly British koala texting {person} on {holiday_name}. "
                        "Write a short, fun, quirky holiday greeting. Two sentences max. "
                        "Be warm and a little silly — use a funny or unexpected emoji. "
                        "No markdown. No generic greetings like 'Happy holidays!'"
                    ),
                }],
            )
            msg = response.content[0].text.strip()
        except Exception as e:
            log.error(f"Holiday message generation error for {person}: {e}")
            continue

        send_imessage(handle, msg)
        log.info(f"Holiday message ({holiday_name}) sent to {person} ({handle})")

    if "holiday_messages_sent" not in state:
        state["holiday_messages_sent"] = {}
    state["holiday_messages_sent"][today.isoformat()] = True
    save_state(state)


# ── Trash reminder ─────────────────────────────────────────────────────────────

_TRASH_SEND_HOUR = 17  # 5 PM


def maybe_send_trash_reminder(state: dict, config: dict):
    today = date.today()
    now = datetime.now()

    if today.weekday() != 1:  # Tuesday only
        return
    if not (_TRASH_SEND_HOUR <= now.hour < _TRASH_SEND_HOUR + 1):
        return
    if state.get("last_trash_reminder_date") == today.isoformat():
        return

    handle_to_person = config["security"].get("handle_to_person", {})
    person_to_handle = {v: k for k, v in handle_to_person.items()}
    eleanor_handle = person_to_handle.get("Eleanor")
    if not eleanor_handle:
        log.warning("Trash reminder: Eleanor handle not found in config")
        return

    msg = (
        "Hey Eleanor! Just a reminder — Tuesday is trash night. "
        "Bring the bins to the curb before bed if you want your allowance this week!"
    )
    send_imessage(eleanor_handle, msg)
    log.info("Trash reminder sent to Eleanor")

    state["last_trash_reminder_date"] = today.isoformat()
    save_state(state)


# ── Tapback filter ─────────────────────────────────────────────────────────────

_TAPBACK_PREFIXES = (
    "Liked ", "Loved ", "Disliked ", "Laughed at ",
    "Emphasized ", "Questioned ", "Removed a heart from ",
    "Removed a like from ", "Removed a dislike from ",
    "Removed a laugh from ", "Removed an exclamation from ",
    "Removed a question mark from ",
)

def is_tapback(text: str) -> bool:
    return any(text.startswith(p) for p in _TAPBACK_PREFIXES)

def _prepare_image_file(att: dict) -> Optional[tuple]:
    """Convert attachment to a (file_path, mime_type) tuple ready for API use.

    Returns None on failure. Caller is responsible for cleaning up any temp file
    created during HEIC conversion (check if returned path differs from original).
    """
    raw_path = att["filename"]
    mime_type = att["mime_type"]
    file_path = Path(raw_path).expanduser()

    if not file_path.exists():
        log.warning(f"Attachment file not found: {file_path}")
        return None

    # iPhones send HEIC by default — convert to JPEG with sips (built-in macOS)
    if mime_type in ("image/heic", "image/heif"):
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            tmp_path = tmp.name
            tmp.close()
            subprocess.run(
                ["sips", "-s", "format", "jpeg", str(file_path), "--out", tmp_path],
                check=True, capture_output=True,
            )
            file_path = Path(tmp_path)
            mime_type = "image/jpeg"
            log.info(f"Converted HEIC to JPEG: {tmp_path}")
        except Exception as e:
            log.error(f"HEIC conversion failed: {e}")
            return None

    supported = ("image/jpeg", "image/png", "image/gif", "image/webp")
    if mime_type not in supported:
        log.warning(f"Unsupported image type: {mime_type}")
        return None

    return file_path, mime_type


def _run_receipt_bridge(image_path: str, handle: str):
    """Call the grocery agent bridge and send the reply to handle via outbox.

    Cleans up the temp file after the call regardless of outcome.
    """
    try:
        result = call_receipt_parser(image_path)
        reply = result.get("reply")
        if not reply:
            error = result.get("error", "unknown error")
            log.error(f"Receipt bridge returned no reply for {handle}: {error}")
            reply = "Sorry, something went wrong on my end processing that receipt — let David know."
        send_imessage(handle, reply)
        log.info(f"Receipt processed for {handle}: {reply[:80]}")
    finally:
        try:
            Path(image_path).unlink(missing_ok=True)
            log.info(f"Cleaned up temp receipt file: {image_path}")
        except Exception as e:
            log.warning(f"Could not delete temp file {image_path}: {e}")


def _is_grocery_receipt_caption(text: str) -> bool:
    """Return True if the message caption suggests a grocery receipt."""
    lowered = text.lower()
    return any(kw in lowered for kw in _GROCERY_CAPTION_KEYWORDS)


def _is_recipe_idea_caption(text: str) -> bool:
    """Return True if the message caption indicates the user wants to save a recipe idea."""
    lowered = text.lower()
    return any(kw in lowered for kw in _RECIPE_IDEA_CAPTION_KEYWORDS)


def _run_recipe_idea_from_image(image_path: str, mime_type: str, handle: str) -> str:
    """Copy the raw image file into the recipe ideas inbox for MenuBuilder to process.

    Does not extract the recipe name — MenuBuilder reads images directly.
    Cleans up the temp file in all cases via finally.
    """
    _MIME_TO_EXT = {
        "image/jpeg": ".jpg",
        "image/png":  ".png",
        "image/gif":  ".gif",
        "image/webp": ".webp",
    }
    try:
        _RECIPE_IDEAS_DIR.mkdir(exist_ok=True)
        ext = _MIME_TO_EXT.get(mime_type, ".jpg")
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        dest = _RECIPE_IDEAS_DIR / f"{timestamp}_recipe{ext}"
        shutil.copy2(image_path, dest)
        log.info(f"Saved recipe idea image from {handle} to {dest}")
        return "Saved the recipe photo to ideas — just ask me to review your ideas when you're ready!"
    except Exception as e:
        log.error(f"Recipe idea image save error for {handle}: {e}", exc_info=True)
        return "Something went wrong on my end — try sending it again?"
    finally:
        try:
            Path(image_path).unlink(missing_ok=True)
            log.info(f"Cleaned up temp recipe idea image: {image_path}")
        except Exception as e:
            log.warning(f"Could not delete temp recipe idea file {image_path}: {e}")


def _vision_is_receipt(image_path: str, mime_type: str) -> bool:
    """Ask Claude Haiku whether the image is a grocery receipt. Returns True/False."""
    try:
        image_data = base64.standard_b64encode(Path(image_path).read_bytes()).decode("utf-8")
        response = _20q_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": mime_type, "data": image_data},
                    },
                    {
                        "type": "text",
                        "text": "Is this image a grocery receipt? Reply with only 'yes' or 'no'.",
                    },
                ],
            }],
        )
        answer = response.content[0].text.strip().lower()
        log.info(f"Vision receipt check answered: {answer!r}")
        return answer.startswith("yes")
    except Exception as e:
        log.error(f"Vision receipt check error: {e}")
        return False


def route_image_message(attachments: list, text: str, handle: str,
                        idea_submitters: Optional[list] = None):
    """Route an inbound image through recipe-idea, grocery, or general vision handling.

    - Path 0 (recipe idea caption + authorised handle): extract name via Vision, save idea.
    - Path A (grocery caption keyword): parse receipt immediately, no confirmation.
    - Path B (no keyword):
        - If Haiku thinks it's a receipt: ask user to confirm, set pending state.
        - Otherwise: fall through to general vision handler and return a reply string.

    Returns a reply string only for the general-vision fallback (Path B, not-a-receipt).
    For Path 0, A, and receipt-confirmation paths, replies are sent directly and None is returned.
    idea_submitters: if provided, only handles in this list may trigger Path 0.
    """
    if not attachments:
        return "Sorry, I couldn't read that image — try resending?"

    # Use the first image attachment only
    prepared = _prepare_image_file(attachments[0])
    if prepared is None:
        return "Sorry, I couldn't read that image — try resending?"

    file_path, mime_type = prepared
    image_path = str(file_path)

    # Path 0 — caption indicates a recipe idea save request
    if _is_recipe_idea_caption(text):
        if idea_submitters is not None and handle not in idea_submitters:
            log.info(f"Path 0: recipe idea caption from non-submitter {handle}, falling through")
        else:
            log.info(f"Path 0: recipe idea caption from {handle}, extracting recipe name via Vision")
            reply = _run_recipe_idea_from_image(image_path, mime_type, handle)
            send_imessage(handle, reply)
            return None

    # Path A — caption explicitly mentions a grocery store or receipt
    if _is_grocery_receipt_caption(text):
        log.info(f"Path A: grocery caption from {handle}, routing to receipt bridge")
        _run_receipt_bridge(image_path, handle)
        return None  # reply already sent inside _run_receipt_bridge

    # Path B — ask Haiku to check
    if _vision_is_receipt(image_path, mime_type):
        log.info(f"Path B: vision says receipt from {handle}, requesting confirmation")
        # Write receipt to a stable temp path so it survives until confirmation
        ts = int(time.time())
        stable_path = f"/tmp/receipt_{ts}.jpg"
        try:
            shutil.copy2(image_path, stable_path)
            # Clean up original converted temp if it was a new temp file
            if image_path != stable_path:
                Path(image_path).unlink(missing_ok=True)
        except Exception as e:
            log.error(f"Could not copy receipt to stable path: {e}")
            stable_path = image_path  # fall back to original path

        _pending_receipt[handle] = {"image_path": stable_path}
        send_imessage(handle, "Looks like a grocery receipt — want me to process it? (yes/no)")
        return None  # confirmation message already sent

    # Not a receipt — general vision handler
    log.info(f"Not a receipt from {handle}, routing to general vision handler")
    image_contents = []
    try:
        image_data = base64.standard_b64encode(Path(image_path).read_bytes()).decode("utf-8")
        image_contents.append({
            "type": "image",
            "source": {"type": "base64", "media_type": mime_type, "data": image_data},
        })

        prompt = text.strip() if text.strip() else "What's in this image?"
        image_contents.append({"type": "text", "text": prompt})

        response = _20q_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            system=(
                "You are Keanu, a friendly British koala family assistant. "
                "Describe or interpret images helpfully and conversationally. "
                "Keep replies concise — this is SMS."
            ),
            messages=[{"role": "user", "content": image_contents}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        log.error(f"Vision API / read error for {image_path}: {e}")
        return "Sorry, I had trouble processing that image — try again?"
    finally:
        # Clean up converted HEIC temp file (no-op if it's the original attachment path)
        Path(image_path).unlink(missing_ok=True)


def handle_image_message(attachments: list, text: str, handle: str) -> str:
    """Legacy wrapper — kept for callers that expect a return value.

    New code should call route_image_message() directly.
    """
    result = route_image_message(attachments, text, handle)
    return result if result is not None else ""


def is_menu_start(text: str) -> bool:
    return any(p in text.lower() for p in (
        "start menu", "menu time", "do the menu", "weekly menu", "start the menu",
    ))

# ── iMessage send ──────────────────────────────────────────────────────────────
# send_imessage and send_imessage_group imported from tools

# ── chat.db polling ────────────────────────────────────────────────────────────

def get_max_rowid() -> int:
    conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT MAX(rowid) FROM message").fetchone()
        return row[0] or 0
    finally:
        conn.close()

def poll_new_messages(last_rowid: int, min_date: Optional[float] = None) -> list[dict]:
    conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    try:
        APPLE_EPOCH_OFFSET = 978307200
        date_filter = ""
        params: list = [last_rowid]
        if min_date is not None:
            date_filter = "AND m.date >= ?"
            params.append(int((min_date - APPLE_EPOCH_OFFSET) * 1e9))
        cursor = conn.execute(f"""
            SELECT m.rowid, m.text, h.id AS sender_handle,
                   a.filename AS att_filename,
                   a.mime_type AS att_mime_type
            FROM message m
            JOIN handle h ON m.handle_id = h.rowid
            JOIN chat_message_join cmj ON cmj.message_id = m.rowid
            JOIN chat c ON c.rowid = cmj.chat_id
            LEFT JOIN message_attachment_join maj ON maj.message_id = m.rowid
            LEFT JOIN attachment a ON a.rowid = maj.attachment_id
            WHERE m.is_from_me = 0
              AND c.chat_identifier NOT LIKE 'chat%'
              AND m.rowid > ?
              AND (
                (m.text IS NOT NULL AND m.text != '')
                OR (a.mime_type LIKE 'image/%')
              )
              {date_filter}
            ORDER BY m.rowid ASC
        """, params)
        # Deduplicate by rowid — one message can have multiple attachments
        seen: dict = {}
        for row in cursor.fetchall():
            rowid, text, handle, att_filename, att_mime_type = row
            if rowid not in seen:
                seen[rowid] = {
                    "rowid": rowid,
                    "text": text or "",
                    "handle": handle,
                    "attachments": [],
                }
            if att_filename and att_mime_type and att_mime_type.startswith("image/"):
                seen[rowid]["attachments"].append({
                    "filename": att_filename,
                    "mime_type": att_mime_type,
                })
        return sorted(seen.values(), key=lambda m: m["rowid"])
    finally:
        conn.close()

# ── Rate limiting ──────────────────────────────────────────────────────────────

_rate_limit_store: dict[str, list[float]] = defaultdict(list)

def is_rate_limited(handle: str, limit: int) -> bool:
    now = time.time()
    _rate_limit_store[handle] = [t for t in _rate_limit_store[handle] if now - t < 3600]
    if len(_rate_limit_store[handle]) >= limit:
        return True
    _rate_limit_store[handle].append(now)
    return False

# ── Receipt pending state ──────────────────────────────────────────────────────

# Keyed by handle. Value: {"image_path": str} — held until user confirms or denies.
_pending_receipt: dict = {}

_GROCERY_CAPTION_KEYWORDS = (
    "receipt", "grocery", "kroger", "costco", "trader joe", "aldi", "whole foods",
)

_RECIPE_IDEA_CAPTION_KEYWORDS = (
    "save this as an idea", "save as an idea", "recipe idea", "add this as an idea",
    "add as an idea", "save this recipe", "we should make this", "add to ideas",
    "save as idea", "save as a recipe",
)

# ── 20 Questions ───────────────────────────────────────────────────────────────

_20q_client = anthropic.Anthropic()
_active_20q: dict[str, dict] = {}
_active_20q_reverse: dict[str, dict] = {}
_20Q_START_PATTERNS = ("20 questions", "twenty questions", "play 20", "play twenty")
_20Q_REVERSE_PATTERNS = ("you guess", "your turn to guess", "you try to guess", "i'll think", "i will think", "you ask", "reverse 20", "me think of", "i think of something", "my turn to think")
_20Q_QUIT_PHRASES = ("give up", "i give up", "what is it", "don't know", "no idea", "quit", "stop the game", "end game")


def is_20q_start(text: str) -> bool:
    lowered = text.lower()
    return any(p in lowered for p in _20Q_START_PATTERNS)


def is_20q_reverse_start(text: str) -> bool:
    lowered = text.lower()
    return any(p in lowered for p in _20Q_REVERSE_PATTERNS)


def start_20q_game(handle: str, is_kid: bool) -> str:
    category = (
        "something a child would know (animal, food, simple object, cartoon character)"
        if is_kid else
        "something interesting (animal, famous person, place, food, everyday object)"
    )
    prompt = (
        f"You're Keanu, starting a 20 Questions game. Pick {category}.\n\n"
        "Reply in exactly this format (two lines, nothing else):\n"
        "SECRET: [your chosen thing]\n"
        "MESSAGE: [your opening message to the player]"
    )
    try:
        response = _20q_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        lines = response.content[0].text.strip().splitlines()
        secret = next((l[7:].strip() for l in lines if l.startswith("SECRET:")), None)
        opening = next((l[8:].strip() for l in lines if l.startswith("MESSAGE:")), None)
        if not secret or not opening:
            raise ValueError("bad format")
        _active_20q[handle] = {"secret": secret, "questions_asked": 0, "history": [], "is_kid": is_kid}
        log.info(f"20Q started for {handle}, secret={secret!r}")
        return opening
    except Exception as e:
        log.error(f"20Q start error: {e}")
        return "Couldn't start the game, sorry — try again!"


def handle_20q_turn(handle: str, text: str) -> tuple[str, bool]:
    game = _active_20q.get(handle)
    if not game:
        return "No game in progress!", True

    secret = game["secret"]
    n = game["questions_asked"]
    lowered = text.lower()

    if any(p in lowered for p in _20Q_QUIT_PHRASES):
        del _active_20q[handle]
        return f"It was {secret}! Better luck next time!", True

    plural = "s" if n + 1 != 1 else ""
    system = (
        f"You are Keanu, playing 20 Questions. You are secretly thinking of: {secret}\n\n"
        "Rules:\n"
        "- Answer yes/no questions about your secret truthfully (one short sentence)\n"
        f"- If the player guesses correctly, reply: \"Yes! You got it in {n + 1} question{plural}! It was {secret}!\" then on a new line write exactly: GAME_OVER\n"
        f"- After every non-winning answer, append \" ({n + 1}/20)\" to your reply\n"
        f"- If this is question 20 and they haven't guessed, reveal it: \"You've used all 20! It was {secret}! Well played though!\" then GAME_OVER\n"
        "- Keep answers to one sentence. Be playful and British."
    )
    messages = game["history"] + [{"role": "user", "content": text}]

    try:
        response = _20q_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=system,
            messages=messages,
        )
        reply = response.content[0].text.strip()
    except Exception as e:
        log.error(f"20Q turn error: {e}")
        return "Something went wrong — try again!", False

    game_over = "GAME_OVER" in reply
    reply = reply.replace("GAME_OVER", "").strip()

    if game_over:
        del _active_20q[handle]
    else:
        game["questions_asked"] += 1
        game["history"].append({"role": "user", "content": text})
        game["history"].append({"role": "assistant", "content": reply})
        if game["questions_asked"] >= 20:
            del _active_20q[handle]

    return reply, game_over

def start_20q_reverse_game(handle: str, is_kid: bool) -> str:
    prompt = (
        "You are Keanu, starting a reverse 20 Questions game where the human thinks of something "
        "and you ask yes/no questions to figure it out.\n\n"
        "Ask your very first yes/no question to kick things off. "
        "Be playful and Australian. One sentence, end with (1/20)."
    )
    try:
        response = _20q_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        opening = response.content[0].text.strip()
        _active_20q_reverse[handle] = {"questions_asked": 1, "history": [], "is_kid": is_kid}
        log.info(f"20Q reverse started for {handle}")
        return opening
    except Exception as e:
        log.error(f"20Q reverse start error: {e}")
        return "Couldn't start the game, sorry — try again!"


def handle_20q_reverse_turn(handle: str, text: str) -> tuple[str, bool]:
    game = _active_20q_reverse.get(handle)
    if not game:
        return "No game in progress!", True

    n = game["questions_asked"]
    lowered = text.lower()

    if any(p in lowered for p in _20Q_QUIT_PHRASES):
        del _active_20q_reverse[handle]
        return "No worries, maybe next time!", True

    history_text = "\n".join(
        f"{'Keanu' if m['role'] == 'assistant' else 'Player'}: {m['content']}"
        for m in game["history"]
    ) or "None yet."

    system = (
        "You are Keanu, playing reverse 20 Questions. The human is thinking of something secret "
        "and you're asking yes/no questions to figure it out.\n\n"
        f"Questions asked so far: {n}/20\n"
        f"Q&A so far:\n{history_text}\n\n"
        "Rules:\n"
        "- React briefly to the answer, then ask ONE new yes/no question — OR make a guess if confident\n"
        "- To guess say \"Is it [X]?\" and wait for confirmation\n"
        f"- If the player says yes to your guess, celebrate and write GAME_OVER on a new line\n"
        "- If your guess is wrong, keep asking questions\n"
        f"- If this is question 20, make your best guess and write GAME_OVER regardless\n"
        f"- Append \"({n + 1}/20)\" after your question or guess\n"
        "- Playful Australian tone. Two sentences max."
    )

    try:
        response = _20q_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=system,
            messages=game["history"] + [{"role": "user", "content": text}],
        )
        reply = response.content[0].text.strip()
    except Exception as e:
        log.error(f"20Q reverse turn error: {e}")
        return "Something went wrong — try again!", False

    game_over = "GAME_OVER" in reply
    reply = reply.replace("GAME_OVER", "").strip()

    if game_over:
        del _active_20q_reverse[handle]
    else:
        game["questions_asked"] += 1
        game["history"].append({"role": "user", "content": text})
        game["history"].append({"role": "assistant", "content": reply})
        if game["questions_asked"] > 20:
            del _active_20q_reverse[handle]

    return reply, game_over


# ── Main loop ──────────────────────────────────────────────────────────────────

STARTUP_GRACE_SECONDS = 120


class _SendHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/send":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body)
                handle = payload["handle"]
                text = payload["text"]
            except (json.JSONDecodeError, KeyError):
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "missing handle or text"}')
                return
            with _outbox_lock:
                outbox = json.loads(OUTBOX_FILE.read_text()) if OUTBOX_FILE.exists() else []
                outbox.append({"handle": handle, "text": text})
                OUTBOX_FILE.write_text(json.dumps(outbox))
            log.info(f"API /send: queued message to {handle}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        elif self.path == "/start_menu_workflow":
            # Triggered by launchd (davidallison user) — runs as allisonbot so file writes succeed.
            try:
                cfg = load_config()
                admin_handle = cfg["security"].get("menu_admin")
                if not admin_handle:
                    raise ValueError("no menu_admin in config")
                reply = menu_workflow.handle_start(cfg)
                with _outbox_lock:
                    outbox = json.loads(OUTBOX_FILE.read_text()) if OUTBOX_FILE.exists() else []
                    outbox.append({"handle": admin_handle, "text": reply})
                    OUTBOX_FILE.write_text(json.dumps(outbox))
                log.info(f"API /start_menu_workflow: queued opening message to {admin_handle}")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')
            except Exception as e:
                log.error(f"API /start_menu_workflow error: {e}")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):  # suppress default HTTP access log
        log.debug("API: " + format % args)


def start_api_server(port: int):
    server = HTTPServer(("127.0.0.1", port), _SendHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info(f"API server listening on http://127.0.0.1:{port}")


def main():
    log.info("Keanu starting up...")
    cfg = load_config()
    start_api_server(cfg.get("api_port", 5050))
    state = load_state()

    if state["last_rowid"] == 0:
        state["last_rowid"] = get_max_rowid()
        save_state(state)
        log.info(f"First run: starting from ROWID {state['last_rowid']}")

    log.info(f"Polling {CHAT_DB} every {POLL_INTERVAL}s (last ROWID: {state['last_rowid']})")

    startup_cutoff = time.time() - STARTUP_GRACE_SECONDS
    first_poll = True

    while True:
        try:
            if not isinstance(state, dict):
                log.error(f"Server state corrupted in memory (type={type(state).__name__}), reloading from disk")
                state = load_state()
            drain_outbox()
            config = load_config()
            maybe_send_trash_reminder(state, config)
            maybe_send_holiday_message(state, config)
            min_date = startup_cutoff if first_poll else None
            messages = poll_new_messages(state["last_rowid"], min_date)
            first_poll = False

            for msg in messages:
                state["last_rowid"] = msg["rowid"]
                save_state(state)

                handle = msg["handle"]
                text = msg["text"]
                attachments = msg.get("attachments", [])
                has_image = len(attachments) > 0
                log.info(f"Message from {handle}: {text[:80]}" + (f" [{len(attachments)} image(s)]" if has_image else ""))

                if is_tapback(text):
                    log.info(f"Ignoring tapback from {handle}")
                    continue

                if not text.strip() and not has_image:
                    log.info(f"Ignoring empty message from {handle}")
                    continue

                if handle not in config["security"]["allowed_numbers"]:
                    log.warning(f"Rejected from unauthorized handle: {handle}")
                    continue

                if is_rate_limited(handle, config["security"]["rate_limit_per_hour"]):
                    log.warning(f"Rate limit hit for {handle}")
                    send_imessage(handle, "You've sent too many messages. Try again in an hour.")
                    continue

                if not config["agents"]["menu"]["enabled"]:
                    send_imessage(handle, "The assistant is currently disabled.")
                    continue

                # Pending receipt confirmation — intercept yes/no before other routing
                if handle in _pending_receipt and not has_image:
                    lowered = text.strip().lower()
                    _YES = ("yes", "y", "yep", "yeah", "sure", "ok", "okay")
                    _NO  = ("no", "n", "nope", "nah", "cancel", "never mind", "nevermind")
                    if lowered in _YES:
                        pending = _pending_receipt.pop(handle)
                        log.info(f"Receipt confirmed by {handle}, running bridge")
                        _run_receipt_bridge(pending["image_path"], handle)
                        continue
                    elif lowered in _NO:
                        pending = _pending_receipt.pop(handle)
                        try:
                            Path(pending["image_path"]).unlink(missing_ok=True)
                        except Exception:
                            pass
                        log.info(f"Receipt declined by {handle}, discarding")
                        send_imessage(handle, "No worries, I'll leave it!")
                        continue
                    else:
                        # Not a yes/no — fall through to normal routing without consuming
                        log.info(f"Pending receipt for {handle} but reply was not yes/no, falling through")

                # Image messages — route via grocery detection or general vision
                # Exception: if admin is in awaiting_idea_content, pass caption text
                # to the menu session handler instead of the image router.
                if has_image:
                    _skip_image_route = False
                    menu_admin = config["security"].get("menu_admin")
                    if handle == menu_admin and MENU_SESSION_FILE.exists():
                        try:
                            _ms = json.loads(MENU_SESSION_FILE.read_text())
                            if _ms.get("state") == "awaiting_idea_content" and text.strip():
                                log.info(f"Admin in awaiting_idea_content — routing image caption to menu handler")
                                reply = menu_workflow.menu_agent_reply(text, _ms, config)
                                if reply:
                                    send_imessage(handle, reply)
                                _skip_image_route = True
                        except Exception as e:
                            log.error(f"Image/menu state check error: {e}")
                    if _skip_image_route:
                        continue
                    log.info(f"Routing image from {handle} to image router")
                    _idea_submitters = config["security"].get("idea_submitters", [])
                    reply = route_image_message(attachments, text, handle,
                                               idea_submitters=_idea_submitters)
                    if reply:
                        send_imessage(handle, reply)
                    continue

                # 20Q games intercept all messages while active
                if handle in _active_20q:
                    reply, _ = handle_20q_turn(handle, text)
                    send_imessage(handle, reply)
                    continue

                if handle in _active_20q_reverse:
                    reply, _ = handle_20q_reverse_turn(handle, text)
                    send_imessage(handle, reply)
                    continue

                is_kid = handle in config["security"].get("kids", [])

                if is_20q_reverse_start(text):
                    log.info(f"Starting 20Q reverse game for {handle} (is_kid={is_kid})")
                    reply = start_20q_reverse_game(handle, is_kid)
                    send_imessage(handle, reply)
                    continue

                if is_20q_start(text):
                    log.info(f"Starting 20Q game for {handle} (is_kid={is_kid})")
                    reply = start_20q_game(handle, is_kid)
                    send_imessage(handle, reply)
                    continue

                # Menu workflow — admin only
                menu_admin = config["security"].get("menu_admin")
                if handle == menu_admin and is_menu_start(text):
                    reply = menu_workflow.handle_start(config)
                    send_imessage(handle, reply)
                    continue

                if handle == menu_admin and MENU_SESSION_FILE.exists():
                    try:
                        session = json.loads(MENU_SESSION_FILE.read_text())
                        wf_state = session.get("state", "idle")
                        if wf_state not in ("idle", "complete", None):
                            if "cancel menu" in text.lower():
                                MENU_SESSION_FILE.write_text(json.dumps({"state": "idle"}))
                                send_imessage(handle, "Menu session cancelled.")
                            else:
                                reply = menu_workflow.menu_agent_reply(text, session, config)
                                if reply:
                                    send_imessage(handle, reply)
                            continue
                    except Exception as e:
                        log.error(f"Menu session routing error: {e}")

                # Menu approval pending — capture partner's response
                if MENU_PENDING_FILE.exists():
                    try:
                        pending = json.loads(MENU_PENDING_FILE.read_text())
                        if handle == pending.get("partner_handle"):
                            MENU_RESPONSE_FILE.write_text(json.dumps({
                                "received_at": datetime.now().isoformat(),
                                "message": text,
                            }))
                            MENU_PENDING_FILE.unlink()
                            admin_handle = config["security"].get("menu_admin")
                            if admin_handle:
                                outbox = json.loads(OUTBOX_FILE.read_text()) if OUTBOX_FILE.exists() else []
                                outbox.append({"handle": admin_handle, "text": f"Ashley replied to the menu: {text}"})
                                OUTBOX_FILE.write_text(json.dumps(outbox))
                            send_imessage(handle, "Thanks! Passed it on to David.")
                            log.info(f"Menu feedback captured from {handle}: {text[:80]}")
                            # Also advance the menu workflow if awaiting Ashley's signoff
                            if MENU_SESSION_FILE.exists():
                                try:
                                    wf_session = json.loads(MENU_SESSION_FILE.read_text())
                                    if wf_session.get("state") == "awaiting_ashley_signoff":
                                        menu_workflow.handle_ashley_reply(text, wf_session, config)
                                except Exception as e:
                                    log.error(f"Ashley reply session update error: {e}")
                            continue
                    except Exception as e:
                        log.error(f"Menu pending check error: {e}")

                # Sunday menu feedback from idea submitters → forward to admin
                admin = config["security"].get("menu_admin")
                idea_submitters = config["security"].get("idea_submitters", [])
                if (date.today().weekday() == 6
                        and handle in idea_submitters
                        and handle != admin
                        and admin):
                    sender_name = config["security"].get("handle_to_person", {}).get(handle, "Family member")
                    outbox = json.loads(OUTBOX_FILE.read_text()) if OUTBOX_FILE.exists() else []
                    outbox.append({"handle": admin, "text": f"{sender_name} re: menu — {text}"})
                    OUTBOX_FILE.write_text(json.dumps(outbox))
                    log.info(f"Forwarded Sunday menu feedback from {handle} to admin")

                # Everything else goes through the agent
                reply = get_reply(handle, text, config)
                send_imessage(handle, reply)

        except Exception as e:
            log.error(f"Main loop error: {e}", exc_info=True)
            try:
                send_imessage(handle, "Sorry, I'm having a bit of trouble right now. Try again in a moment!")
            except Exception:
                pass

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
