"""
Group chat games: chat-scoped SMS Activities, persisted to disk so a restart
doesn't end a game. One active game per group chat.

A family member starts every game. Story Chain, Imposter and Balderdash share
one lobby: players reply "in", and "keanu go" begins. Emoji Guess and Hangman
start right away and anyone in the chat can guess.

Story Chain — players take turns adding a sentence to a story Keanu opens and
closes. No hidden information, so everything happens in the group thread.

Imposter — Keanu privately texts every player a secret word except one, the
imposter, who only gets the category. Players give clues in turn, vote on who
the imposter is, and a caught imposter gets one guess at the word. Private
texts go only to that game's players, only at the start, and only carry the
word or the role. The word and the imposter are never logged.

Emoji Guess — Keanu posts a movie or book as emojis; first right answer scores.
Answers are checked against hand-written titles, no model call.

Hangman — one shared word; text a letter or "guess <word>". The word isn't logged.

Balderdash — Keanu posts a real but strange word and privately asks each
player for a fake definition. Those come back as 1:1 texts, which server.py
hands to handle_private_game_message before its allowed-handle check (friends
play too); a captured text is only stored as a definition. Then everyone votes
in the group for the real one.

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

import difflib

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
_EMOJI_START = re.compile(r"\bemojis?\b", re.IGNORECASE)
_HANGMAN_START = re.compile(r"\bhang\s?man\b", re.IGNORECASE)
_BALDERDASH_START = re.compile(r"\b(balderdash|fake definitions?)\b", re.IGNORECASE)
_COMMAND = re.compile(r"^\s*@?keanu[\s,:]+(go|start|stop|end|quit|cancel|skip|reveal|hint)\W*$", re.IGNORECASE)
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
    if _BALDERDASH_START.search(text):
        return "balderdash"
    if _HANGMAN_START.search(text):
        return "hangman"
    if _EMOJI_START.search(text):
        return "emoji"
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
    if word in ("skip", "reveal", "hint"):
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


_TITLES = {"story_chain": "Story Chain", "imposter": "Imposter", "emoji": "Emoji Guess",
           "hangman": "Hangman", "balderdash": "Balderdash"}
_MIN_PLAYERS = {"story_chain": 2, "imposter": 3, "balderdash": 3}
_JOIN_HELP = ("Everyone else, reply \"in\" to join "
              "(friends: \"in\" plus your name, like \"in Sam\").")
_INTROS = {
    "story_chain": "Story Chain! We'll build a story one sentence at a time.",
    "imposter": ("Imposter! I'll text everyone a secret word, except one sneaky imposter. "
                 "Give clues, then vote out the imposter. Needs 3 or more players."),
    "balderdash": ("Balderdash! I'll post a strange real word, everyone texts me a fake "
                   "definition privately, then you vote for the real one. Needs 3 or more players."),
}


def start_game(kind: str, chat_guid: str, handle: str, config: dict) -> List[str]:
    # Emoji Guess and Hangman have no lobby: anyone in the chat can guess
    if kind == "emoji":
        return _emoji_start(chat_guid, handle, config)
    if kind == "hangman":
        return _hangman_start(chat_guid, handle, config)
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
        if kind == "balderdash":
            return _balderdash_go(game, send_private)
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


# ── Shared scoring (Emoji Guess, Hangman, Balderdash) ──────────────────────────

def _ensure_player(game: dict, handle: str, config: dict) -> str:
    """Adds someone who plays without joining (Emoji Guess, Hangman); returns their name."""
    for p in game["players"]:
        if p["handle"] == handle:
            return p["name"]
    name = _player_name(handle, "", game, config)
    game["players"].append({"handle": handle, "name": name})
    return name


def _mid_game_join(game: dict, handle: str, text: str, config: dict) -> List[str]:
    """A friend naming themselves ("in Sam") during a no-lobby game, so points show a name."""
    given = parse_join(text)
    if not given or any(p["handle"] == handle for p in game["players"]):
        return []
    name = _player_name(handle, given, game, config)
    game["players"].append({"handle": handle, "name": name})
    save_game(game)
    return [f"{name}'s in!"]


def _scoreboard(game: dict) -> str:
    scores = game["scores"]
    ranked = sorted(game["players"], key=lambda p: -scores.get(p["handle"], 0))
    return ", ".join(f"{p['name']} {scores.get(p['handle'], 0)}" for p in ranked)


def _winner_line(game: dict) -> str:
    scores = game["scores"]
    top = max(scores.values(), default=0)
    if top == 0:
        return "Nobody scored this time!"
    winners = [p["name"] for p in game["players"] if scores.get(p["handle"], 0) == top]
    if len(winners) == 1:
        return f"{winners[0]} wins!"
    return f"It's a tie between {' and '.join(winners)}!"


# ── Emoji Guess ────────────────────────────────────────────────────────────────

EMOJI_ROUNDS = 5

# (emojis, title, other accepted answers). Hand-written: generated emoji
# puzzles are often unguessable, and the answer check needs exact titles.
_EMOJI_PUZZLES = (
    ("🦁👑", "The Lion King", ()),
    ("❄️👸⛄", "Frozen", ()),
    ("🐠🔍", "Finding Nemo", ("Nemo",)),
    ("🐟🔍💙", "Finding Dory", ("Dory",)),
    ("🧸🤠🚀", "Toy Story", ()),
    ("🎈🏠👴", "Up", ()),
    ("🤖🌱🗑️", "WALL-E", ("Walle",)),
    ("🐀👨‍🍳🇫🇷", "Ratatouille", ()),
    ("🏎️⚡", "Cars", ()),
    ("👹👁️🚪", "Monsters, Inc.", ("Monsters Inc", "Monsters Incorporated")),
    ("🦸‍♂️👨‍👩‍👧‍👦", "The Incredibles", ()),
    ("😢😡😱🤢😄🧠", "Inside Out", ()),
    ("💀🎸🌼", "Coco", ()),
    ("🌊🛶🐓", "Moana", ()),
    ("🌹🏰👹", "Beauty and the Beast", ()),
    ("🧜‍♀️🐚", "The Little Mermaid", ()),
    ("🐘👂🎪", "Dumbo", ()),
    ("🧞‍♂️🪔🐒", "Aladdin", ()),
    ("👸🍎🧙‍♀️", "Snow White", ("Snow White and the Seven Dwarfs",)),
    ("👠🎃🕛", "Cinderella", ()),
    ("👸💤🌹", "Sleeping Beauty", ()),
    ("🕷️👨", "Spider-Man", ("Spiderman",)),
    ("🦇👨", "Batman", ()),
    ("🐼🥋", "Kung Fu Panda", ()),
    ("🐉🧒📖", "How to Train Your Dragon", ("Train Your Dragon",)),
    ("🟢👹🧅", "Shrek", ()),
    ("🐧💃🎶", "Happy Feet", ()),
    ("🦥🐘🧊", "Ice Age", ()),
    ("🦹‍♂️🌙👧👧👧", "Despicable Me", ()),
    ("🏝️🤖🪿", "The Wild Robot", ()),
    ("🐻🍯🎈", "Winnie the Pooh", ("Pooh",)),
    ("🐻🧥🍊", "Paddington", ()),
    ("🧙‍♂️⚡👓", "Harry Potter", ()),
    ("🍫🏭🎫", "Charlie and the Chocolate Factory", ("Willy Wonka", "Wonka")),
    ("🍑🐛🐞", "James and the Giant Peach", ()),
    ("🕷️🐷🕸️", "Charlotte's Web", ()),
    ("🦁🧙‍♀️🚪❄️", "The Lion, the Witch and the Wardrobe", ("Narnia",)),
    ("🐛🍎🍐🍓", "The Very Hungry Caterpillar", ("Hungry Caterpillar",)),
    ("🐱🎩", "The Cat in the Hat", ()),
    ("🟢🥚🍖", "Green Eggs and Ham", ()),
    ("🎄💚😠", "The Grinch", ("How the Grinch Stole Christmas", "Grinch")),
    ("👦🌳🍎", "The Giving Tree", ()),
    ("🌙🐰🛏️", "Goodnight Moon", ()),
    ("🐺👵🔴", "Little Red Riding Hood", ("Red Riding Hood",)),
    ("🐷🐷🐷🐺", "The Three Little Pigs", ()),
    ("👧🐻🐻🐻🥣", "Goldilocks and the Three Bears", ("Goldilocks",)),
    ("🌪️🏠👠🌈", "The Wizard of Oz", ()),
    ("⚔️✨🌌", "Star Wars", ()),
    ("🦖🏝️🚙", "Jurassic Park", ()),
    ("👻🚫", "Ghostbusters", ()),
    ("🧒✈️🏠🎄", "Home Alone", ()),
    ("🍝🐕🐕", "Lady and the Tramp", ()),
    ("🐻🐯🐒👦🌴", "The Jungle Book", ()),
    ("🍄👨‍🔧", "The Super Mario Bros. Movie", ("Super Mario", "Mario", "Super Mario Bros")),
    ("🦊🐰🚓", "Zootopia", ()),
    ("🏹👧🐻", "Brave", ()),
    ("🌺⚔️🐉", "Mulan", ()),
    ("👸💇‍♀️🗼", "Tangled", ("Rapunzel",)),
    ("🐼🔴👧", "Turning Red", ()),
    ("🏠🦋🕯️👨‍👩‍👧‍👦", "Encanto", ()),
    ("🧌🎶🌈", "Trolls", ()),
    ("🐢🥷🍕", "Teenage Mutant Ninja Turtles", ("Ninja Turtles", "TMNT")),
    ("🐶🐱🏙️🤫", "The Secret Life of Pets", ()),
    ("🚂❄️🎅", "The Polar Express", ()),
    ("🐢🐇🏁", "The Tortoise and the Hare", ()),
    ("🦆😢🦢", "The Ugly Duckling", ()),
    ("🐟🌈✨", "The Rainbow Fish", ()),
    ("🐶👮📖", "Dog Man", ()),
    ("📓😩👦", "Diary of a Wimpy Kid", ("Wimpy Kid",)),
    ("⛏️🟩🧱", "A Minecraft Movie", ("Minecraft", "Minecraft Movie")),
)
_EMOJI_BY_TITLE = {title: (emojis, title, aliases) for emojis, title, aliases in _EMOJI_PUZZLES}


def _normalize_title(text: str) -> str:
    t = text.lower().replace("&", " and ").replace("-", " ")
    t = re.sub(r"[^a-z0-9 ]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return re.sub(r"^((is it|it is|its|i think|i guess|maybe|the|a|an)\s+)+", "", t).strip()


def _is_title_guess(text: str, answers) -> bool:
    guess = _normalize_title(_MENTION.sub("", text))
    if len(guess) < 2:
        return False
    for answer in answers:
        a = _normalize_title(answer)
        if guess == a:
            return True
        # Small typos ("ratatoullie"), but not for short titles like "Up"
        if len(a) >= 5 and difflib.SequenceMatcher(None, guess, a).ratio() >= 0.85:
            return True
        # The whole title inside a short message ("omg its frozen!!")
        if (len(a) >= 4 and re.search(r"\b" + re.escape(a) + r"\b", guess)
                and len(guess.split()) <= len(a.split()) + 3):
            return True
    return False


def _used_puzzles_path(chat_guid: str) -> Path:
    path = _state_path(chat_guid)
    return path.with_name(path.stem + "_emoji_used.txt")


def _pick_puzzles(chat_guid: str) -> List[str]:
    """Titles this chat hasn't seen yet; starts over once they run low."""
    path = _used_puzzles_path(chat_guid)
    try:
        used = set(path.read_text().splitlines())
    except FileNotFoundError:
        used = set()
    fresh = [title for title in _EMOJI_BY_TITLE if title not in used]
    if len(fresh) < EMOJI_ROUNDS:
        used, fresh = set(), list(_EMOJI_BY_TITLE)
    picks = random.sample(fresh, EMOJI_ROUNDS)
    GAME_STATE_DIR.mkdir(exist_ok=True)
    path.write_text("\n".join(sorted(used | set(picks))))
    return picks


