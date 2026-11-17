"""
Group chat games: chat-scoped SMS Activities, persisted to disk so a restart
doesn't end a game. One active game per group chat.

Story Chain — players take turns adding a sentence to a story Keanu opens and
closes. No hidden information, so everything happens in the group thread.

Functions here return the texts to post in the group; server.py sends them.
"""
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import List, Optional

import anthropic

log = logging.getLogger(__name__)

GAME_STATE_DIR = Path(__file__).parent / "game_state"
JOIN_TIMEOUT = 30 * 60        # a game nobody starts expires after 30 min
PLAY_TIMEOUT = 24 * 60 * 60   # games drift across an afternoon; expire after a day
MAX_LINE_CHARS = 300
MAX_NAME_CHARS = 20

_client = anthropic.Anthropic()
_MODEL = "claude-haiku-4-5-20251001"

# iMessage may turn "@Keanu" into a plain-text mention, so the @ is optional.
_MENTION = re.compile(r"@?\bkeanu\b", re.IGNORECASE)
_STORY_START = re.compile(r"\bstory\b", re.IGNORECASE)
_COMMAND = re.compile(r"^\s*@?keanu[\s,:]+(go|start|stop|end|quit|cancel|skip)\W*$", re.IGNORECASE)
_TURN_QUESTION = re.compile(r"\b(who|whose|next|turn)\b", re.IGNORECASE)
_JOIN = re.compile(r"^\s*(?:i'?m\s+)?in\b[\s,:!.-]*(.*?)\s*$", re.IGNORECASE)

_FALLBACK_OPENINGS = (
    "Once upon a time, a koala found a mysterious key glowing at the bottom of a eucalyptus tree.",
    "On the first day of summer, the school bus took a wrong turn and ended up somewhere very strange.",
    "Nobody in town believed the old lighthouse was haunted, until the night the light turned purple.",
)


# ── Persistence ────────────────────────────────────────────────────────────────

def _state_path(chat_guid: str) -> Path:
    # Group guids look like "any;+;<hex>"; the hex part is unique per chat.
    return GAME_STATE_DIR / (re.sub(r"[^A-Za-z0-9]", "_", chat_guid.split(";")[-1]) + ".json")


def load_game(chat_guid: str) -> Optional[dict]:
    path = _state_path(chat_guid)
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except Exception as e:
        log.error(f"Group game state unreadable for chat {chat_tag(chat_guid)}: {e}")
        return None


def is_expired(game: dict) -> bool:
    timeout = JOIN_TIMEOUT if game.get("phase") == "joining" else PLAY_TIMEOUT
    return time.time() - game.get("updated", 0) > timeout


