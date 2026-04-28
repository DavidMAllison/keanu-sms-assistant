"""Tool definitions and implementations for Keanu."""

import json
import logging
import time
from datetime import date, timedelta
from pathlib import Path

import re

from agents.menu_agent import (
    load_context,
    find_all_recipe_matches,
    extract_recipe_text,
    save_recipe_idea as _save_idea,
    update_meal_plan as _update_plan,
    save_feedback,
    save_preference,
    _load_handle_to_name,
    RECIPES_DIR,
    RECIPE_CHUNK_CHARS,
    get_dropbox_preview_url,
    METADATA_FILE,
)
from agents.menu_agent import IDEAS_DIR
from agents.schedule_agent import (
    _load_schedule,
    _next_occurrence,
    _biweekly_active,
    _format_time,
)

log = logging.getLogger(__name__)

_BASE = Path(__file__).parent
GAPS_FILE = _BASE / "capability_gaps.json"


# ── Tool definitions (sent to Claude API) ─────────────────────────────────────

_DEFS = {
    "get_meal_plan": {
        "name": "get_meal_plan",
        "description": "Get the current week's meal plan, inventory, and family preferences.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "get_recipe": {
        "name": "get_recipe",
        "description": (
            "Get the full recipe for a meal by name. Returns the recipe text if it's short enough "
            "to send, or a Dropbox link if it's long."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Recipe name to look up"},
            },
            "required": ["name"],
        },
    },
    "get_schedule": {
        "name": "get_schedule",
        "description": "Get upcoming games and practices for a family member.",
        "input_schema": {
            "type": "object",
            "properties": {
                "person": {
                    "type": "string",
                    "description": "Family member name: Eleanor, Wren, David, or Ashley",
                },
                "event_type": {
                    "type": "string",
                    "enum": ["game", "practice", "any"],
                    "description": "Type of event to look up",
                },
            },
            "required": ["person", "event_type"],
        },
    },
    "check_recipe_similarity": {
        "name": "check_recipe_similarity",
        "description": (
            "Check whether a recipe already exists in the ideas list or recipe collection "
            "before saving it. Always call this before save_recipe_idea."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The recipe URL, name, or description to check",
                },
            },
            "required": ["content"],
        },
    },
    "save_recipe_idea": {
        "name": "save_recipe_idea",
        "description": "Save a recipe idea (URL, name, or description) to the ideas list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The recipe URL, name, or description to save",
                },
            },
            "required": ["content"],
        },
    },
    "update_meal_plan": {
        "name": "update_meal_plan",
        "description": "Change a meal in the weekly plan. Admin only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": "Natural language, e.g. 'change Thursday to chicken tacos'",
                },
            },
            "required": ["instruction"],
        },
    },
    "log_feedback": {
        "name": "log_feedback",
        "description": "Log meal feedback or ratings about a recipe.",
        "input_schema": {
            "type": "object",
            "properties": {
                "recipe": {"type": "string", "description": "Recipe name"},
                "feedback": {"type": "string", "description": "The feedback to log"},
            },
            "required": ["recipe", "feedback"],
        },
    },
    "log_preference": {
        "name": "log_preference",
        "description": "Record a dietary preference or food note for the family.",
        "input_schema": {
            "type": "object",
            "properties": {
                "preference": {"type": "string", "description": "The preference to record"},
            },
            "required": ["preference"],
        },
    },
    "add_schedule_event": {
        "name": "add_schedule_event",
        "description": (
            "Add an event to the family schedule. Use date for one-time events, "
            "day_of_week for recurring ones."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "person": {
                    "type": "string",
                    "description": "Family member name: Eleanor, Wren, David, or Ashley",
                },
                "description": {
                    "type": "string",
                    "description": "Event description or note (e.g. 'Soccer game 2:00-3:00 PM at Miller Park')",
                },
                "affects_dinner": {
                    "type": "boolean",
                    "description": "Whether this event affects dinner timing",
                },
                "date": {
                    "type": "string",
                    "description": "For one-time events: ISO date YYYY-MM-DD",
                },
                "day_of_week": {
                    "type": "string",
                    "description": "For recurring events: Monday, Tuesday, etc.",
                },
                "activity": {
                    "type": "string",
                    "description": "For recurring events: short activity name (e.g. 'Soccer practice')",
                },
                "start_time": {
                    "type": "string",
                    "description": "For recurring events: start time in HH:MM 24h format",
                },
                "end_time": {
                    "type": "string",
                    "description": "For recurring events: end time in HH:MM 24h format",
                },
            },
            "required": ["person", "description", "affects_dinner"],
        },
    },
    "log_capability_gap": {
        "name": "log_capability_gap",
        "description": (
            "Log a request you couldn't fulfil, or feedback about your own behaviour, "
            "for the admin to review."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "What was requested or the feedback"},
            },
            "required": ["description"],
        },
    },
}


