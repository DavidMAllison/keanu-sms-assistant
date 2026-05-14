"""Tool definitions and implementations for Keanu."""

import json
import logging
import time
from datetime import date, timedelta
from pathlib import Path

import re
from typing import Optional

from agents.menu_agent import (
    load_context,
    find_all_recipe_matches,
    extract_recipe_text,
    save_recipe_idea as _save_idea,
    update_meal_plan as _update_plan,
    save_preference,
    _load_handle_to_name,
    COOKING_BASE,
    IDEAS_DIR,
    RECIPES_DIR,
    WEEKLYPLAN_DIR,
    RECIPE_CHUNK_CHARS,
    get_dropbox_preview_url,
    METADATA_FILE,
)
from agents.schedule_agent import (
    _load_schedule,
    _next_occurrence,
    _biweekly_active,
    _format_time,
)

log = logging.getLogger(__name__)

_BASE = Path(__file__).parent
GAPS_FILE = _BASE / "capability_gaps.json"
OUTBOX_FILE = _BASE / ".outbox.json"
INVENTORY_FILE = Path("/Users/Shared/cooking/inventory.md")


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
            "Search the ideas list and recipe collection by name or keyword. Use this to answer "
            "'is X on my ideas list?' questions, and always call it before save_recipe_idea."
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
                "sentiment": {
                    "type": "string",
                    "enum": ["liked", "disliked", "mixed"],
                    "description": "Overall sentiment inferred from the message",
                },
            },
            "required": ["recipe", "feedback", "sentiment"],
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
    "update_inventory": {
        "name": "update_inventory",
        "description": "Add an item to the food inventory (freezer, pantry, etc.).",
        "input_schema": {
            "type": "object",
            "properties": {
                "item": {"type": "string", "description": "Item with quantity, e.g. '6 Costco chicken breast packages'"},
                "section": {
                    "type": "string",
                    "enum": ["Frozen - Chicken", "Frozen - Pork", "Frozen - Beef", "Frozen Meals", "Vegetables/Produce", "Pantry Staples", "Dairy", "Other"],
                    "description": "Which inventory section to add it to",
                },
            },
            "required": ["item", "section"],
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
    "relay_message": {
        "name": "relay_message",
        "description": (
            "Send a message to another family member on the admin's behalf. "
            "Use when the admin asks you to forward something or message someone else."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "Name of the recipient (e.g. 'Ashley', 'Wren')"},
                "message": {"type": "string", "description": "The message text to send"},
            },
            "required": ["recipient", "message"],
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
        names += ["update_meal_plan", "update_inventory", "relay_message"]
    return [_DEFS[n] for n in names]


# ── Tool implementations ───────────────────────────────────────────────────────

def _tool_get_meal_plan() -> str:
    return load_context()


def _find_in_meal_plan(name: str) -> Optional[tuple]:
    """Search the current week's meal plan for a meal matching name. Returns (meal_name, url) or None."""
    today = date.today()
    _DAY_ABBREVS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    _DAY_NAMES = {
        "monday": "Mon", "tuesday": "Tue", "wednesday": "Wed",
        "thursday": "Thu", "friday": "Fri", "saturday": "Sat", "sunday": "Sun",
    }

    # Resolve temporal/day keywords to the 3-letter abbreviation used in meal plan lines
    name_lower = name.lower().strip()
    day_prefix = None  # e.g. "Mon" — search by line prefix instead of keywords
    if any(w in name_lower for w in ("tonight", "today", "dinner")):
        day_prefix = _DAY_ABBREVS[today.weekday()]
    else:
        for day_word, abbrev in _DAY_NAMES.items():
            if day_word in name_lower:
                day_prefix = abbrev
                break

    plan_files = sorted(WEEKLYPLAN_DIR.glob("mealplan_*.txt"), reverse=True)
    for pf in plan_files:
        try:
            week_start = date.fromisoformat(pf.stem.replace("mealplan_", ""))
        except ValueError:
            continue
        if week_start > today:
            continue
        lines = pf.read_text().splitlines()

        def _extract_url_and_name(i: int):
            for j in range(i + 1, min(i + 3, len(lines))):
                stripped = lines[j].strip()
                if stripped.startswith("http"):
                    meal_name = lines[i].strip().split("[")[0].split("|")[0].strip()
                    meal_name = re.sub(r"^[A-Za-z]{3}\s+\d+/\d+\s+", "", meal_name).strip()
                    return meal_name, stripped
            return None

        if day_prefix:
            # Match the line that starts with the day abbreviation
            for i, line in enumerate(lines):
                if line.strip().startswith(day_prefix):
                    result = _extract_url_and_name(i)
                    if result:
                        return result
        else:
            keywords = [w for w in name_lower.split() if len(w) > 3]
            for i, line in enumerate(lines):
                line_lower = line.lower()
                if not keywords or sum(1 for k in keywords if k in line_lower) < max(1, len(keywords) // 2):
                    continue
                result = _extract_url_and_name(i)
                if result:
                    return result
                break
        break  # Only check the most recent plan that doesn't start after today
    return None


def _find_in_condiments(name: str) -> Optional[str]:
    condiments_file = COOKING_BASE / "condiments.json"
    if not condiments_file.exists():
        return None
    try:
        data = json.loads(condiments_file.read_text())
    except Exception:
        return None
    name_lower = name.lower()
    keywords = [w for w in name_lower.split() if len(w) > 2]
    best = None
    for key, entry in data.items():
        key_lower = key.lower()
        if not keywords or any(k in key_lower for k in keywords):
            best = (key, entry)
            break
    if not best:
        return None
    key, entry = best
    lines = [f"# {entry['name']}"]
    if entry.get("servings"):
        lines.append(f"Servings: {entry['servings']}")
    lines.append("\n## Ingredients")
    for ing in entry.get("ingredients", []):
        qty = f"{ing['quantity']} {ing['unit']}".strip()
        lines.append(f"- {qty} {ing['name']}".strip())
    lines.append("\n## Instructions")
    for i, step in enumerate(entry.get("instructions", []), 1):
        lines.append(f"{i}. {step}")
    if entry.get("notes"):
        lines.append(f"\nNotes: {entry['notes']}")
    return "\n".join(lines)


def _tool_get_recipe(name: str) -> str:
    # First: check the meal plan for a URL
    plan_result = _find_in_meal_plan(name)
    if plan_result:
        meal_name, url = plan_result
        if "dropbox.com" in url:
            # Try to serve from local recipe file via existing machinery
            matches = find_all_recipe_matches(name)
            if matches:
                m = matches[0]
                text = extract_recipe_text(RECIPES_DIR / m["filename"])
                if text:
                    return f"{text}\n\n[Dropbox link if user wants full recipe: {url}]"
            return f"Full recipe at: {url}"
        else:
            # External URL — still try local JSON metadata for ingredients first
            matches = find_all_recipe_matches(meal_name)
            if matches:
                m = matches[0]
                text = extract_recipe_text(RECIPES_DIR / m["filename"])
                if text:
                    return f"{text}\n\n[Full recipe at: {url}]"
            if METADATA_FILE.exists():
                try:
                    data = json.loads(METADATA_FILE.read_text()).get("recipes", {})
                    meal_lower = meal_name.lower()
                    for rname, meta in data.items():
                        if not isinstance(meta, dict):
                            continue
                        rname_lower = rname.lower()
                        words = [w for w in rname_lower.split() if len(w) > 3]
                        if rname_lower == meal_lower or (len(words) >= 2 and sum(1 for w in words if w in meal_lower) >= 2):
                            ingredients = meta.get("ingredients", [])
                            if ingredients:
                                lines = [f"{meal_name} - Ingredients:"]
                                for ing in ingredients:
                                    qty = f"{ing.get('quantity', '')} {ing.get('unit', '')}".strip()
                                    lines.append(f"- {qty} {ing['name']}".strip())
                                return "\n".join(lines) + f"\n\n[Full recipe at: {url}]"
                except Exception:
                    pass
            return f"That one's from an external site — full recipe and ingredients at: {url}"

    # Check condiments collection
    condiment = _find_in_condiments(name)
    if condiment:
        return condiment

    # Fall back: search local recipe collection via JSON metadata
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

    # 3. Keyword match against all JSON entries (active, idea, any status)
    search_term = slug.replace("-", " ").replace("_", " ") if url else content
    search_lower = search_term.lower()
    try:
        meta_data = json.loads(METADATA_FILE.read_text()).get("recipes", {})
    except Exception:
        meta_data = {}
    json_matches = []
    for name, meta in meta_data.items():
        if not isinstance(meta, dict):
            continue
        name_lower = name.lower()
        if name_lower == search_lower:
            json_matches = [name]
            break
        words = [w for w in name_lower.split() if len(w) > 3]
        if len(words) >= 2 and sum(1 for w in words if w in search_lower) >= 2:
            json_matches.append(name)
    for name in json_matches[:4]:
        meta = meta_data.get(name, {})
        status = meta.get("status", "active")
        cuisine = meta.get("cuisine", "")
        method = meta.get("cooking_method", "")
        ingredients = meta.get("ingredients", [])
        key_ingr = ", ".join(i["name"] for i in ingredients[:4]) if ingredients else ""
        detail = " | ".join(filter(None, [status, cuisine, method, key_ingr]))
        results.append(f"Already in collection: {name}" + (f" ({detail})" if detail else ""))

    if not results:
        return "No duplicates or similar recipes found."
    return "\n".join(results)


def _tool_save_recipe_idea(content: str) -> str:
    return "Saved to the ideas list." if _save_idea(content) else "Couldn't save that one."


def _tool_update_meal_plan(instruction: str) -> str:
    result = _update_plan(instruction)
    return f"Updated: {result}" if result else "Couldn't parse that — try 'change Thursday to chicken tacos'."


def _tool_log_feedback(recipe: str, feedback: str, sentiment: str, handle: str) -> str:
    from datetime import datetime
    queue_file = COOKING_BASE / "feedback_queue.json"
    handle_map = _load_handle_to_name()
    entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "handle": handle,
        "person": handle_map.get(handle, "Someone"),
        "recipe": recipe,
        "feedback": feedback,
        "sentiment": sentiment,
    }
    try:
        existing = json.loads(queue_file.read_text()) if queue_file.exists() else []
        existing.append(entry)
        queue_file.write_text(json.dumps(existing, indent=2))
        return "Feedback logged."
    except Exception as e:
        log.error(f"feedback queue write failed: {e}", exc_info=True)
        return "Couldn't save feedback."


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
        existing = [e for e in overrides.get(date_str, []) if e.get("person") == person]
        if existing:
            existing_notes = "; ".join(e.get("note", "") for e in existing)
            new_times = set(re.findall(r'\d{1,2}:\d{2}', description))
            existing_times = set(re.findall(r'\d{1,2}:\d{2}', existing_notes))
            if new_times & existing_times:
                return f"Already have '{existing_notes}' for {person} on {date_str}. Not added."
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


def _tool_update_inventory(item: str, section: str) -> str:
    try:
        lines = INVENTORY_FILE.read_text().splitlines()
        target = None
        for i, line in enumerate(lines):
            if section.lower() in line.lstrip("#").strip().lower():
                target = i
                break
        if target is None:
            return f"Section '{section}' not found in inventory."
        insert_at = target + 1
        none_line = None
        while insert_at < len(lines):
            l = lines[insert_at]
            if l.startswith("#") or l.strip() == "---":
                break
            if "None currently" in l:
                none_line = insert_at
            insert_at += 1
        if none_line is not None:
            lines.pop(none_line)
            insert_at = none_line
        lines.insert(insert_at, f"- {item}")
        for i, line in enumerate(lines):
            if line.startswith("**Last Updated:**"):
                lines[i] = f"**Last Updated:** {date.today().strftime('%B %-d, %Y')}"
                break
        INVENTORY_FILE.write_text("\n".join(lines) + "\n")
        return f"Added to {section}: {item}"
    except Exception as e:
        return f"Couldn't update inventory: {e}"


def _tool_relay_message(recipient: str, message: str, config: dict) -> str:
    name_to_handle = {v: k for k, v in config.get("security", {}).get("handle_to_person", {}).items()}
    handle = name_to_handle.get(recipient) or name_to_handle.get(recipient.title())
    if not handle:
        return f"Don't know a handle for '{recipient}' — check the name and try again."
    outbox = json.loads(OUTBOX_FILE.read_text()) if OUTBOX_FILE.exists() else []
    outbox.append({"handle": handle, "text": message})
    OUTBOX_FILE.write_text(json.dumps(outbox))
    return f"Sent to {recipient}."


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
            return _tool_log_feedback(inputs["recipe"], inputs["feedback"], inputs["sentiment"], handle)
        if name == "log_preference":
            return _tool_log_preference(inputs["preference"], handle)
        if name == "update_inventory":
            return _tool_update_inventory(inputs["item"], inputs["section"])
        if name == "log_capability_gap":
            return _tool_log_capability_gap(inputs["description"], handle)
        if name == "relay_message":
            return _tool_relay_message(inputs["recipient"], inputs["message"], config)
        return f"Unknown tool: {name}"
    except Exception as e:
        log.error(f"Tool error ({name}): {e}")
        return f"Tool error: {e}"
