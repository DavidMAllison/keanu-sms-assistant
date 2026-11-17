"""
Group chat games: chat-scoped SMS Activities, persisted to disk so a restart
doesn't end a game. One active game per group chat.

Both games share one lobby: a family member starts it, players reply "in",
and "keanu go" begins.

Story Chain — players take turns adding a sentence to a story Keanu opens and
closes. No hidden information, so everything happens in the group thread.

Imposter — Keanu privately texts every player a secret word except one, the
imposter, who only gets the category. Players give clues in turn, vote on who
the imposter is, and a caught imposter gets one guess at the word. Private
texts go only to that game's players, only at the start, and only carry the
word or the role. The word and the imposter are never logged.

Functions here return the texts to post in the group; server.py sends them.
Private texts go through the send_private callable server.py passes in.
"""
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Callable, List, Optional

import anthropic

log = logging.getLogger(__name__)

GAME_STATE_DIR = Path(__file__).parent / "game_state"
JOIN_TIMEOUT = 30 * 60        # a game nobody starts expires after 30 min
PLAY_TIMEOUT = 24 * 60 * 60   # games drift across an afternoon; expire after a day
MAX_LINE_CHARS = 300
MAX_CLUE_CHARS = 80
IMPOSTER_CLUE_ROUNDS = 2
MAX_NAME_CHARS = 30

_client = anthropic.Anthropic()
_MODEL = "claude-haiku-4-5-20251001"

# iMessage may turn "@Keanu" into a plain-text mention, so the @ is optional.
_MENTION = re.compile(r"@?\bkeanu\b", re.IGNORECASE)
_STORY_START = re.compile(r"\bstory\b", re.IGNORECASE)
_IMPOSTER_START = re.compile(r"\bimpost[eo]r\b", re.IGNORECASE)
_COMMAND = re.compile(r"^\s*@?keanu[\s,:]+(go|start|stop|end|quit|cancel|skip|reveal)\W*$", re.IGNORECASE)
_TURN_QUESTION = re.compile(r"\b(who|whose|next|turn)\b", re.IGNORECASE)
_JOIN = re.compile(r"^\s*(?:i'?m\s+)?in\b[\s,:!.-]*(.*?)\s*$", re.IGNORECASE)

_FALLBACK_OPENINGS = (
    "Once upon a time, a koala found a mysterious key glowing at the bottom of a eucalyptus tree.",
    "On the first day of summer, the school bus took a wrong turn and ended up somewhere very strange.",
    "Nobody in town believed the old lighthouse was haunted, until the night the light turned purple.",
)

_IMPOSTER_CATEGORIES = {
    "Animals": ("penguin", "giraffe", "octopus", "kangaroo", "hamster"),
    "Foods": ("pizza", "pancakes", "tacos", "popcorn", "spaghetti"),
    "Places": ("beach", "library", "zoo", "airport", "playground"),
    "Sports": ("soccer", "swimming", "basketball", "skiing", "gymnastics"),
    "Things at home": ("toaster", "bathtub", "couch", "fridge", "pillow"),
    "Jobs": ("firefighter", "teacher", "astronaut", "chef", "doctor"),
}


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


def game_start(text: str) -> Optional[str]:
    """Which game a message asks to start, or None."""
    if not _MENTION.search(text):
        return None
    if _IMPOSTER_START.search(text):
        return "imposter"
    if _STORY_START.search(text):
        return "story_chain"
    return None


def parse_command(text: str) -> Optional[str]:
    m = _COMMAND.match(text)
    if not m:
        return None
    word = m.group(1).lower()
    if word in ("go", "start"):
        return "go"
    if word in ("skip", "reveal"):
        return word
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
    """A name given when joining ("in Sam") wins for that game, family included;
    otherwise the family name from config, else a numbered player."""
    if given:
        # Keep the player's own casing ("WeWishYouADinoChristmas"); just
        # capitalize an all-lowercase name ("sam" -> "Sam")
        return given[0].upper() + given[1:] if given.islower() else given
    family = config.get("security", {}).get("handle_to_person", {})
    if handle in family:
        return family[handle]
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


def _who_is_in(game: dict) -> str:
    names = [p["name"] for p in game["players"]]
    if len(names) == 1:
        return f"{names[0]}'s in"
    return f"{', '.join(names[:-1])} and {names[-1]} are in"