def save_game(game: dict):
    GAME_STATE_DIR.mkdir(exist_ok=True)
    game["updated"] = time.time()
    path = _state_path(game["chat_guid"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(game, indent=2))
    os.replace(tmp, path)


def clear_game(chat_guid: str):
    try:
        _state_path(chat_guid).unlink()
    except FileNotFoundError:
        pass


def chat_tag(chat_guid: str) -> str:
    """Short chat id for logs."""
    return chat_guid[-6:]


# ── Parsing ────────────────────────────────────────────────────────────────────

def mentions_keanu(text: str) -> bool:
    return bool(_MENTION.search(text))


def is_story_start(text: str) -> bool:
    return bool(_MENTION.search(text) and _STORY_START.search(text))


def parse_command(text: str) -> Optional[str]:
    m = _COMMAND.match(text)
    if not m:
        return None
    word = m.group(1).lower()
    if word in ("go", "start"):
        return "go"
    if word == "skip":
        return "skip"
    return "stop"


def is_turn_question(text: str) -> bool:
    """Just "keanu", or a short "keanu who's next?" — not a story line that names Keanu."""
    if not _MENTION.search(text):
        return False
    rest = _MENTION.sub("", text).strip(" \t\n,:;?!.")
    return not rest or (len(rest) <= 40 and bool(_TURN_QUESTION.search(rest)))


def parse_join(text: str) -> Optional[str]:
    """Returns the name given after "in" ("" if none), or None if not a join."""
    m = _JOIN.match(text)
    if not m:
        return None
    name = re.sub(r"[^A-Za-z' -]", "", m.group(1)).strip()
    return name[:MAX_NAME_CHARS].strip()


# ── Story Chain ────────────────────────────────────────────────────────────────

def _player_name(handle: str, given: str, game: dict, config: dict) -> str:
    family = config.get("security", {}).get("handle_to_person", {})
    if handle in family:
        return family[handle]
    if given:
        return given.title()
    return f"Player {len(game['players']) + 1}"


def _total_turns(n_players: int) -> int:
    return n_players * (3 if n_players <= 4 else 2)


def _haiku(prompt: str, max_tokens: int = 150) -> str:
    response = _client.messages.create(
        model=_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip().strip('"')


def _opening_line() -> str:
    try:
        return _haiku(
            "You're Keanu, a friendly British koala, opening a group story game for kids. "
            "Write ONE imaginative, kid-friendly opening sentence that leaves lots of room "
            "for the next person. Reply with only the sentence."
        )
    except Exception as e:
        log.error(f"Story Chain opening error: {e}")
        return random.choice(_FALLBACK_OPENINGS)


def _ending_line(story: str) -> str:
    try:
        return _haiku(
            "You're Keanu, a friendly British koala, finishing a group story kids wrote one "
            "sentence at a time. Write a 1-2 sentence kid-friendly ending that ties it up. "
            "Reply with only the ending.\n\nStory so far:\n" + story,
            max_tokens=200,
        )
    except Exception as e:
        log.error(f"Story Chain ending error: {e}")
        return "And that's how it all ended, happily ever after."


def _story_text(game: dict) -> str:
    return " ".join([game["opening"]] + [line["text"] for line in game["lines"]])


def _current(game: dict) -> dict:
    return game["players"][game["turn"] % len(game["players"])]


def _turn_status(game: dict) -> str:
    return (f"It's {_current(game)['name']}'s turn ({len(game['lines']) + 1}/{game['total_turns']}). "
            "Commands: keanu skip, keanu stop.")


def start_story_chain(chat_guid: str, handle: str, config: dict) -> List[str]:
    game = {
        "game": "story_chain",
        "phase": "joining",
        "chat_guid": chat_guid,
        "starter": handle,
        "players": [],
        "turn": 0,
        "lines": [],
        "opening": "",
        "total_turns": 0,
        "created": time.time(),
    }
    game["players"].append({"handle": handle, "name": _player_name(handle, "", game, config)})
    save_game(game)
    log.info(f"Story Chain started in chat {chat_tag(chat_guid)}")
    return [
        "Story Chain! We'll build a story one sentence at a time.\n\n"
        f"{game['players'][0]['name']} is in. Everyone else, reply \"in\" to join "
        "(friends: \"in\" plus your name, like \"in Sam\").\n\n"
        "Say \"keanu go\" when everyone's ready, or \"keanu stop\" to cancel."
    ]


def _finish(game: dict, stopped_early: bool = False) -> List[str]:
    clear_game(game["chat_guid"])
    log.info(f"Story Chain finished in chat {chat_tag(game['chat_guid'])} "
             f"({len(game['lines'])} lines, stopped_early={stopped_early})")
    story = _story_text(game)
    ending = _ending_line(story)
    header = "Stopping here! Here's your story so far:" if stopped_early else "The end! Here's your story:"
    return [f"{header}\n\n{story} {ending}\n\nBrilliant work, storytellers!"]


def handle_story_chain(game: dict, handle: str, text: str, is_family: bool,
                       config: dict) -> List[str]:
    command = parse_command(text)
    in_game = any(p["handle"] == handle for p in game["players"])
    can_control = is_family or handle == game["starter"]

    if game["phase"] == "joining":
        if command == "stop" and can_control:
            clear_game(game["chat_guid"])
            log.info(f"Story Chain cancelled in chat {chat_tag(game['chat_guid'])}")
            return ["Story Chain cancelled. Maybe next time!"]
        if command == "go" and can_control:
            if len(game["players"]) < 2:
                return ["Need at least 2 players! Reply \"in\" to join."]
            game["phase"] = "playing"
            game["opening"] = _opening_line()
            game["total_turns"] = _total_turns(len(game["players"]))
            save_game(game)
            order = " → ".join(p["name"] for p in game["players"])
            rounds = game["total_turns"] // len(game["players"])
            return [
                f"Here we go! Order: {order}. {rounds} rounds, one sentence each.\n\n"
                f"\"{game['opening']}\"\n\n{_current(game)['name']}, you're up!"
            ]
        if command is None and not in_game:
            given = parse_join(text)
            if given is not None:
                name = _player_name(handle, given, game, config)
                game["players"].append({"handle": handle, "name": name})
                save_game(game)
                return [f"{name}'s in! ({len(game['players'])} players)"]
        return []

    # playing
    if command == "stop" and (can_control or in_game):
        return _finish(game, stopped_early=True)
    if command == "skip" and (can_control or in_game):
        skipped = _current(game)["name"]
        game["turn"] += 1
        save_game(game)
        return [f"Skipping {skipped}. {_current(game)['name']}, you're up!"]
    if command is not None:
        return []
    if handle != _current(game)["handle"]:
        # Kids chat between turns; only answer when someone asks Keanu
        return [_turn_status(game)] if _MENTION.search(text) else []
    if is_turn_question(text):
        return [_turn_status(game)]

    line = text.strip()[:MAX_LINE_CHARS]
    if not line:
        return []
    game["lines"].append({"name": _current(game)["name"], "text": line})
    game["turn"] += 1
    if len(game["lines"]) >= game["total_turns"]:
        return _finish(game)
    save_game(game)
    return [f"{_current(game)['name']}, you're up! ({len(game['lines']) + 1}/{game['total_turns']})"]


# ── Entry point ────────────────────────────────────────────────────────────────

def handle_group_game_message(chat_guid: str, handle: str, text: str, is_family: bool,
                              config: dict) -> Optional[List[str]]:
    """
    Returns texts to post in the group, or None if no game claimed the message
    (no game active and it wasn't a game start).
    """
    game = load_game(chat_guid)
    notice = []
    if game and is_expired(game):
        # Announce once (the state is cleared here), then treat the message as
        # if no game were running — it may be a fresh start.
        clear_game(chat_guid)
        log.info(f"Group game in chat {chat_tag(chat_guid)} expired")
        if game.get("phase") == "playing":
            notice = [f"Our story timed out. Here's how far we got:\n\n{_story_text(game)}\n\n"
                      "Say \"keanu story\" to start a new one."]
        game = None
    if game:
        if game["game"] == "story_chain":
            return handle_story_chain(game, handle, text, is_family, config)
        log.error(f"Unknown game {game.get('game')!r} in chat {chat_tag(chat_guid)}, clearing")
        clear_game(chat_guid)
        return None
    if is_family and is_story_start(text):
        return notice + start_story_chain(chat_guid, handle, config)
    return notice or None
