"""
menu_workflow.py — SMS-triggered weekly menu workflow, agent-driven.

State lives in /Users/Shared/cooking/menu_session.json.
"start menu" from admin triggers handle_start(). Subsequent messages
from admin route to menu_agent_reply(), which uses Claude Sonnet with
tool use to drive the conversation naturally.
"""

import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional


log = logging.getLogger(__name__)

DRY_RUN = False

MENU_SESSION_FILE = Path("/Users/Shared/cooking/menu_session.json")
OUTBOX_FILE = Path("/Users/Shared/sms-assistant/.outbox.json")
MENU_PENDING_FILE = Path("/Users/Shared/sms-assistant/menu_feedback_pending.json")

DAYS_ORDER = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

DAY_NAME_MAP = {
    "monday": "Mon", "tuesday": "Tue", "wednesday": "Wed", "thursday": "Thu",
    "friday": "Fri", "saturday": "Sat", "sunday": "Sun",
    "mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu",
    "fri": "Fri", "sat": "Sat", "sun": "Sun",
}

_MENU_SYSTEM = (Path(__file__).parent.parent / "system_prompts/menu.txt").read_text()


# ── Session I/O ───────────────────────────────────────────────────────────────

def _load_session() -> dict:
    if MENU_SESSION_FILE.exists():
        try:
            return json.loads(MENU_SESSION_FILE.read_text())
        except Exception:
            pass
    return {"state": "idle"}


def _save_session(session: dict):
    if DRY_RUN:
        log.info(f"[DRY_RUN] Would write session state={session.get('state')}")
        return
    MENU_SESSION_FILE.write_text(json.dumps(session, indent=2))


# ── MenuBuilder MCP bridge ────────────────────────────────────────────────────

from menubuilder_bridge import call_menubuilder_tool  # noqa: E402


def _sync_session_state(state: str):
    """Write just the state to menu_session.json so server.py routing stays current."""
    if DRY_RUN:
        log.info(f"[DRY_RUN] Would sync session state={state}")
        return
    try:
        existing = json.loads(MENU_SESSION_FILE.read_text()) if MENU_SESSION_FILE.exists() else {}
        existing["state"] = state
        MENU_SESSION_FILE.write_text(json.dumps(existing, indent=2))
    except Exception as e:
        log.error(f"Could not sync session state: {e}")


def _send_outbox(handle: str, text: str):
    if DRY_RUN:
        log.info(f"[DRY_RUN] Would send to {handle}: {text[:80]}")
        return
    outbox = json.loads(OUTBOX_FILE.read_text()) if OUTBOX_FILE.exists() else []
    outbox.append({"handle": handle, "text": text})
    OUTBOX_FILE.write_text(json.dumps(outbox))


def _send_to_ashley(text: str, handle: str):
    """Send a message to Ashley via outbox and recreate PENDING_FILE so her reply is routed back."""
    if not handle:
        log.error("No partner_handle in settings — cannot send to Ashley.")
        return
    _send_outbox(handle, text)
    if not DRY_RUN:
        MENU_PENDING_FILE.write_text(json.dumps({
            "sent_at": datetime.now().isoformat(),
            "partner_handle": handle,
        }))


def _format_meal_list(meals: list) -> str:
    lines = []
    for m in meals:
        fb = f" — {m['sms_feedback']}" if m.get("sms_feedback") else ""
        lines.append(f"{m['day']}: {m['name']}{fb}")
    return "\n".join(lines)





def _format_numbered_list(selected: dict, week_start: date, quick_days: Optional[list] = None) -> str:
    """Format selected meals as compact Sun-Sat list for SMS approval."""
    ordered = [(day, selected[day]) for day in DAYS_ORDER if day in selected]
    lines = [f"{day}: {name}" for day, name in ordered]
    return "\n".join(lines)


# ── handle_finalize ───────────────────────────────────────────────────────────

def handle_finalize(session: dict, config: dict):
    """Delegates fully to MenuBuilder's finalize_plan MCP tool."""
    result = call_menubuilder_tool("finalize_plan")
    admin_handle = config["security"].get("menu_admin")
    if result.get("state") == "complete":
        if admin_handle:
            _send_outbox(admin_handle, "Plan ready — apps launched.")
            prep = result.get("prep_guide", "")
            if prep:
                _send_outbox(admin_handle, prep)
        session["state"] = "complete"
        _save_session(session)
        log.info("Menu workflow complete.")
    else:
        log.error(f"finalize_plan returned unexpected result: {result}")
        if admin_handle:
            _send_outbox(admin_handle, f"Finalization failed: {result.get('error', 'unknown')}")


