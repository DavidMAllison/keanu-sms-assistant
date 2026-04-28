"""Single Claude agent with tool use. Replaces the three separate ask_*_agent functions."""

import logging
from collections import defaultdict
from datetime import date
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

from agents.menu_agent import load_context
from tools import build_tool_list, execute_tool

log = logging.getLogger(__name__)

_client = anthropic.Anthropic()
_conversation_history: dict[str, list[dict]] = defaultdict(list)
MAX_HISTORY = 10
MAX_TOOL_ROUNDS = 5

_SYSTEM_PROMPT = (Path(__file__).parent / "system_prompts/menu.txt").read_text()

_KID_SYSTEM = """You are Keanu, a friendly koala chatting with a kid over iMessage. Keep it fun and short!
- One or two sentences max
- Playful and encouraging
- British English where it fits ("brilliant", "cheers", etc.)
- No markdown, no bullet points
- You can look up dinner plans and activities — use your tools when asked"""


def _build_system(handle: str, config: dict, is_kid: bool) -> str:
    today = date.today().strftime("%A, %B %-d, %Y")
    handle_map = config.get("security", {}).get("handle_to_person", {})
    name = handle_map.get(handle, "there")
    context = load_context()

    if is_kid:
        # Just tonight's dinner line — keep context minimal for kids
        tonight = next((ln for ln in context.splitlines() if ln.strip()), "")
        return f"{_KID_SYSTEM}\n\nToday is {today}. You're chatting with {name}.\n{tonight}"

    return (
        f"{_SYSTEM_PROMPT}\n\n"
        f"Today is {today}. You're chatting with {name}.\n\n"
        f"## Current context\n{context}\n\n"
        "Use your tools when you need recipe details, schedule info, or to save/log something. "
        "When you can't do something, call log_capability_gap and say exactly: "
        "\"Can't do that one yet, noted.\""
    )


def get_reply(handle: str, text: str, config: dict) -> str:
    is_kid = handle in config["security"].get("kids", [])
    is_admin = handle == config["security"].get("menu_admin")
    is_idea_submitter = handle in config["security"].get("idea_submitters", [])

    model = "claude-haiku-4-5-20251001" if is_kid else "claude-sonnet-4-6"
    max_tokens = 250 if is_kid else 500
    tools = build_tool_list(is_kid, is_admin, is_idea_submitter)
    system = _build_system(handle, config, is_kid)

    history = _conversation_history[handle]
    history.append({"role": "user", "content": text})
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
        return "Sorry, I ran into an error. Try again in a moment."