def _emoji_puzzle(game: dict) -> tuple:
    return _EMOJI_BY_TITLE[game["puzzles"][game["round"]]]


def _emoji_prompt(game: dict) -> str:
    return f"Round {game['round'] + 1}/{len(game['puzzles'])}: {_emoji_puzzle(game)[0]}"


def _emoji_start(chat_guid: str, handle: str, config: dict) -> List[str]:
    game = {
        "game": "emoji", "phase": "playing", "chat_guid": chat_guid, "starter": handle,
        "players": [], "scores": {}, "puzzles": _pick_puzzles(chat_guid), "round": 0,
        "hints": 0, "created": time.time(),
    }
    game["players"].append({"handle": handle, "name": _player_name(handle, "", game, config)})
    save_game(game)
    log.info(f"Emoji Guess started in chat {chat_tag(chat_guid)}")
    return [f"Emoji Guess! Name the movie or book. First right answer gets the point. "
            f"{EMOJI_ROUNDS} rounds. Commands: keanu hint, keanu skip, keanu stop.\n\n{_emoji_prompt(game)}"]


def _emoji_next(game: dict, lead: str) -> List[str]:
    game["round"] += 1
    game["hints"] = 0
    if game["round"] >= len(game["puzzles"]):
        clear_game(game["chat_guid"])
        log.info(f"Emoji Guess finished in chat {chat_tag(game['chat_guid'])}")
        return [f"{lead}\n\nThat's the game! Scores: {_scoreboard(game)}. {_winner_line(game)}"]
    save_game(game)
    return [f"{lead}\n\n{_emoji_prompt(game)}"]