# ── Week helpers ──────────────────────────────────────────────────────────────

def _get_week_start() -> date:
    """Return next Monday (or this Monday if today is Monday)."""
    today = date.today()
    days_ahead = (0 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)


# ── handle_start ──────────────────────────────────────────────────────────────

def handle_start(config: dict) -> str:
    """idle → active. Calls MB bridge for last week's meals, seeds conversation history."""
    result = call_menubuilder_tool("start_menu_workflow")
    if "error" in result:
        return "Sorry, couldn't start the menu workflow. Check the logs."

    meals = result.get("last_week_meals", [])

    # Normalize week_start to Monday — MCP may return today (Sunday) on trigger day
    raw_week_start = result.get("week_start", "")
    try:
        ws = date.fromisoformat(raw_week_start)
        if ws.weekday() != 0:  # 0 = Monday
            ws = ws + timedelta(days=(7 - ws.weekday()) % 7)
        week_start_str = ws.isoformat()
    except (ValueError, TypeError):
        week_start_str = _get_week_start().isoformat()

    # Partition meals missing feedback into first-cooks (prompt) vs known (skip silently)
    first_cook_missing = []
    for m in meals:
        if m.get("sms_feedback"):
            continue
        if m.get("times_cooked", 0) == 0:
            first_cook_missing.append(m)
        # Known meals with no feedback are silently skipped — no prompt needed

    session = _load_session()
    session["last_week_meals"] = meals
    session["week_start"] = week_start_str
    session["schedule_notes"] = []
    session["quick_days"] = []
    session["selected_meals"] = {}

    if not first_cook_missing:
        session["state"] = "awaiting_schedule"
        reply = "Let's make this week's menu.\n\nAny schedule changes this week?"
    else:
        session["state"] = "awaiting_meal_logging"
        session["feedback_queue"] = [m["name"] for m in first_cook_missing]
        first = first_cook_missing[0]["name"]
        reply = f"Let's make this week's menu.\n\nHow did {first} go?"

    # Seed conversation history with opening exchange so Claude has context
    # on the first reply. Uses a placeholder user turn so history starts correctly.
    session["conversation"] = [
        {"role": "user", "content": "[Menu build started]"},
        {"role": "assistant", "content": reply},
    ]

    _save_session(session)
    return reply


# ── handle_ashley_reply ───────────────────────────────────────────────────────

