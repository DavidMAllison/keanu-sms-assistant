"""
Keanu - iMessage AI assistant
Polls chat.db for new messages, routes to Claude via tool use, replies via AppleScript.
"""

import json
import logging
import os
import sqlite3
import subprocess
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import anthropic
import yaml
from dotenv import load_dotenv

from agent import get_reply
from agents import menu_workflow
from tools import send_imessage, send_imessage_group, drain_outbox

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────

CONFIG_FILE = Path(__file__).parent / "config/settings.yaml"
STATE_FILE = Path(__file__).parent / ".keanu_state.json"
OUTBOX_FILE = Path(__file__).parent / ".outbox.json"
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
            SELECT m.rowid, m.text, h.id AS sender_handle
            FROM message m
            JOIN handle h ON m.handle_id = h.rowid
            JOIN chat_message_join cmj ON cmj.message_id = m.rowid
            JOIN chat c ON c.rowid = cmj.chat_id
            WHERE m.is_from_me = 0
              AND c.chat_identifier NOT LIKE 'chat%'
              AND m.rowid > ?
              AND m.text IS NOT NULL
              AND m.text != ''
              {date_filter}
            ORDER BY m.rowid ASC
        """, params)
        return [{"rowid": row[0], "text": row[1], "handle": row[2]} for row in cursor.fetchall()]
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


def main():
    log.info("Keanu starting up...")
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
                log.info(f"Message from {handle}: {text[:80]}")

                if is_tapback(text):
                    log.info(f"Ignoring tapback from {handle}")
                    continue

                if not text.strip():
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
                                reply = menu_workflow.dispatch(text, session, config)
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