def _emoji_hint(game: dict) -> str:
    title = _emoji_puzzle(game)[1]
    words = title.split()
    game["hints"] += 1
    save_game(game)
    if game["hints"] == 1:
        plural = "s" if len(words) != 1 else ""
        return f"Hint: {len(words)} word{plural}, starting with {title[0].upper()}."
    if game["hints"] == 2:
        return "Hint: " + " ".join(w[0] + re.sub(r"[A-Za-z0-9]", "_", w[1:]) for w in words)
    return "No more hints! Say \"keanu skip\" to see the answer."


def handle_emoji(game: dict, handle: str, text: str, is_family: bool, config: dict) -> List[str]:
    command = parse_command(text)
    can_control = is_family or handle == game["starter"]
    _, title, aliases = _emoji_puzzle(game)

    if command == "stop" and can_control:
        clear_game(game["chat_guid"])
        log.info(f"Emoji Guess stopped in chat {chat_tag(game['chat_guid'])}")
        return [f"Game stopped. The answer was {title}. Scores: {_scoreboard(game)}."]
    if command == "skip" and can_control:
        return _emoji_next(game, f"Skipped! It was {title}.")
    if command == "hint":
        return [_emoji_hint(game)]
    if command is not None:
        return []
    if _is_title_guess(text, (title,) + tuple(aliases)):
        name = _ensure_player(game, handle, config)
        game["scores"][handle] = game["scores"].get(handle, 0) + 1
        return _emoji_next(game, f"Yes! {name} got it: {title}.")
    joined = _mid_game_join(game, handle, text, config)
    if joined:
        return joined
    # Wrong guesses stay quiet so the chat isn't flooded
    if mentions_keanu(text):
        return [f"{_emoji_prompt(game)}\nName the movie or book! (keanu hint, keanu skip)"]
    return []


