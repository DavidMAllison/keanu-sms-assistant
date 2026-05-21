"""Single Claude agent with tool use. Replaces the three separate ask_*_agent functions."""

import json
import logging
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

from agents.menu_agent import load_context
from tools import build_tool_list, execute_tool

log = logging.getLogger(__name__)

_client = anthropic.Anthropic()
_conversation_history: dict[str, list[dict]] = defaultdict(list)
_last_seen: dict[str, float] = {}
MAX_HISTORY = 10
MAX_TOOL_ROUNDS = 5
SESSION_TIMEOUT = 90 * 60  # 90 minutes

_SYSTEM_PROMPT = (Path(__file__).parent / "system_prompts/menu.txt").read_text()

_KID_SYSTEM = """You are Keanu, a friendly koala chatting with a kid over iMessage. Keep it fun and short!
- One or two sentences max
- Playful and encouraging
- Australian English where it fits ("no worries", "heaps fun", "arvo", "mate", etc.)
- No markdown, no bullet points
- You can look up dinner plans and activities — use your tools when asked
- If they say they're bored or want something to do, suggest one specific fun activity (craft, game, or silly challenge) they can do at home — be creative and enthusiastic
- If they want to play "Would You Rather", ask a fun kid-friendly question and react enthusiastically to their answer — keep the game going if they want to continue
- If they ask if a friend can come to dinner, use check_friend_dinner — if parents_notified is true, tell them mum and dad have been messaged and will reply soon; if the schedule is busy or no dinner is planned, explain why not"""


def _build_system(handle: str, config: dict, is_kid: bool) -> str:
    today = date.today().strftime("%A, %B %-d, %Y")
    handle_map = config.get("security", {}).get("handle_to_person", {})
    name = handle_map.get(handle, "there")
    context = load_context()

    if is_kid:
        # Just tonight's dinner line — keep context minimal for kids
        tonight = next((ln for ln in context.splitlines() if ln.strip()), "")
        return f"{_KID_SYSTEM}\n\nToday is {today}. You're chatting with {name}.\n{tonight}"

    pending_note = _pending_friend_request_note()
    return (
        f"{_SYSTEM_PROMPT}\n\n"
        f"Today is {today}. You're chatting with {name}.\n\n"
        f"## Current context\n{context}\n\n"
        + (f"## Pending friend dinner request\n{pending_note}\n\n" if pending_note else "")
        + "Use your tools when you need recipe details, schedule info, or to save/log something. "
        "When you can't do something, call log_capability_gap and say exactly: "
        "\"Can't do that one yet, noted.\""
    )


def _pending_friend_request_note() -> str:
    pending_file = Path(__file__).parent / ".pending_friend_requests.json"
    if not pending_file.exists():
        return ""
    try:
        pending = json.loads(pending_file.read_text())
    except Exception:
        return ""
    if not pending:
        return ""
    # Expire requests older than 12 hours
    from datetime import datetime as _dt
    now = _dt.now()
    active = []
    for p in pending:
        try:
            asked = _dt.fromisoformat(p["asked_at"])
            if (now - asked).total_seconds() < 43200:
                active.append(p)
        except Exception:
            pass
    if not active:
        pending_file.write_text("[]")
        return ""
    lines = []
    for p in active:
        day = p.get("day", "tonight")
        lines.append(
            f"{p['kid_name']} asked if a friend can come to dinner on {day} ({p['meal']}). "
            f"If you're replying yes or no to this, use relay_message with recipient \"{p['kid_name']}\" to pass it on."
        )
    return "\n".join(lines)


def get_reply(handle: str, text: str, config: dict) -> str:
    is_kid = handle in config["security"].get("kids", [])
    is_admin = handle == config["security"].get("menu_admin")
    is_idea_submitter = handle in config["security"].get("idea_submitters", [])

    model = "claude-haiku-4-5-20251001" if is_kid else "claude-sonnet-4-6"
    max_tokens = 250 if is_kid else 500
    tools = build_tool_list(is_kid, is_admin, is_idea_submitter)
    system = _build_system(handle, config, is_kid)

    now = time.time()
    if now - _last_seen.get(handle, 0) > SESSION_TIMEOUT:
        _conversation_history[handle] = []
    _last_seen[handle] = now

    history = _conversation_history[handle]
    now_str = datetime.now().strftime("%A, %B %-d, %Y at %-I:%M %p")
    history.append({"role": "user", "content": f"[{now_str}] {text}"})
    if len(history) > MAX_HISTORY * 2:
        history = history[-(MAX_HISTORY * 2):]
        _conversation_history[handle] = history

    # Filter empty entries before sending to API
    messages = [m for m in history if isinstance(m.get("content"), str) and m["content"].strip()]

    try:
        for _ in range(MAX_TOOL_ROUNDS):
            response = _client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                tools=tools,
                messages=messages,
            )

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result = execute_tool(block.name, block.input, handle, config)
                        log.info(f"Tool {block.name} -> {result[:80]}")
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})
            else:
                reply = next(
                    (b.text for b in response.content if hasattr(b, "text")),
                    "Sorry, something went wrong."
                ).strip()
                history.append({"role": "assistant", "content": reply})
                return reply

        # Exceeded max tool rounds
        reply = "Sorry, I got a bit turned around there. Try again?"
        history.append({"role": "assistant", "content": reply})
        return reply

    except Exception as e:
        log.error(f"Agent error for {handle}: {e}")
        history.pop()  # don't keep the failed user message in history
        if "credit balance is too low" in str(e):
            return "API credits ran out — no point texting until David tops them up!"
        return "Sorry, I ran into an error. Try again in a moment."