def handle_ashley_reply(text: str, session: dict, config: dict):
    """
    Called from server.py after Ashley replies while awaiting_ashley_signoff.
    Delegates to MenuBuilder via bridge.
    """
    admin_handle = config["security"].get("menu_admin")
    partner_handle = config["security"].get("partner_handle", "")

    # If we're mid URL-swap resolution, handle that first
    if session.get("pending_url_swap"):
        _handle_pending_url_swap(text, session, config)
        return

    result = call_menubuilder_tool("handle_ashley_reply", reply=text)

    # MenuBuilder detected a URL in Ashley's message — resolve it
    if result.get("has_url"):
        url = result["url"]
        day = result.get("extracted_day", "")
        url_result = call_menubuilder_tool("process_recipe_url", url=url, day=day)
        status = url_result.get("status")

        if status == "similar_exists":
            existing = url_result["recipe"]
            score = url_result.get("match_score", 0)
            session = _load_session()
            session["pending_url_swap"] = {"url": url, "day": day, "existing_recipe": existing}
            _save_session(session)
            _send_to_ashley(
                f"I already have something similar: \"{existing}\" ({score:.0%} match). "
                f"Use that one, or add this new recipe?",
                partner_handle,
            )
            if admin_handle:
                _send_outbox(admin_handle, f"Ashley sent a URL for {day} — similar recipe exists. Waiting on her choice.")
        elif status == "added":
            new_recipe = url_result["recipe"]
            if day and url_result.get("swapped_day"):
                _send_to_ashley(f"Got it — swapped {day} to {new_recipe}.", partner_handle)
            elif day:
                call_menubuilder_tool("swap_meal", day=day, reason="Ashley request", replacement=new_recipe)
                _send_to_ashley(f"Got it — swapped {day} to {new_recipe}.", partner_handle)
            if admin_handle:
                _send_outbox(admin_handle, f"Ashley's URL added as '{new_recipe}' and scheduled for {day}.")
        else:
            _send_to_ashley("Couldn't fetch that recipe. Can you paste the name?", partner_handle)
            if admin_handle:
                _send_outbox(admin_handle, f"Ashley sent an unfetchable URL: {url}")
        return

    new_state = result.get("state", "")
    _sync_session_state(new_state)

    if new_state == "complete":
        if admin_handle:
            _send_outbox(admin_handle, "Plan written, apps launched.")
    elif new_state == "awaiting_idea_activation":
        pending_ideas = result.get("pending_ideas", [])  # list of {"name": str, "source_url": str}
        if admin_handle and pending_ideas:
            first = pending_ideas[0]
            name = first["name"]
            has_url = bool(first.get("source_url", ""))

            session = _load_session()
            session["pending_idea"] = name
            session["remaining_ideas"] = [p["name"] for p in pending_ideas[1:]]

            if has_url:
                session["state"] = "awaiting_idea_content"
                _save_session(session)
                _send_outbox(admin_handle, f"Couldn't fetch '{name}' — paste the recipe content and I'll activate it.")
            else:
                session["state"] = "awaiting_idea_url"
                _save_session(session)
                _send_outbox(admin_handle, f"I don't have a source URL for '{name}'. What's the URL?")
    elif new_state == "awaiting_ashley_signoff":
        # Ashley requested a change — MenuBuilder re-sent the updated menu
        if admin_handle:
            _send_outbox(admin_handle, f"Ashley requested a change: '{text}'. Updated and re-sent.")
    else:
        if admin_handle:
            _send_outbox(admin_handle, f"Ashley replied but something went wrong. Handle manually: '{text}'")


# ── _handle_pending_url_swap ──────────────────────────────────────────────────

def _handle_pending_url_swap(text: str, session: dict, config: dict):
    """
    Ashley replied to the 'similar recipe exists' question.
    Pending context is in session["pending_url_swap"].
    """
    admin_handle = config["security"].get("menu_admin")
    partner_handle = config["security"].get("partner_handle", "")
    pending = session.get("pending_url_swap", {})
    url = pending.get("url", "")
    day = pending.get("day", "")
    existing = pending.get("existing_recipe", "")

    lower = text.lower()
    if any(w in lower for w in ("new", "add", "different", "that one", "yes", "yeah", "yep")):
        result = call_menubuilder_tool("process_recipe_url", url=url, day=day, force_add=True)
        new_recipe = result.get("recipe", url)
        _send_to_ashley(f"Added and swapped {day} to {new_recipe}.", partner_handle)
        if admin_handle:
            _send_outbox(admin_handle, f"Ashley chose to add new recipe '{new_recipe}' for {day}.")
    else:
        call_menubuilder_tool("swap_meal", day=day, reason="Ashley request", replacement=existing)
        _send_to_ashley(f"Got it — using {existing} for {day}.", partner_handle)
        if admin_handle:
            _send_outbox(admin_handle, f"Ashley chose existing recipe '{existing}' for {day}.")

    session.pop("pending_url_swap", None)
    _save_session(session)
    call_menubuilder_tool("approve_menu")


# ── _handle_idea_content ──────────────────────────────────────────────────────

def _handle_idea_content(text: str, session: dict, config: dict) -> str:
    """awaiting_idea_content — admin pasted recipe text for an idea that couldn't be fetched."""
    pending = session.get("pending_idea", "")
    if pending:
        call_menubuilder_tool("activate_idea_recipe", name=pending, content=text)
        log.info(f"Activated idea '{pending}' via MenuBuilder.")

    remaining = session.get("remaining_ideas", [])
    if remaining:
        session["pending_idea"] = remaining[0]
        session["remaining_ideas"] = remaining[1:]
        _save_session(session)
        return f"Got it! Now paste the content for '{remaining[0]}'."

    session.pop("pending_idea", None)
    session.pop("remaining_ideas", None)
    _save_session(session)
    handle_finalize(session, config)
    return "Thanks! Finishing up the plan..."


# ── _handle_idea_url ─────────────────────────────────────────────────────────