# ── Hangman ────────────────────────────────────────────────────────────────────

HANGMAN_LIVES = 6

# Kid level: friends may be playing too
_HANGMAN_WORDS = (
    "giraffe", "penguin", "octopus", "kangaroo", "hamster", "dolphin", "elephant", "cheetah",
    "pancake", "popcorn", "spaghetti", "burrito", "sandwich", "pumpkin", "blueberry", "pineapple",
    "volcano", "rainbow", "thunder", "blizzard", "tornado", "library", "airport", "playground",
    "backpack", "notebook", "scissors", "crayon", "umbrella", "blanket", "pillow", "bathtub",
    "toaster", "rocket", "planet", "astronaut", "galaxy", "dinosaur", "dragon", "unicorn",
    "wizard", "castle", "pirate", "treasure", "skeleton", "mermaid", "jellyfish", "starfish",
    "seahorse", "lobster", "hedgehog", "squirrel", "raccoon", "flamingo", "peacock", "kitten",
    "basketball", "trampoline", "skateboard", "bicycle", "scooter", "snowman", "sunflower",
    "cactus", "mushroom", "campfire", "lantern", "compass", "telescope", "keyboard", "guitar",
    "trumpet", "violin", "cupcake", "cookie", "muffin", "waffle", "pretzel", "avocado",
    "broccoli", "carrot", "zipper", "pajamas", "sneakers", "mittens", "igloo", "island",
    "jungle", "desert", "canyon", "waterfall", "lighthouse", "submarine", "helicopter",
    "tractor", "firework", "birthday", "vacation", "monster", "robot", "butterfly", "ladybug",
)

