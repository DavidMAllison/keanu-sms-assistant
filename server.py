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

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────

CONFIG_FILE = Path(__file__).parent / "config/settings.yaml"
STATE_FILE = Path(__file__).parent / ".keanu_state.json"
GAPS_FILE = Path(__file__).parent / "capability_gaps.json"
OUTBOX_FILE = Path(__file__).parent / ".outbox.json"
CHAT_DB = Path.home() / "Library/Messages/chat.db"
POLL_INTERVAL = 3

# ── Config / state ────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f)

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_rowid": 0, "last_gap_notify": 0.0}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state))

# ── Capability gaps ────────────────────────────────────────────────────────────

def unreviewed_gaps() -> list:
    if not GAPS_FILE.exists():
        return []
    return [g for g in json.loads(GAPS_FILE.read_text()) if not g.get("reviewed")]

# ── Outbox ────────────────────────────────────────────────────────────────────

def drain_outbox():
    if not OUTBOX_FILE.exists():
        return
    try:
        messages = json.loads(OUTBOX_FILE.read_text())
        OUTBOX_FILE.unlink()
        for entry in messages:
            send_imessage(entry["handle"], entry["text"])
            log.info(f"Outbox: sent to {entry['handle']}")
    except Exception as e:
        log.error(f"Outbox drain error: {e}")

# ── School countdown ──────────────────────────────────────────────────────────

SCHOOL_LAST_DAY = date(2026, 5, 22)
_COUNTDOWN_SEND_AFTER = 7   # 7 AM
_COUNTDOWN_SEND_BEFORE = 10  # stop sending if server was down past 10 AM


def _school_days_remaining(from_date: date) -> int:
    count = 0
    check = from_date + timedelta(days=1)
    while check <= SCHOOL_LAST_DAY:
        if check.weekday() < 5:
            count += 1
        check += timedelta(days=1)
    return count


def maybe_send_school_countdown(state: dict, config: dict):
    today = date.today()
    now = datetime.now()

    if today.weekday() >= 5:  # skip weekends
        return
    if today > SCHOOL_LAST_DAY:
        return
    if not (_COUNTDOWN_SEND_AFTER <= now.hour < _COUNTDOWN_SEND_BEFORE):
        return
    if state.get("last_countdown_date") == today.isoformat():
        return

    remaining = _school_days_remaining(today)
    handle_to_person = config["security"].get("handle_to_person", {})
    kids = config["security"].get("kids", [])

    for handle in kids:
        name = handle_to_person.get(handle, "there")
        if today == SCHOOL_LAST_DAY:
            msg = f"Good morning, {name}! Today is the LAST day of school — have a brilliant one! Have a great summer!"
        elif remaining == 1:
            msg = f"Good morning, {name}! Just 1 school day left after today — nearly there!"
        else:
            msg = f"Good morning, {name}! {remaining} school days left until summer!"
        send_imessage(handle, msg)
        log.info(f"School countdown sent to {handle}: {remaining} days remaining")

    state["last_countdown_date"] = today.isoformat()
    save_state(state)


# ── Weekly koala fact ─────────────────────────────────────────────────────────

_KOALA_FACT_INTERVAL_DAYS = 7
_koala_client = anthropic.Anthropic()


def _generate_koala_fact() -> str:
    response = _koala_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": (
                "You are Keanu, an Australian koala texting kids. Share one surprising or funny "
                "koala fact in your voice — warm, cheeky, Australian slang welcome. "
                "Two sentences max. No 'Did you know' opener — just dive straight in."
            ),
        }],
    )
    return response.content[0].text.strip()


def maybe_send_koala_fact(state: dict, config: dict):
    now = datetime.now()
    today = date.today()

    if not (_COUNTDOWN_SEND_AFTER <= now.hour < _COUNTDOWN_SEND_BEFORE):
        return

    last_sent = state.get("last_koala_fact_date")
    if last_sent and (today - date.fromisoformat(last_sent)).days < _KOALA_FACT_INTERVAL_DAYS:
        return

    try:
        fact = _generate_koala_fact()
    except Exception as e:
        log.error(f"Koala fact generation error: {e}")
        return

    kids = config["security"].get("kids", [])
    for handle in kids:
        send_imessage(handle, fact)
        log.info(f"Koala fact sent to {handle}")

    state["last_koala_fact_date"] = today.isoformat()
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

# ── iMessage send ──────────────────────────────────────────────────────────────

def send_imessage(handle: str, text: str):
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
            WHERE m.is_from_me = 0
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
_20Q_START_PATTERNS = ("20 questions", "twenty questions", "play 20", "play twenty")
_20Q_QUIT_PHRASES = ("give up", "i give up", "what is it", "don't know", "no idea", "quit", "stop the game", "end game")


def is_20q_start(text: str) -> bool:
    lowered = text.lower()
    return any(p in lowered for p in _20Q_START_PATTERNS)


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
            drain_outbox()
            config = load_config()
            maybe_send_school_countdown(state, config)
            maybe_send_koala_fact(state, config)
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

                # 20Q game intercepts all messages while active
                if handle in _active_20q:
                    reply, _ = handle_20q_turn(handle, text)
                    send_imessage(handle, reply)
                    continue

                if is_20q_start(text):
                    is_kid = handle in config["security"].get("kids", [])
                    log.info(f"Starting 20Q game for {handle} (is_kid={is_kid})")
                    reply = start_20q_game(handle, is_kid)
                    send_imessage(handle, reply)
                    continue

                # Remind admin of unreviewed gaps (at most once per day)
                if handle == config["security"].get("menu_admin"):
                    if time.time() - state.get("last_gap_notify", 0) > 86400:
                        gaps = unreviewed_gaps()
                        if gaps:
                            count = len(gaps)
                            state["last_gap_notify"] = time.time()
                            save_state(state)
                            send_imessage(handle, f"Heads up — {count} unreviewed capability gap{'s' if count > 1 else ''} in capability_gaps.json.")

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