def _join_status(game: dict) -> str:
    return f"Waiting to start: {_who_is_in(game)}. Reply \"in\" to join, or \"keanu go\" to begin."


def _turn_status(game: dict) -> str:
    return (f"It's {_current(game)['name']}'s turn ({len(game['lines']) + 1}/{game['total_turns']}). "
            "Commands: keanu skip, keanu stop.")


_TITLES = {"story_chain": "Story Chain", "imposter": "Imposter"}
_MIN_PLAYERS = {"story_chain": 2, "imposter": 3}
_JOIN_HELP = ("Everyone else, reply \"in\" to join "
              "(friends: \"in\" plus your name, like \"in Sam\").")
_INTROS = {
    "story_chain": "Story Chain! We'll build a story one sentence at a time.",
    "imposter": ("Imposter! I'll text everyone a secret word, except one sneaky imposter. "
                 "Give clues, then vote out the imposter. Needs 3 or more players."),
}


def start_game(kind: str, chat_guid: str, handle: str, config: dict) -> List[str]:
    game = {
        "game": kind,
        "phase": "joining",
        "chat_guid": chat_guid,
        "starter": handle,
        "players": [],
        "turn": 0,
        "created": time.time(),
    }
    if kind == "story_chain":
        game.update({"lines": [], "opening": "", "total_turns": 0})
    game["players"].append({"handle": handle, "name": _player_name(handle, "", game, config)})
    save_game(game)
    log.info(f"{_TITLES[kind]} started in chat {chat_tag(chat_guid)}")
    return [
        f"{_INTROS[kind]}\n\n"
        f"{game['players'][0]['name']} is in. {_JOIN_HELP}\n\n"
        "Say \"keanu go\" when everyone's ready, or \"keanu stop\" to cancel."
    ]


def start_story_chain(chat_guid: str, handle: str, config: dict) -> List[str]:
    return start_game("story_chain", chat_guid, handle, config)


def _handle_lobby(game: dict, handle: str, text: str, command: Optional[str], in_game: bool,
                  can_control: bool, config: dict, send_private: Optional[Callable]) -> List[str]:
    kind = game["game"]
    if command == "stop" and can_control:
        clear_game(game["chat_guid"])
        log.info(f"{_TITLES[kind]} cancelled in chat {chat_tag(game['chat_guid'])}")
        return [f"{_TITLES[kind]} cancelled. Maybe next time!"]
    if command == "go" and can_control:
        need = _MIN_PLAYERS[kind]
        if len(game["players"]) < need:
            return [f"Need at least {need} players. {_who_is_in(game)} so far. Reply \"in\" to join."]
        if kind == "imposter":
            return _imposter_go(game, send_private)
        return _story_go(game)
    if command is None and not in_game:
        given = parse_join(text)
        if given is None and game_start(text):
            given = ""  # "keanu story" while one is waiting means they want to play
        if given is not None:
            name = _player_name(handle, given, game, config)
            game["players"].append({"handle": handle, "name": name})
            save_game(game)
            return [f"{name}'s in! ({len(game['players'])} players)"]
    if command is None and mentions_keanu(text):
        return [_join_status(game)]
    return []


def _story_go(game: dict) -> List[str]:
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
        return _handle_lobby(game, handle, text, command, in_game, can_control, config, None)

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


# ── Imposter ───────────────────────────────────────────────────────────────────

def _pick_secret() -> tuple:
    category = random.choice(list(_IMPOSTER_CATEGORIES))
    try:
        word = _haiku(
            f"Pick ONE common word in the category \"{category}\" that every kid aged 8 "
            "and up knows, for a guessing game. Reply with only the word.",
            max_tokens=20,
        )
        word = re.sub(r"[^A-Za-z' -]", "", word).strip()
        if not word or len(word) > 30:
            raise ValueError(f"unusable word {word!r}")
    except Exception as e:
        log.error(f"Imposter word pick error: {e}")
        word = random.choice(_IMPOSTER_CATEGORIES[category])
    return category, word.lower()


def _name_of(game: dict, handle: str) -> str:
    return next(p["name"] for p in game["players"] if p["handle"] == handle)


def _imposter_reveal(game: dict) -> str:
    return f"The imposter was {_name_of(game, game['imposter'])} and the word was {game['word'].upper()}."