_LETTER = re.compile(r"^\s*(?:letter\s+)?([a-z])\s*[!.?]*\s*$", re.IGNORECASE)
_WORD_GUESS = re.compile(r"^\s*(?:guess|is it)[\s:]+([a-z]+)\W*$", re.IGNORECASE)


def _hangman_wrong_letters(game: dict) -> List[str]:
    return [c for c in game["guessed"] if c not in game["word"]]


def _hangman_lives(game: dict) -> int:
    return HANGMAN_LIVES - len(_hangman_wrong_letters(game)) - len(game["wrong_words"])


def _hangman_board(game: dict) -> str:
    lines = [" ".join(c.upper() if c in game["guessed"] else "_" for c in game["word"]),
             f"Lives: {_hangman_lives(game)}/{HANGMAN_LIVES}"]
    misses = [c.upper() for c in _hangman_wrong_letters(game)] + [w.upper() for w in game["wrong_words"]]
    if misses:
        lines.append("Wrong: " + ", ".join(misses))
    return "\n".join(lines)


def _hangman_start(chat_guid: str, handle: str, config: dict) -> List[str]:
    game = {
        "game": "hangman", "phase": "playing", "chat_guid": chat_guid, "starter": handle,
        "players": [], "word": random.choice(_HANGMAN_WORDS), "guessed": [], "wrong_words": [],
        "created": time.time(),
    }
    game["players"].append({"handle": handle, "name": _player_name(handle, "", game, config)})
    save_game(game)
    log.info(f"Hangman started in chat {chat_tag(chat_guid)}")
    return [f"Hangman! Text just a letter to guess it, or \"guess <word>\" if you know it. "
            f"{HANGMAN_LIVES} misses and I win. \"keanu stop\" ends it.\n\n{_hangman_board(game)}"]


def _hangman_end(game: dict, handle: Optional[str], config: dict) -> List[str]:
    clear_game(game["chat_guid"])
    word = game["word"].upper()
    if handle is None:
        log.info(f"Hangman finished in chat {chat_tag(game['chat_guid'])} (out of lives)")
        return [f"Out of lives! The word was {word}. I win this one!"]
    log.info(f"Hangman finished in chat {chat_tag(game['chat_guid'])} (solved)")
    name = _ensure_player(game, handle, config)
    lives = _hangman_lives(game)
    return [f"{name} got it! The word was {word}, with {lives} {'life' if lives == 1 else 'lives'} to spare."]


def handle_hangman(game: dict, handle: str, text: str, is_family: bool, config: dict) -> List[str]:
    command = parse_command(text)
    can_control = is_family or handle == game["starter"]
    word = game["word"]

    if command == "stop" and can_control:
        clear_game(game["chat_guid"])
        log.info(f"Hangman stopped in chat {chat_tag(game['chat_guid'])}")
        return [f"Game stopped. The word was {word.upper()}."]
    if command is not None:
        return []

    body = _MENTION.sub("", text).strip(" \t\n,:")
    m = _LETTER.match(body)
    if m:
        letter = m.group(1).lower()
        if letter in game["guessed"]:
            return [f"{letter.upper()} was already guessed!\n\n{_hangman_board(game)}"]
        game["guessed"].append(letter)
        if all(c in game["guessed"] for c in word):
            return _hangman_end(game, handle, config)
        if _hangman_lives(game) <= 0:
            return _hangman_end(game, None, config)
        save_game(game)
        verdict = f"Yes, there's {letter.upper()}!" if letter in word else f"No {letter.upper()}!"
        return [f"{verdict}\n\n{_hangman_board(game)}"]

    m = _WORD_GUESS.match(body)
    guess = m.group(1).lower() if m else None
    if guess is None and body.lower().strip(" !?.") == word:
        guess = word  # just texting the word counts; any other chatter doesn't
    if guess:
        if guess == word:
            return _hangman_end(game, handle, config)
        if guess in game["wrong_words"]:
            return [f"{guess.upper()} was already guessed!\n\n{_hangman_board(game)}"]
        game["wrong_words"].append(guess)
        if _hangman_lives(game) <= 0:
            return _hangman_end(game, None, config)
        save_game(game)
        return [f"Not {guess.upper()}!\n\n{_hangman_board(game)}"]

    joined = _mid_game_join(game, handle, text, config)
    if joined:
        return joined
    if mentions_keanu(text):
        return [f"{_hangman_board(game)}\n\nText a letter, or \"guess <word>\"."]
    return []