def _handle_idea_url(text: str, session: dict, config: dict) -> str:
    """awaiting_idea_url — admin provided a URL for an idea that had none."""
    pending = session.get("pending_idea", "")

    result = call_menubuilder_tool("activate_idea_recipe", name=pending, source_url=text.strip())

    if result.get("auto_activated"):
        remaining = session.get("remaining_ideas", [])
        if remaining:
            session["pending_idea"] = remaining[0]
            session["remaining_ideas"] = remaining[1:]
            session["state"] = "awaiting_idea_content"
            _save_session(session)
            return f"Got it! Now paste the content for '{remaining[0]}'."
        session.pop("pending_idea", None)
        session.pop("remaining_ideas", None)
        _save_session(session)
        handle_finalize(session, config)
        return "Thanks! Finishing up the plan..."
    else:
        # Fetch failed — fall back to paste
        session["state"] = "awaiting_idea_content"
        _save_session(session)
        return f"Couldn't fetch that URL. Paste the recipe content for '{pending}'."


# ── Agent tool definitions ────────────────────────────────────────────────────

def _build_menu_tools() -> list:
    return [
        {
            "name": "log_meal_feedback",
            "description": (
                "Record David's feedback for a single meal from last week. "
                "Call once per meal after he responds about how it went."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "meal_name": {
                        "type": "string",
                        "description": "Name of the meal from last week's plan",
                    },
                    "feedback": {
                        "type": "string",
                        "description": "David's feedback, e.g. 'loved it', 'kids didn't eat it', 'make again soon'",
                    },
                },
                "required": ["meal_name", "feedback"],
            },
        },
        {
            "name": "record_schedule_note",
            "description": (
                "Save a schedule constraint for this week. "
                "Call whenever David mentions a busy night, game, practice, or other constraint."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": "The schedule note, e.g. 'soccer game Tuesday', 'busy Wednesday evening'",
                    },
                },
                "required": ["note"],
            },
        },
        {
            "name": "generate_meal_plan",
            "description": (
                "Generate this week's meal suggestions based on accumulated schedule notes "
                "and the cuisine direction David gives. "
                "Call after asking about schedule and getting a cuisine direction. "
                "Returns the proposed meal list to show David."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "cuisine_direction": {
                        "type": "string",
                        "description": "Cuisine preference, e.g. 'Mexican', 'Asian', 'Italian', \"what we've got\"",
                    },
                },
                "required": ["cuisine_direction"],
            },
        },
        {
            "name": "swap_meal",
            "description": (
                "Swap the meal for a specific day in the current plan. "
                "Call once per day when David asks to change something. "
                "Pass the day abbreviation and his reason — the system handles finding a replacement."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "day": {
                        "type": "string",
                        "description": "Three-letter day abbreviation to swap: Mon, Tue, Wed, Thu, Fri, Sat, or Sun",
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "Why this day is being swapped. Be specific — the system uses this to pick a replacement. "
                            "Examples: 'prefer ideas', 'swap to Indian', 'too much chicken this week', "
                            "'try a new recipe we haven't made before'. "
                            "If David asks to look at new ideas, pass 'prefer ideas'."
                        ),
                    },
                    "replacement": {
                        "type": "string",
                        "description": (
                            "Exact recipe name if David names a specific recipe. "
                            "Leave empty to let the system auto-pick based on the reason."
                        ),
                    },
                },
                "required": ["day", "reason"],
            },
        },
        {
            "name": "approve_menu",
            "description": (
                "Approve the current meal plan and send it to Ashley for sign-off. "
                "Call when David says the plan looks good, or approves it."
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    ]


# ── Agent tool execution ──────────────────────────────────────────────────────

def _execute_menu_tool(tool_name: str, tool_input: dict, session: dict, config: dict) -> str:
    if tool_name == "log_meal_feedback":
        meal_name = tool_input.get("meal_name", "")
        feedback = tool_input.get("feedback", "")
        meals = session.get("last_week_meals", [])
        queue = session.get("feedback_queue", [])

        # Record feedback on the matching meal locally (for conversation context)
        for meal in meals:
            name_lower = meal["name"].lower()
            input_lower = meal_name.lower()
            if name_lower == input_lower or input_lower in name_lower or name_lower in input_lower:
                if not meal.get("sms_feedback"):
                    meal["sms_feedback"] = feedback
                break

        # Forward each individual feedback to MenuBuilder as it arrives.
        # MCP matches by keyword — include meal name so it lands on the right meal.
        call_menubuilder_tool("log_meal_feedback", feedback=f"{meal_name}: {feedback}")

        # Remove from queue
        queue = [
            m for m in queue
            if m.lower() != meal_name.lower() and meal_name.lower() not in m.lower()
        ]
        session["last_week_meals"] = meals
        session["feedback_queue"] = queue

        # When all feedback is collected, signal done and handle first-cook detection
        if not queue:
            result = call_menubuilder_tool("log_meal_feedback", feedback="done")
            first_cooks = result.get("first_cook_meals", [])
            if first_cooks:
                session["first_cook_queue"] = first_cooks
                session["state"] = "awaiting_first_cook_feedback"
            else:
                session["state"] = "awaiting_schedule"

        _save_session(session)
        log.info(f"Logged feedback for '{meal_name}': {feedback}")
        remaining = len(queue)
        if remaining:
            return f"Feedback recorded for {meal_name}. {remaining} meal(s) still need feedback."
        return f"Feedback recorded for {meal_name}. All feedback collected."

    if tool_name == "record_schedule_note":
        note = tool_input.get("note", "")
        notes = session.get("schedule_notes", [])
        if note not in notes:
            notes.append(note)
        session["schedule_notes"] = notes
        _save_session(session)
        log.info(f"Schedule note recorded: {note}")
        return f"Schedule note saved: {note}"

    if tool_name == "generate_meal_plan":
        cuisine_direction = tool_input.get("cuisine_direction", "")
        session["cuisine_direction"] = cuisine_direction

        constraints = "; ".join(session.get("schedule_notes", []))

        result = call_menubuilder_tool(
            "get_meal_suggestions",
            cuisine_direction=cuisine_direction,
            constraints=constraints,
        )

        if "error" in result:
            return f"Couldn't generate a plan: {result['error']}"

        selected = result.get("selected_meals", {})
        quick_days = result.get("quick_days", [])
        session["selected_meals"] = selected
        session["quick_days"] = quick_days
        session["state"] = "awaiting_meal_approval"
        _save_session(session)

        # MCP already wrote to activity — no advance_to_meal_approval needed
        ws = date.fromisoformat(session.get("week_start", date.today().isoformat()))
        return _format_numbered_list(selected, ws)

    if tool_name == "swap_meal":
        day = tool_input.get("day", "")
        reason = tool_input.get("reason", "")
        replacement = tool_input.get("replacement", "")
        result = call_menubuilder_tool(
            "swap_meal",
            day=day,
            reason=reason,
            replacement=replacement,
            cuisine_direction=session.get("cuisine_direction", ""),
        )
        new_state = result.get("state", session.get("state"))
        _sync_session_state(new_state)
        session["state"] = new_state
        updated = result.get("selected_meals")
        if updated:
            session["selected_meals"] = updated
        _save_session(session)
        week_start = date.fromisoformat(session["week_start"])
        plan = _format_numbered_list(session.get("selected_meals", {}), week_start, session.get("quick_days", []))
        note = result.get("note", "")
        return f"{note}\n\n{plan}" if note else plan

    if tool_name == "approve_menu":
        result = call_menubuilder_tool("approve_menu")
        if "error" in result:
            log.error(f"approve_menu MCP error: {result['error']}")
            return "Something went wrong sending the menu to Ashley — check the logs."
        # Generate shopping list now that meals are locked
        sl_result = call_menubuilder_tool(
            "generate_shopping_list",
            meals=session.get("selected_meals", {}),
            week_start=session.get("week_start", ""),
        )
        if "error" in sl_result:
            log.warning(f"generate_shopping_list failed: {sl_result['error']}")
        session["state"] = "awaiting_ashley_signoff"
        _save_session(session)
        return "Menu sent to Ashley."

    return f"Unknown tool: {tool_name}"


# ── Agent system prompt ───────────────────────────────────────────────────────

def _build_menu_system_prompt(session: dict) -> str:
    """Build a context-aware system prompt for the menu agent turn."""
    meals = session.get("last_week_meals", [])
    queue = session.get("feedback_queue", [])
    schedule_notes = session.get("schedule_notes", [])
    selected = session.get("selected_meals", {})
    week_start_str = session.get("week_start", date.today().isoformat())

    lines = ["\n\n## Menu Workflow Active\n"]

    if meals:
        lines.append("**Last week's meals:**")
        for m in meals:
            fb = f" — {m['sms_feedback']}" if m.get("sms_feedback") else " — no feedback yet"
            lines.append(f"- {m['day']}: {m['name']}{fb}")
        lines.append("")

    if queue:
        lines.append(f"**Feedback still needed from David:** {', '.join(queue)}\n")
    else:
        lines.append("**All meal feedback collected.**\n")

    if schedule_notes:
        lines.append(f"**Schedule notes collected:** {'; '.join(schedule_notes)}\n")

    if selected:
        try:
            ws = date.fromisoformat(week_start_str)
        except Exception:
            ws = date.today()
        lines.append("**Current meal selection:**")
        lines.append(_format_numbered_list(selected, ws))
        lines.append("")

    lines.append(
        "**Your job:** Continue the menu build conversation naturally — one question at a time.\n"
        "1. Collect any remaining meal feedback (log_meal_feedback for each meal, one at a time).\n"
        "2. Ask about schedule changes this week (record_schedule_note for any constraints).\n"
        "3. Ask for cuisine direction, then call generate_meal_plan.\n"
        "4. Show the list. Refine with swap_meal if David requests changes.\n"
        "   IMPORTANT: if David says 'lock in', 'keep', 'yes', or 'looks good' for a specific day without naming a new recipe — just confirm it verbally. Do NOT call swap_meal.\n"
        "5. Call approve_menu when David is happy with the plan.\n"
        "\nThis is SMS — keep replies short. One question per message.\n"
        "David may step away and return hours later. If there's conversation history, "
        "pick up naturally where you left off without asking him to re-explain anything."
    )

    return _MENU_SYSTEM + "\n".join(lines)


# ── Main agent entry point ────────────────────────────────────────────────────

def menu_agent_reply(text: str, session: dict, config: dict) -> str:
    """
    Route an inbound message from the menu admin through Claude agent tool-use.
    Replaces the old dispatch() state machine.
    Conversation history is accumulated in session["conversation"] and persisted
    to menu_session.json so context survives across SMS gaps.
    """
    import anthropic as _anthropic

    state = session.get("state", "idle")

    # Hold/pause — acknowledge and exit without advancing state
    if any(p in text.lower() for p in ("hold", "pause", "not now", "later", "stop for now")):
        return "Got it — pick it up whenever you're ready."

    # Paste-based idea activation — doesn't fit agent model
    if state == "awaiting_idea_content":
        return _handle_idea_content(text, session, config)

    if state == "awaiting_idea_url":
        return _handle_idea_url(text, session, config)

    # Waiting on Ashley — David just gets a status update
    if state == "awaiting_ashley_signoff":
        return "Still waiting on Ashley's OK — I'll let you know when she replies."

    # Finalization pass-through
    if state == "awaiting_finalization":
        result = call_menubuilder_tool("finalize_plan")
        _sync_session_state("complete")
        session["state"] = "complete"
        _save_session(session)
        return "Plan ready!"

    # Build conversation history for this turn
    conversation = session.get("conversation", [])
    now_str = datetime.now().strftime("%A, %B %-d at %-I:%M %p")
    conversation.append({"role": "user", "content": f"[{now_str}] {text}"})

    system = _build_menu_system_prompt(session)
    tools = _build_menu_tools()

    try:
        client = _anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    except Exception as e:
        log.error(f"Could not create Anthropic client: {e}")
        return "Sorry, I ran into an error — try again in a moment."

    # Local copy of messages for this turn (may grow with tool_use/tool_result blocks)
    messages = list(conversation)
    final_reply = ""

    try:
        for _ in range(5):
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=500,
                system=system,
                tools=tools,
                tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                messages=messages,
            )

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result = _execute_menu_tool(block.name, block.input, session, config)
                        log.info(f"Menu tool {block.name}({block.input}) -> {result[:120]}")
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })
                # Append tool use/result to local messages only (not persisted)
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})
            else:
                final_reply = next(
                    (b.text for b in response.content if hasattr(b, "text")),
                    "Sorry, something went wrong."
                ).strip()
                break

    except Exception as e:
        log.error(f"Menu agent error: {e}")
        final_reply = "Sorry, I ran into a snag — try again in a moment."

    if final_reply:
        conversation.append({"role": "assistant", "content": final_reply})

    # Persist only simple text turns (cap at 40 to avoid bloat)
    session["conversation"] = conversation[-40:]
    _save_session(session)

    return final_reply