def _imposter_go(game: dict, send_private: Optional[Callable]) -> List[str]:
    category, word = _pick_secret()
    imposter = random.choice(game["players"])["handle"]
    failed = []
    for p in game["players"]:
        if p["handle"] == imposter:
            msg = f"You're the IMPOSTER! Category: {category}. Blend in."
        else:
            msg = (f"Imposter: the secret word is {word.upper()} ({category}). "
                   "Don't say it! Give a clue that shows you know it.")
        if send_private is None or not send_private(p["handle"], msg):
            failed.append(p["name"])
    if failed:
        clear_game(game["chat_guid"])
        log.warning(f"Imposter cancelled in chat {chat_tag(game['chat_guid'])}: "
                    f"private text failed for {len(failed)} player(s)")
        return [f"I couldn't text {' and '.join(failed)} privately, so Imposter is cancelled. "
                "Try Story Chain instead, or start Imposter without them."]
    game.update({
        "phase": "playing", "stage": "clues", "category": category, "word": word,
        "imposter": imposter, "clues": [], "votes": {},
        "total_turns": len(game["players"]) * IMPOSTER_CLUE_ROUNDS,
    })
    save_game(game)
    log.info(f"Imposter dealt in chat {chat_tag(game['chat_guid'])} ({len(game['players'])} players)")
    order = " → ".join(p["name"] for p in game["players"])
    return [
        "Words sent, check your texts from Keanu! "
        f"{IMPOSTER_CLUE_ROUNDS} rounds of clues, a word or two each. Order: {order}.\n\n"
        f"{_current(game)['name']}, you're up!"
    ]


def _imposter_status(game: dict) -> str:
    stage = game["stage"]
    if stage == "clues":
        return (f"It's {_current(game)['name']}'s turn for a clue "
                f"({len(game['clues']) + 1}/{game['total_turns']}). Commands: keanu skip, keanu stop.")
    if stage == "voting":
        waiting = [p["name"] for p in game["players"] if p["handle"] not in game["votes"]]
        return f"Waiting on votes from {', '.join(waiting)}. Reply with who you think the imposter is."
    return f"Waiting for {_name_of(game, game['imposter'])}'s guess at the word."


def _parse_vote(game: dict, text: str) -> List[dict]:
    """Players whose name appears in the message."""
    return [p for p in game["players"]
            if re.search(r"\b" + re.escape(p["name"]) + r"\b", text, re.IGNORECASE)]


def _normalize_word(text: str) -> str:
    w = re.sub(r"[^a-z ]", "", text.lower()).strip()
    w = re.sub(r"^((a|an|the|is it|it is|its|i guess|my guess is)\s+)+", "", w).strip()
    w = w.replace(" ", "")
    if re.search(r"(ch|sh|x|z|ss)es$", w):
        return w[:-2]
    return w[:-1] if w.endswith("s") and not w.endswith("ss") and len(w) > 3 else w


def _tally(game: dict) -> List[str]:
    counts: dict = {}
    for target in game["votes"].values():
        counts[target] = counts.get(target, 0) + 1
    summary = ", ".join(f"{_name_of(game, h)} {n}" for h, n in
                        sorted(counts.items(), key=lambda kv: -kv[1]))
    top = max(counts.values())
    leaders = [h for h, n in counts.items() if n == top]
    imposter_name = _name_of(game, game["imposter"])
    if len(leaders) == 1 and leaders[0] == game["imposter"]:
        game["stage"] = "guess"
        save_game(game)
        return [f"Votes: {summary}.\n\nBusted! {imposter_name} was the imposter! "
                f"{imposter_name}, you get one guess at the word. What is it?"]
    clear_game(game["chat_guid"])
    log.info(f"Imposter finished in chat {chat_tag(game['chat_guid'])} (imposter escaped)")
    why = "It's a tie, so the imposter escapes!" if len(leaders) > 1 else "Wrong person!"
    return [f"Votes: {summary}.\n\n{why} {_imposter_reveal(game)} {imposter_name} wins!"]