# ── Balderdash ─────────────────────────────────────────────────────────────────

BALDERDASH_ROUNDS = 3
MAX_DEFINITION_CHARS = 150

# Hand-picked real words: a wrong "real" definition would break the game
_BALDERDASH_WORDS = (
    ("snollygoster", "A clever but dishonest person"),
    ("collywobbles", "A nervous, fluttery feeling in your stomach"),
    ("gardyloo", "A warning shout before throwing dirty water out of a window"),
    ("widdershins", "In the opposite direction to the sun; counterclockwise"),
    ("bumbershoot", "An umbrella"),
    ("borborygmus", "A rumbling, gurgling noise in your stomach"),
    ("gongoozler", "Someone who enjoys watching boats on canals"),
    ("flibbertigibbet", "A silly, flighty person who talks too much"),
    ("snickersnee", "A large knife used for fighting"),
    ("defenestration", "Throwing someone or something out of a window"),
    ("hornswoggle", "To trick or cheat someone"),
    ("absquatulate", "To leave suddenly; to run off"),
    ("taradiddle", "A small lie or fib"),
    ("erinaceous", "Like a hedgehog"),
    ("pandiculation", "Stretching and yawning, like when you wake up"),
    ("ultracrepidarian", "Someone who gives opinions on things they know nothing about"),
    ("zarf", "A holder for a coffee cup that has no handle"),
    ("aglet", "The plastic or metal tip on the end of a shoelace"),
    ("ferrule", "The metal ring that holds the eraser on a pencil"),
    ("lunula", "The white half-moon shape at the bottom of a fingernail"),
    ("philtrum", "The little groove between your nose and your upper lip"),
    ("glabella", "The smooth space between your eyebrows"),
    ("tittle", "The dot over a lowercase i or j"),
    ("muntin", "A strip that separates the panes of glass in a window"),
    ("grawlix", "Symbols like @#$%! that stand for swear words in comics"),
    ("crepuscular", "Active at twilight, around dawn and dusk"),
    ("petrichor", "The earthy smell when rain falls on dry ground"),
    ("susurrus", "A soft whispering or rustling sound"),
    ("brannigan", "A noisy fight or brawl"),
    ("mumpsimus", "Someone who keeps doing something the wrong way even after being corrected"),
    ("bloviate", "To talk on and on in a pompous, empty way"),
    ("nudiustertian", "Having to do with the day before yesterday"),
    ("groak", "To stare at people eating, hoping they'll share"),
    ("jentacular", "Having to do with breakfast"),
    ("yex", "To hiccup"),
    ("lethologica", "Not being able to remember the word you want to say"),
    ("apricity", "The warmth of the sun on a winter day"),
    ("mondegreen", "A misheard song lyric or phrase"),
    ("snood", "The fleshy flap that hangs over a turkey's beak"),
    ("flews", "The droopy upper lips of a dog like a bloodhound"),
    ("dewlap", "Loose skin hanging under the throat of an animal, like a cow"),
    ("ort", "A scrap of food left over after a meal"),
    ("bindle", "A bundle of belongings tied in cloth and carried on a stick"),
    ("oxter", "An armpit"),
    ("ninnyhammer", "A foolish person"),
    ("bumfuzzle", "To confuse someone"),
    ("winklepicker", "A shoe with a long, sharply pointed toe"),
    ("crwth", "An old Welsh stringed instrument played with a bow"),
    ("sesquipedalian", "Using lots of very long words"),
    ("quidnunc", "A nosy person who always wants the latest gossip"),
)


def _tidy_definition(text: str) -> str:
    """Same shape as the real ones, so punctuation doesn't give a fake away."""
    t = " ".join(text.split())[:MAX_DEFINITION_CHARS].rstrip(" .")
    return t[:1].upper() + t[1:]


