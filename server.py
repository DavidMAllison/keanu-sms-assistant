"""
Keanu - iMessage AI assistant
Polls chat.db for new messages, routes to Claude, replies via AppleScript.
"""

import os
import time
import json
import sqlite3
import subprocess
import yaml
import logging
from collections import defaultdict
from pathlib import Path
from typing import Optional

import anthropic
from dotenv import load_dotenv

from agents.menu_agent import (
    load_system_prompt, load_context, find_recipe_for_message,
    is_recipe_idea, save_recipe_idea, is_menu_change, update_meal_plan,
    detect_feedback, has_feedback_reason, guess_recipe_from_context,
    parse_per_person_feedback, save_feedback,
    RECIPES_DIR, find_all_recipe_matches, extract_recipe_text,
    RECIPE_CHUNK_CHARS, split_into_chunks, get_dropbox_preview_url,
)
from agents.fun_agent import load_system_prompt as load_fun_prompt
from agents.schedule_agent import is_schedule_request, get_schedule_reply

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_FILE = Path(__file__).parent / "config/settings.yaml"
STATE_FILE = Path(__file__).parent / ".keanu_state.json"
GAPS_FILE = Path(__file__).parent / "capability_gaps.json"
CHAT_DB = Path.home() / "Library/Messages/chat.db"
POLL_INTERVAL = 3  # seconds

def load_config():
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f)

# ── State (last processed message ROWID) ─────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_rowid": 0}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state))


# ── Capability gaps ───────────────────────────────────────────────────────────

def save_gap(handle: str, message: str):
    gaps = json.loads(GAPS_FILE.read_text()) if GAPS_FILE.exists() else []
    gaps.append({"date": time.strftime("%Y-%m-%d"), "handle": handle, "request": message, "reviewed": False})
    GAPS_FILE.write_text(json.dumps(gaps, indent=2))

def unreviewed_gaps() -> list:
    if not GAPS_FILE.exists():
        return []
    return [g for g in json.loads(GAPS_FILE.read_text()) if not g.get("reviewed")]

# ── iMessage send via AppleScript ─────────────────────────────────────────────