def handle_imposter(game: dict, handle: str, text: str, is_family: bool, config: dict,
                    send_private: Optional[Callable]) -> List[str]:
    command = parse_command(text)
    in_game = any(p["handle"] == handle for p in game["players"])
    can_control = is_family or handle == game["starter"]

    if game["phase"] == "joining":
        return _handle_lobby(game, handle, text, command, in_game, can_control, config, send_private)

    if command == "stop" and (can_control or in_game):
        clear_game(game["chat_guid"])
        log.info(f"Imposter stopped in chat {chat_tag(game['chat_guid'])}")
        return [f"Game stopped. {_imposter_reveal(game)}"]
    stage = game["stage"]

    if stage == "clues":
        if command == "skip" and (can_control or in_game):
            skipped = _current(game)["name"]
            game["turn"] += 1
            save_game(game)
            return [f"Skipping {skipped}. {_current(game)['name']}, you're up!"]
        if command is not None:
            return []
        if handle != _current(game)["handle"]:
            return [_imposter_status(game)] if mentions_keanu(text) else []
        if is_turn_question(text):
            return [_imposter_status(game)]
        clue = text.strip()[:MAX_CLUE_CHARS]
        if not clue:
            return []
        game["clues"].append({"name": _current(game)["name"], "text": clue})
        game["turn"] += 1
        if len(game["clues"]) < game["total_turns"]:
            save_game(game)
            return [f"{_current(game)['name']}, you're up! (clue {len(game['clues']) + 1}/{game['total_turns']})"]
        game["stage"] = "voting"
        save_game(game)
        recap = "\n".join(
            f"{p['name']}: " + ", ".join(c["text"] for c in game["clues"] if c["name"] == p["name"])
            for p in game["players"])
        names = ", ".join(p["name"] for p in game["players"])
        return [f"Clues are in!\n\n{recap}\n\nVote! Reply with who you think the imposter is: {names}."]

    if stage == "voting":
        if command == "reveal" and can_control:
            if not game["votes"]:
                return ["No votes yet! Reply with who you think the imposter is."]
            return _tally(game)
        if command is not None or not in_game:
            return [_imposter_status(game)] if command is None and mentions_keanu(text) else []
        if handle in game["votes"]:
            return [_imposter_status(game)] if mentions_keanu(text) else []
        named = _parse_vote(game, text)
        if len(named) != 1:
            return [_imposter_status(game)] if mentions_keanu(text) else []
        if named[0]["handle"] == handle:
            return ["You can't vote for yourself! Who do you think it is?"]
        game["votes"][handle] = named[0]["handle"]
        if len(game["votes"]) == len(game["players"]):
            return _tally(game)
        save_game(game)
        return [f"Got it, {_name_of(game, handle)}! ({len(game['votes'])}/{len(game['players'])} voted)"]

    # guess
    if command is not None or handle != game["imposter"] or is_turn_question(text):
        return [_imposter_status(game)] if command is None and mentions_keanu(text) else []
    clear_game(game["chat_guid"])
    log.info(f"Imposter finished in chat {chat_tag(game['chat_guid'])} (imposter caught)")
    name = _name_of(game, game["imposter"])
    if _normalize_word(text) == _normalize_word(game["word"]):
        return [f"Yes! The word was {game['word'].upper()}, so {name} steals the win!"]
    return [f"Nope! The word was {game['word'].upper()}. Everyone else wins!"]


# ── Entry point ────────────────────────────────────────────────────────────────

def handle_group_game_message(chat_guid: str, handle: str, text: str, is_family: bool,
                              config: dict,
                              send_private: Optional[Callable] = None) -> Optional[List[str]]:
    """
    Returns texts to post in the group, or None if no game claimed the message
    (no game active and it wasn't a game start). send_private(handle, text) -> bool
    sends a private text (Imposter's secret words).
    """
    game = load_game(chat_guid)
    notice = []
    if game and is_expired(game):
        # Announce once (the state is cleared here), then treat the message as
        # if no game were running — it may be a fresh start.
        clear_game(chat_guid)
        log.info(f"Group game in chat {chat_tag(chat_guid)} expired")
        if game.get("phase") == "playing" and game["game"] == "story_chain":
            notice = [f"Our story timed out. Here's how far we got:\n\n{_story_text(game)}\n\n"
                      "Say \"keanu story\" to start a new one."]
        elif game.get("phase") == "playing" and game["game"] == "imposter":
            notice = [f"Our Imposter game timed out. {_imposter_reveal(game)}"]
        game = None
    if game:
        if game["game"] == "story_chain":
            return handle_story_chain(game, handle, text, is_family, config)
        if game["game"] == "imposter":
            return handle_imposter(game, handle, text, is_family, config, send_private)
        log.error(f"Unknown game {game.get('game')!r} in chat {chat_tag(chat_guid)}, clearing")
        clear_game(chat_guid)
        return None
    kind = game_start(text)
    if is_family and kind:
        return notice + start_game(kind, chat_guid, handle, config)
    return notice or None