def build_tool_list(is_kid: bool, is_admin: bool, is_idea_submitter: bool) -> list:
    if is_kid:
        return [_DEFS["get_meal_plan"], _DEFS["get_schedule"]]
    names = ["get_meal_plan", "get_recipe", "get_schedule", "add_schedule_event",
             "log_feedback", "log_preference", "log_capability_gap"]
    if is_idea_submitter:
        names += ["check_recipe_similarity", "save_recipe_idea"]
    if is_admin:
        names.append("update_meal_plan")
    return [_DEFS[n] for n in names]


# ── Tool implementations ───────────────────────────────────────────────────────

def _tool_get_meal_plan() -> str:
    return load_context()


def _tool_get_recipe(name: str) -> str:
    matches = find_all_recipe_matches(name)
    if not matches:
        return f"No recipe found matching '{name}'."
    if len(matches) > 1:
        names = ", ".join(m["name"] for m in matches)
        return f"Found multiple matches: {names}. Which one?"
    m = matches[0]
    text = extract_recipe_text(RECIPES_DIR / m["filename"])
    if not text:
        return f"Couldn't read that recipe. Dropbox link: {get_dropbox_preview_url(m['filename'])}"
    dropbox = get_dropbox_preview_url(m["filename"])
    return f"{text}\n\n[Dropbox link if user wants full recipe: {dropbox}]"


def _tool_get_schedule(person: str, event_type: str = "any") -> str:
    schedule = _load_schedule()
    today = date.today()
    results = []

    if event_type in ("game", "any"):
        for date_str in sorted(schedule.get("weekly_overrides", {}).keys()):
            event_date = date.fromisoformat(date_str)
            if event_date < today:
                continue
            for entry in schedule["weekly_overrides"][date_str]:
                if entry.get("person") != person:
                    continue
                note = entry.get("note", "")
                if any(w in note.lower() for w in ("game", "match", "tournament", "cup")):
                    results.append(("game", event_date, note))

    if event_type in ("practice", "any"):
        for day_name, entries in schedule.get("standing", {}).items():
            for entry in entries:
                if entry.get("person") != person:
                    continue
                next_date = _next_occurrence(day_name, today)
                if not _biweekly_active(entry, next_date):
                    next_date += timedelta(weeks=2)
                activity = entry.get("activity", "")
                start = _format_time(entry.get("start", ""))
                end = _format_time(entry.get("end", ""))
                location = entry.get("note", "").split(" — ")[0] if entry.get("note") else ""
                desc = f"{activity}, {start}–{end}"
                if location:
                    desc += f" ({location})"
                results.append(("practice", next_date, desc))

    if not results:
        what = event_type if event_type != "any" else "event"
        return f"No upcoming {what} found for {person}."

    results.sort(key=lambda x: x[1])
    lines = []
    for kind, event_date, desc in results[:3]:
        day_str = event_date.strftime("%A, %B %-d")
        lines.append(f"{kind.title()}: {day_str} — {desc}")
    return "\n".join(lines)


def _tool_check_recipe_similarity(content: str) -> str:
    results = []

    # 1. Exact URL match in ideas list
    url_match = re.search(r"https?://\S+", content)
    url = url_match.group().rstrip(".,)") if url_match else None
    if url and IDEAS_DIR.exists():
        for f in IDEAS_DIR.iterdir():
            if f.suffix == ".txt" and url in f.read_text():
                results.append(f"Already in ideas list: {f.name}")

    # 2. Name/keyword match against ideas list filenames
    if IDEAS_DIR.exists():
        slug = url_match and re.sub(r"^\d+[-_]?", "", url.rstrip("/").split("/")[-1]) or ""
        slug_words = [w for w in re.split(r"[-_]", slug) if len(w) > 3]
        for f in IDEAS_DIR.iterdir():
            if f.suffix != ".txt":
                continue
            fname = f.stem.lower()
            if slug_words and sum(1 for w in slug_words if w in fname) >= 2:
                if not any(f.name in r for r in results):
                    results.append(f"Similar idea already saved: {f.name}")

    # 3. Keyword match against recipe collection
    search_term = slug.replace("-", " ").replace("_", " ") if url else content
    matches = find_all_recipe_matches(search_term)
    if matches:
        try:
            meta_data = json.loads(METADATA_FILE.read_text()).get("recipes", {})
        except Exception:
            meta_data = {}
        for m in matches[:4]:
            meta = meta_data.get(m["name"], {})
            cuisine = meta.get("cuisine", "")
            method = meta.get("cooking_method", "")
            ingredients = meta.get("ingredients", [])
            key_ingr = ", ".join(i["name"] for i in ingredients[:4]) if ingredients else ""
            detail = " | ".join(filter(None, [cuisine, method, key_ingr]))
            results.append(f"Similar recipe in collection: {m['name']}" + (f" ({detail})" if detail else ""))

    if not results:
        return "No duplicates or similar recipes found."
    return "\n".join(results)