def send_imessage(handle: str, text: str):
    """Send an iMessage to a handle (phone number or Apple ID) via AppleScript."""
    safe_text = text.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
    tell application "Messages"
        set targetService to 1st service whose service type = iMessage
        set targetBuddy to buddy "{handle}" of targetService
        send "{safe_text}" to targetBuddy
    end tell
    '''
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f"AppleScript error sending to {handle}: {result.stderr.strip()}")
    else:
        log.info(f"Sent reply to {handle}")

# ── chat.db polling ───────────────────────────────────────────────────────────

def get_max_rowid() -> int:
    conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT MAX(rowid) FROM message").fetchone()
        return row[0] or 0
    finally:
        conn.close()

def poll_new_messages(last_rowid: int, min_date: Optional[float] = None) -> list[dict]:
    """Return new incoming messages with rowid > last_rowid."""
    conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    try:
        # chat.db stores dates as nanoseconds since 2001-01-01 (Apple epoch)
        APPLE_EPOCH_OFFSET = 978307200  # seconds between Unix epoch and Apple epoch
        date_filter = ""
        params: list = [last_rowid]
        if min_date is not None:
            date_filter = "AND m.date >= ?"
            params.append(int((min_date - APPLE_EPOCH_OFFSET) * 1e9))

        cursor = conn.execute(f"""
            SELECT m.rowid, m.text, h.id AS sender_handle
            FROM message m
            JOIN handle h ON m.handle_id = h.rowid
            WHERE m.is_from_me = 0
              AND m.rowid > ?
              AND m.text IS NOT NULL
              AND m.text != ''
              {date_filter}
            ORDER BY m.rowid ASC
        """, params)
        return [
            {"rowid": row[0], "text": row[1], "handle": row[2]}
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()

# ── Rate limiting ─────────────────────────────────────────────────────────────

_rate_limit_store: dict[str, list[float]] = defaultdict(list)

def is_rate_limited(handle: str, limit: int) -> bool:
    now = time.time()
    window = 3600
    _rate_limit_store[handle] = [t for t in _rate_limit_store[handle] if now - t < window]
    if len(_rate_limit_store[handle]) >= limit:
        return True
    _rate_limit_store[handle].append(now)
    return False

# ── Conversation history ──────────────────────────────────────────────────────

_conversation_history: dict[str, list[dict]] = defaultdict(list)
MAX_HISTORY = 10

# Pending feedback: handle -> {sentiment, recipe}
_pending_feedback: dict[str, dict] = {}

# Pending recipe: handle -> {type: "disambiguate"|"send_choice", ...}
_pending_recipe: dict[str, dict] = {}

# ── Intent detection ─────────────────────────────────────────────────────────

_FUN_KEYWORDS = {
    "joke", "jokes", "riddle", "riddles", "knock knock", "knock-knock",
    "funny", "make me laugh", "tell me a joke", "give me a joke",
    "tell me a riddle", "give me a riddle",
    "trivia", "fun fact", "tell me a fact", "would you rather",
    "story", "tell me a story", "make up a story",
    "tongue twister", "brain teaser", "guess what",
    "game", "play a game", "quiz",
}

def is_recipe_fetch_request(message: str) -> bool:
    lowered = message.lower()
    if "recipe" not in lowered:
        return False
    return any(p in lowered for p in (
        "give me", "send me", "can i get", "share", "what's the recipe",
        "whats the recipe", "the recipe for", "full recipe",
    ))


def send_recipe_chunks(handle: str, chunks: list[str]):
    for i, chunk in enumerate(chunks):
        if i > 0:
            time.sleep(0.5)
        send_imessage(handle, chunk)


def is_fun_request(text: str) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in _FUN_KEYWORDS)

def is_kid_handle(handle: str, config: dict) -> bool:
    return handle in config["security"].get("kids", [])

# ── Claude ────────────────────────────────────────────────────────────────────

anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

def ask_fun_agent(handle: str, message: str, is_kid: bool) -> str:
    system_prompt = load_fun_prompt(is_kid)
    history = _conversation_history[handle]
    history.append({"role": "user", "content": message})

    if len(history) > MAX_HISTORY * 2:
        history = history[-(MAX_HISTORY * 2):]
        _conversation_history[handle] = history

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=system_prompt,
            messages=history,
        )
        reply = response.content[0].text.strip()
    except Exception as e:
        log.error(f"Claude API error (fun agent): {e}")
        reply = "Sorry, I couldn't think of one. Try again!"

    history.append({"role": "assistant", "content": reply})
    return reply


def ask_menu_agent(handle: str, message: str) -> str:
    system_prompt = load_system_prompt()
    context = load_context()
    full_system = f"{system_prompt}\n\n## Current Context\n{context}"

    recipe_text = find_recipe_for_message(message)
    if recipe_text:
        full_system += f"\n\n## Recipe Details\n{recipe_text}"

    history = _conversation_history[handle]
    history.append({"role": "user", "content": message})

    if len(history) > MAX_HISTORY * 2:
        history = history[-(MAX_HISTORY * 2):]
        _conversation_history[handle] = history

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            system=full_system,
            messages=history,
        )
        reply = response.content[0].text.strip()
    except Exception as e:
        log.error(f"Claude API error: {e}")
        reply = "Sorry, I ran into an error. Try again in a moment."

    history.append({"role": "assistant", "content": reply})
    return reply

# ── Main loop ─────────────────────────────────────────────────────────────────

STARTUP_GRACE_SECONDS = 120  # ignore messages older than this on startup

def main():
    log.info("Keanu starting up...")
    state = load_state()

    # On first run, skip existing messages to avoid replaying history
    if state["last_rowid"] == 0:
        state["last_rowid"] = get_max_rowid()
        save_state(state)
        log.info(f"First run: starting from ROWID {state['last_rowid']}")

    log.info(f"Polling {CHAT_DB} every {POLL_INTERVAL}s (last ROWID: {state['last_rowid']})")

    startup_cutoff = time.time() - STARTUP_GRACE_SECONDS
    first_poll = True

    while True:
        try:
            config = load_config()
            min_date = startup_cutoff if first_poll else None
            messages = poll_new_messages(state["last_rowid"], min_date)

            first_poll = False
            for msg in messages:
                state["last_rowid"] = msg["rowid"]
                save_state(state)

                handle = msg["handle"]
                text = msg["text"]
                log.info(f"Message from {handle}: {text[:80]}")

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

                # Resolve pending recipe state first
                if handle in _pending_recipe:
                    pending = _pending_recipe.pop(handle)

                    if pending["type"] == "disambiguate":
                        reply_lower = text.lower()
                        chosen = None
                        for m in pending["matches"]:
                            name_words = [w for w in m["name"].lower().split() if len(w) > 3]
                            if sum(1 for w in name_words if w in reply_lower) >= 1:
                                chosen = m
                                break
                        if chosen:
                            recipe_text = extract_recipe_text(RECIPES_DIR / chosen["filename"])
                            if not recipe_text:
                                send_imessage(handle, "Couldn't read that one, sorry.")
                            elif len(recipe_text) <= RECIPE_CHUNK_CHARS:
                                send_imessage(handle, recipe_text)
                            else:
                                _pending_recipe[handle] = {"type": "send_choice", "name": chosen["name"], "filename": chosen["filename"]}
                                send_imessage(handle, "That one's long — multiple texts or a Dropbox link?")
                        else:
                            names = ", ".join(m["name"] for m in pending["matches"])
                            send_imessage(handle, f"Not sure which one — {names}?")
                            _pending_recipe[handle] = pending
                        continue

                    elif pending["type"] == "send_choice":
                        reply_lower = text.lower()
                        if any(w in reply_lower for w in ("link", "dropbox", "url")):
                            url = get_dropbox_preview_url(pending["filename"])
                            send_imessage(handle, url)
                        elif any(w in reply_lower for w in ("multiple", "chunks", "texts", "messages", "send")):
                            recipe_text = extract_recipe_text(RECIPES_DIR / pending["filename"])
                            if recipe_text:
                                send_recipe_chunks(handle, split_into_chunks(recipe_text))
                            else:
                                send_imessage(handle, "Couldn't read that one, sorry.")
                        else:
                            _pending_recipe[handle] = pending
                            send_imessage(handle, "Multiple texts or a Dropbox link?")
                        continue

                # Resolve pending feedback
                if handle in _pending_feedback:
                    pending = _pending_feedback.pop(handle)
                    if has_feedback_reason(text):
                        entries = parse_per_person_feedback(text, handle)
                        save_feedback(pending["recipe"], entries)
                        reply = f"Logged for {pending['recipe']}."
                    else:
                        reply = "Go on then — what specifically did you think?"
                        _pending_feedback[handle] = pending  # keep waiting
                    send_imessage(handle, reply)
                    continue

                # Detect new feedback
                sentiment = detect_feedback(text)
                if sentiment:
                    recipe = guess_recipe_from_context(text)
                    if recipe and has_feedback_reason(text):
                        entries = parse_per_person_feedback(text, handle)
                        save_feedback(recipe, entries)
                        reply = f"Logged for {recipe}."
                        send_imessage(handle, reply)
                        continue
                    elif recipe:
                        _pending_feedback[handle] = {"sentiment": sentiment, "recipe": recipe}
                        reply = f"Good to know — what did you think specifically about {recipe}?"
                        send_imessage(handle, reply)
                        continue

                menu_admin = config["security"].get("menu_admin")

                if is_menu_change(text):
                    if handle == menu_admin:
                        new_recipe = update_meal_plan(text)
                        if new_recipe:
                            log.info(f"Menu updated by admin: {new_recipe}")
                            reply = f"Done, changed to {new_recipe}."
                        else:
                            reply = "Couldn't quite parse that — try something like 'change Thursday to chicken tacos'."
                    else:
                        reply = "Sorry, only David can change the menu!"
                    send_imessage(handle, reply)
                    continue

                if is_recipe_idea(text):
                    idea_submitters = config["security"].get("idea_submitters", [])
                    if handle in idea_submitters:
                        ok = save_recipe_idea(text)
                        reply = "Saved to the ideas list, brilliant!" if ok else "Hmm, couldn't save that one."
                        log.info(f"Recipe idea from {handle}: {text[:60]}")
                    else:
                        reply = "Nice idea, but only the grown-ups can add to the list!"
                    send_imessage(handle, reply)
                    continue

                if is_recipe_fetch_request(text):
                    matches = find_all_recipe_matches(text)
                    if len(matches) == 1:
                        m = matches[0]
                        recipe_text = extract_recipe_text(RECIPES_DIR / m["filename"])
                        if recipe_text:
                            if len(recipe_text) <= RECIPE_CHUNK_CHARS:
                                send_imessage(handle, recipe_text)
                            else:
                                _pending_recipe[handle] = {"type": "send_choice", "name": m["name"], "filename": m["filename"]}
                                send_imessage(handle, f"{m['name']} is too long for one text — multiple messages or a Dropbox link?")
                            continue
                    elif len(matches) > 1:
                        _pending_recipe[handle] = {"type": "disambiguate", "matches": matches}
                        names = "\n".join(f"- {m['name']}" for m in matches)
                        send_imessage(handle, f"Found a few — which one?\n{names}")
                        continue
                    # 0 matches: fall through to menu agent

                # Remind admin of unreviewed gaps
                if handle == menu_admin:
                    gaps = unreviewed_gaps()
                    if gaps:
                        count = len(gaps)
                        send_imessage(handle, f"Heads up — {count} unreviewed capability gap{'s' if count > 1 else ''} in capability_gaps.json.")

                if is_schedule_request(text):
                    log.info(f"Routing to schedule agent for {handle}")
                    reply = get_schedule_reply(handle, text, config)
                    send_imessage(handle, reply)
                    continue

                if is_fun_request(text):
                    kid = is_kid_handle(handle, config)
                    log.info(f"Routing to fun agent (is_kid={kid})")
                    reply = ask_fun_agent(handle, text, kid)
                else:
                    reply = ask_menu_agent(handle, text)

                # Detect gap marker phrase and log the original request
                if "Can't do that one yet, noted." in reply:
                    save_gap(handle, text)
                    log.info(f"Capability gap logged from {handle}: {text[:80]}")

                send_imessage(handle, reply)

        except Exception as e:
            log.error(f"Polling error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