def _balderdash_deal(game: dict, send_private: Optional[Callable]) -> List[str]:
    """Picks the round's word and asks every player privately for a fake definition."""
    fresh = [w for w in _BALDERDASH_WORDS if w[0] not in game["used"]] or list(_BALDERDASH_WORDS)
    word, definition = random.choice(fresh)
    game["used"].append(word)
    game.update({"stage": "writing", "word": word, "definition": definition,
                 "fakes": {}, "options": [], "votes": {}})
    n = game["round"] + 1
    failed = []
    for p in game["players"]:
        msg = (f"Balderdash round {n}: what does {word.upper()} mean? Make up a definition that "
               "sounds real and text it to me right here, not in the group.")
        if send_private is None or not send_private(p["handle"], msg):
            failed.append(p["name"])
    if failed:
        clear_game(game["chat_guid"])
        log.warning(f"Balderdash stopped in chat {chat_tag(game['chat_guid'])}: "
                    f"private text failed for {len(failed)} player(s)")
        scores = f" Scores: {_scoreboard(game)}." if game["round"] else ""
        return [f"I couldn't text {' and '.join(failed)} privately, so Balderdash is stopped.{scores}"]
    save_game(game)
    log.info(f"Balderdash round {n} dealt in chat {chat_tag(game['chat_guid'])}")
    return [f"Round {n}/{BALDERDASH_ROUNDS}: what does {word.upper()} mean? Check your texts from "
            "Keanu and reply there with a fake definition. (\"keanu reveal\" moves on without "
            "anyone who's stuck.)"]


def _balderdash_go(game: dict, send_private: Optional[Callable]) -> List[str]:
    game.update({"phase": "playing", "round": 0, "scores": {}, "used": []})
    return _balderdash_deal(game, send_private)


def _balderdash_vote_prompt(game: dict) -> List[str]:
    options = [{"text": text, "author": h} for h, text in game["fakes"].items()]
    options.append({"text": game["definition"], "author": None})
    random.shuffle(options)
    game.update({"stage": "voting", "options": options})
    save_game(game)
    listing = "\n".join(f"{i}. {o['text']}" for i, o in enumerate(options, 1))
    return [f"Definitions are in! What does {game['word'].upper()} mean?\n\n{listing}\n\n"
            "Vote by replying with a number (not your own!)."]


def _balderdash_status(game: dict) -> str:
    if game["stage"] == "writing":
        waiting = [p["name"] for p in game["players"] if p["handle"] not in game["fakes"]]
        return f"Waiting on fake definitions from {', '.join(waiting)}. Text them to me privately!"
    waiting = [p["name"] for p in game["players"] if p["handle"] not in game["votes"]]
    return f"Waiting on votes from {', '.join(waiting)}. Reply with a number."


def _balderdash_score(game: dict, send_private: Optional[Callable]) -> List[str]:
    options = game["options"]
    scores = game["scores"]
    real = next(i for i, o in enumerate(options) if o["author"] is None)
    right = []
    fooled: dict = {}
    for voter, idx in game["votes"].items():
        author = options[idx]["author"]
        if author is None:
            scores[voter] = scores.get(voter, 0) + 2
            right.append(_name_of(game, voter))
        else:
            scores[author] = scores.get(author, 0) + 1
            fooled.setdefault(author, []).append(_name_of(game, voter))
    lines = [f"{game['word'].upper()} really means: {game['definition']} (number {real + 1})."]
    if right:
        lines.append(f"Found it (+2): {', '.join(right)}.")
    for author, voters in fooled.items():
        lines.append(f"{_name_of(game, author)}'s fake fooled {', '.join(voters)} (+{len(voters)}).")
    result = "\n".join(lines) + f"\n\nScores: {_scoreboard(game)}"
    game["round"] += 1
    if game["round"] >= BALDERDASH_ROUNDS:
        clear_game(game["chat_guid"])
        log.info(f"Balderdash finished in chat {chat_tag(game['chat_guid'])}")
        return [f"{result}\n\nThat's the game! {_winner_line(game)}"]
    return [result] + _balderdash_deal(game, send_private)