def _tool_save_recipe_idea(content: str) -> str:
    return "Saved to the ideas list." if _save_idea(content) else "Couldn't save that one."


def _tool_update_meal_plan(instruction: str) -> str:
    result = _update_plan(instruction)
    return f"Updated: {result}" if result else "Couldn't parse that — try 'change Thursday to chicken tacos'."


def _tool_log_feedback(recipe: str, feedback: str, handle: str) -> str:
    handle_map = _load_handle_to_name()
    person = handle_map.get(handle, "Someone")
    entries = [{"person": person, "text": feedback, "sentiment": "neutral"}]
    return "Feedback logged." if save_feedback(recipe, entries) else "Couldn't save feedback."


def _tool_log_preference(preference: str, handle: str) -> str:
    return "Preference noted." if save_preference(preference, handle) else "Couldn't save that."


def _tool_add_schedule_event(inputs: dict) -> str:
    from agents.schedule_agent import _load_schedule_path
    schedule_file = _load_schedule_path()
    try:
        data = json.loads(schedule_file.read_text())
    except Exception as e:
        return f"Couldn't read schedule: {e}"

    person = inputs["person"]
    description = inputs["description"]
    affects_dinner = inputs.get("affects_dinner", False)
    date_str = inputs.get("date")
    day_of_week = inputs.get("day_of_week")

    if date_str:
        # One-time event → weekly_overrides
        overrides = data.setdefault("weekly_overrides", {})
        overrides.setdefault(date_str, []).append({
            "person": person,
            "note": description,
            "affects_dinner": affects_dinner,
        })
        label = date_str
    elif day_of_week:
        # Recurring event → standing
        standing = data.setdefault("standing", {})
        entry = {
            "person": person,
            "activity": inputs.get("activity", description),
            "affects_dinner": affects_dinner,
        }
        if inputs.get("start_time"):
            entry["start"] = inputs["start_time"]
        if inputs.get("end_time"):
            entry["end"] = inputs["end_time"]
        if description != inputs.get("activity"):
            entry["note"] = description
        standing.setdefault(day_of_week, []).append(entry)
        label = f"every {day_of_week}"
    else:
        return "Need either a date or a day_of_week to add an event."

    data["last_updated"] = time.strftime("%Y-%m-%d")
    try:
        schedule_file.write_text(json.dumps(data, indent=2))
        return f"Added {person}'s event on {label}."
    except Exception as e:
        return f"Couldn't save schedule: {e}"


def _tool_log_capability_gap(description: str, handle: str) -> str:
    gaps = json.loads(GAPS_FILE.read_text()) if GAPS_FILE.exists() else []
    gaps.append({
        "date": time.strftime("%Y-%m-%d"),
        "handle": handle,
        "request": description,
        "reviewed": False,
    })
    GAPS_FILE.write_text(json.dumps(gaps, indent=2))
    return "Noted."


def execute_tool(name: str, inputs: dict, handle: str, config: dict) -> str:
    try:
        if name == "get_meal_plan":
            return _tool_get_meal_plan()
        if name == "get_recipe":
            return _tool_get_recipe(inputs["name"])
        if name == "get_schedule":
            return _tool_get_schedule(inputs["person"], inputs.get("event_type", "any"))
        if name == "add_schedule_event":
            return _tool_add_schedule_event(inputs)
        if name == "check_recipe_similarity":
            return _tool_check_recipe_similarity(inputs["content"])
        if name == "save_recipe_idea":
            return _tool_save_recipe_idea(inputs["content"])
        if name == "update_meal_plan":
            return _tool_update_meal_plan(inputs["instruction"])
        if name == "log_feedback":
            return _tool_log_feedback(inputs["recipe"], inputs["feedback"], handle)
        if name == "log_preference":
            return _tool_log_preference(inputs["preference"], handle)
        if name == "log_capability_gap":
            return _tool_log_capability_gap(inputs["description"], handle)
        return f"Unknown tool: {name}"
    except Exception as e:
        log.error(f"Tool error ({name}): {e}")
        return f"Tool error: {e}"