def handle_balderdash(game: dict, handle: str, text: str, is_family: bool, config: dict,
                      send_private: Optional[Callable]) -> List[str]:
    command = parse_command(text)
    in_game = any(p["handle"] == handle for p in game["players"])
    can_control = is_family or handle == game["starter"]

    if game["phase"] == "joining":
        return _handle_lobby(game, handle, text, command, in_game, can_control, config, send_private)

    if command == "stop" and (can_control or in_game):
        clear_game(game["chat_guid"])
        log.info(f"Balderdash stopped in chat {chat_tag(game['chat_guid'])}")
        return [f"Game stopped. {game['word'].upper()} means: {game['definition']}. "
                f"Scores: {_scoreboard(game)}."]

    if game["stage"] == "writing":
        if command == "reveal" and can_control:
            if not game["fakes"]:
                return ["No fake definitions yet! Text them to me privately."]
            return _balderdash_vote_prompt(game)
        return [_balderdash_status(game)] if command is None and mentions_keanu(text) else []

    # voting
    if command == "reveal" and can_control:
        if not game["votes"]:
            return ["No votes yet! Reply with a number."]
        return _balderdash_score(game, send_private)
    if command is not None or not in_game or handle in game["votes"]:
        return [_balderdash_status(game)] if command is None and mentions_keanu(text) else []
    m = re.match(r"^\s*#?(\d{1,2})\W*$", text)
    if not m:
        return [_balderdash_status(game)] if mentions_keanu(text) else []
    choice = int(m.group(1)) - 1
    if not 0 <= choice < len(game["options"]):
        return [f"Pick a number from 1 to {len(game['options'])}."]
    if game["options"][choice]["author"] == handle:
        return ["That's your own! Pick another."]
    game["votes"][handle] = choice
    if len(game["votes"]) == len(game["players"]):
        return _balderdash_score(game, send_private)
    save_game(game)
    return [f"Got it, {_name_of(game, handle)}! ({len(game['votes'])}/{len(game['players'])} voted)"]


def handle_private_game_message(handle: str, text: str) -> Optional[tuple]:
    """
    A 1:1 text from a Balderdash player who still owes a definition. Returns
    (chat_guid, private_reply, group_replies) when captured, else None so
    normal routing (or the unauthorized-handle rejection) carries on. The text
    is only ever stored as a definition; it never reaches the agent.
    """
    if not text.strip():
        return None
    waiting = []
    for path in GAME_STATE_DIR.glob("*.json"):
        try:
            game = json.loads(path.read_text())
        except Exception:
            continue
        if (isinstance(game, dict) and game.get("game") == "balderdash"
                and game.get("stage") == "writing" and not is_expired(game)
                and handle not in game["fakes"]
                and any(p["handle"] == handle for p in game["players"])):
            waiting.append(game)
    if not waiting:
        return None
    game = max(waiting, key=lambda g: g.get("updated", 0))
    game["fakes"][handle] = _tidy_definition(text)
    log.info(f"Balderdash definition received in chat {chat_tag(game['chat_guid'])} "
             f"({len(game['fakes'])}/{len(game['players'])})")
    if len(game["fakes"]) == len(game["players"]):
        return game["chat_guid"], "Got it! Voting's starting in the group.", _balderdash_vote_prompt(game)
    save_game(game)
    return game["chat_guid"], "Got it! Head back to the group chat.", []


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
        elif game["game"] == "emoji":
            notice = [f"Our Emoji Guess game timed out. Scores: {_scoreboard(game)}."]
        elif game["game"] == "hangman":
            notice = [f"Our Hangman game timed out. The word was {game['word'].upper()}."]
        elif game.get("phase") == "playing" and game["game"] == "balderdash":
            notice = [f"Our Balderdash game timed out. Scores: {_scoreboard(game)}."]
        game = None
    if game:
        if game["game"] == "story_chain":
            return handle_story_chain(game, handle, text, is_family, config)
        if game["game"] == "imposter":
            return handle_imposter(game, handle, text, is_family, config, send_private)
        if game["game"] == "emoji":
            return handle_emoji(game, handle, text, is_family, config)
        if game["game"] == "hangman":
            return handle_hangman(game, handle, text, is_family, config)
        if game["game"] == "balderdash":
            return handle_balderdash(game, handle, text, is_family, config, send_private)
        log.error(f"Unknown game {game.get('game')!r} in chat {chat_tag(chat_guid)}, clearing")
        clear_game(chat_guid)
        return None
    kind = game_start(text)
    if is_family and kind:
        return notice + start_game(kind, chat_guid, handle, config)
    return notice or None
