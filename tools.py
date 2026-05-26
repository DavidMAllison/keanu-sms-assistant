"""Tool definitions and implementations for Keanu."""

import json
import logging
import subprocess
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

import sys as _sys
_sys.path.insert(0, "/Users/davidallison/projects/personal/MenuBuilder")
from recipe_agent import run_agent as _recipe_run_agent, search_local_collection as _search_local_collection

from menubuilder_bridge import call_menubuilder_tool as _call_menubuilder_tool

log = logging.getLogger(__name__)

_DAVID_HOME = "/Users/davidallison"


def _search_local_collection_safe(query: str) -> list:
    import os
    orig = os.environ.get("HOME")
    os.environ["HOME"] = _DAVID_HOME
    try:
        results = _search_local_collection(query)
        return [r for r in results if isinstance(r, dict)]
    except Exception:
        return []
    finally:
        if orig is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = orig

_BASE = Path(__file__).parent
GAPS_FILE = _BASE / "capability_gaps.json"
OUTBOX_FILE = _BASE / ".outbox.json"
PENDING_FRIEND_FILE = _BASE / ".pending_friend_requests.json"
INVENTORY_FILE = Path("/Users/Shared/cooking/inventory.md")


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


def send_imessage_group(handles: list, text: str):
    safe_text = text.replace("\\", "\\\\").replace('"', '\\"')
    buddy_refs = ", ".join(f'buddy "{h}"' for h in handles)
    script = f'''
    tell application "Messages"
        set theService to 1st service whose service type = iMessage
        tell theService
            set theChat to make new chat with properties {{participants: {{{buddy_refs}}}}}
        end tell
        send "{safe_text}" to theChat
    end tell
    '''
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f"AppleScript error sending group message to {handles}: {result.stderr.strip()}")
    else:
        log.info(f"Sent group message to {handles}")


def drain_outbox():
    if not OUTBOX_FILE.exists():
        return
    try:
        messages = json.loads(OUTBOX_FILE.read_text())
        OUTBOX_FILE.unlink()
        for entry in messages:
            if "handles" in entry:
                send_imessage_group(entry["handles"], entry["text"])
                log.info(f"Outbox: sent group message to {entry['handles']}")
            else:
                send_imessage(entry["handle"], entry["text"])
                log.info(f"Outbox: sent to {entry['handle']}")
    except Exception as e:
        log.error(f"Outbox drain error: {e}")


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
            "Look up or find a recipe. Use for both lookup ('show me the lamb barbacoa') and "
            "discovery ('find a carnitas recipe', 'suggest something with chicken'). "
            "Searches the local collection first; searches online sources for discovery requests "
            "or when nothing is found locally. Returns recipe text or a link."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Recipe name or search query"},
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
    "check_friend_dinner": {
        "name": "check_friend_dinner",
        "description": (
            "Check if it's OK for a friend to join dinner on a given night. "
            "Checks the family schedule for kid activities and whether the meal has enough servings. "
            "Automatically notifies parents if the schedule is clear."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "ISO date YYYY-MM-DD to check (defaults to today if omitted). Always use the next upcoming occurrence — e.g. if today is Monday and the kid says 'Sunday', use the coming Sunday's date.",
                },
            },
            "required": [],
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
    "get_prep_guide": {
        "name": "get_prep_guide",
        "description": (
            "Returns the authoritative Sunday batch prep list for this week — "
            "tasks, timings, and order pulled directly from the current meal plan. "
            "Call this whenever the user asks what they can prep ahead, what the Sunday prep is, "
            "what to batch cook, or any variation of those questions. "
            "Do not generate prep advice from recipe files or your own reasoning — always use this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "swap_meal": {
        "name": "swap_meal",
        "description": (
            "Swap a meal in the current week's plan. Confirm day + outgoing recipe with the user first. "
            "Pass incoming as a recipe name (existing collection) or a URL (new recipe). "
            "If the result starts with 'fetch_failed', ask the user to paste the recipe content and "
            "call again with incoming_content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "day": {"type": "string", "description": "Day label as shown in the meal plan, e.g. 'Thu 5/21'"},
                "outgoing": {"type": "string", "description": "Exact recipe name being replaced"},
                "incoming": {"type": "string", "description": "Replacement recipe name (from collection) or a URL"},
                "incoming_content": {
                    "type": "object",
                    "description": "Parsed recipe content when URL fetch failed — only set on retry after fetch_failed",
                    "properties": {
                        "title": {"type": "string"},
                        "time": {"type": "string"},
                        "servings": {"type": "string"},
                        "ingredients": {"type": "array", "items": {"type": "string"}},
                        "instructions": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "required": ["day", "outgoing", "incoming"],
        },
    },
}


def build_tool_list(is_kid: bool, is_admin: bool, is_idea_submitter: bool) -> list:
    if is_kid:
        return [_DEFS["get_meal_plan"], _DEFS["get_schedule"], _DEFS["check_friend_dinner"]]
    names = ["get_meal_plan", "get_recipe", "get_schedule", "add_schedule_event",
             "log_feedback", "log_preference", "log_capability_gap", "relay_message",
             "get_prep_guide"]
    if is_idea_submitter:
        names += ["check_recipe_similarity", "save_recipe_idea", "swap_meal"]
    if is_admin:
        names += ["update_meal_plan", "update_inventory"]
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


def _parse_min_servings(s: str) -> Optional[int]:
    if not s:
        return None
    m = re.search(r'(\d+)', s)
    return int(m.group(1)) if m else None


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


def _tool_get_recipe(name: str, handle: str = "") -> str:
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

    # Search local collection first, fall back to external search
    local_results = _search_local_collection_safe(name)
    if local_results:
        if len(local_results) > 1:
            titles = ", ".join(r["title"] for r in local_results)
            return f"Found multiple matches: {titles}. Which one?"
        r = local_results[0]
        title = r.get("title", name)
        url = r.get("url", "")
        matches = find_all_recipe_matches(title)
        if matches:
            text = extract_recipe_text(RECIPES_DIR / matches[0]["filename"])
            if text:
                hint = "Dropbox link if user wants full recipe" if "dropbox.com" in url else "Full recipe"
                suffix = f"\n\n[{hint}: {url}]" if url else ""
                return f"{text}{suffix}\n\n[Found in local collection. Offer to search online for alternatives if wanted.]"
        if url:
            return f"{title}: {url}"

    # Not found locally — search externally
    if handle:
        outbox = json.loads(OUTBOX_FILE.read_text()) if OUTBOX_FILE.exists() else []
        outbox.append({"handle": handle, "text": "Nothing in the local collection — searching online, this takes about a minute..."})
        OUTBOX_FILE.write_text(json.dumps(outbox))
        drain_outbox()
    try:
        results = _recipe_run_agent(name)
        if results:
            lines = [f"Found {len(results)} recipe(s) online:"]
            for r in results[:3]:
                title = r.get("title", "Unknown")
                url = r.get("url", "")
                source = r.get("source", "")
                if url:
                    lines.append(f"- {title} ({source}): {url}" if source else f"- {title}: {url}")
            return "\n".join(lines)
    except Exception as e:
        import traceback
        log.error(f"run_agent error: {e}\n{traceback.format_exc()}")
    return f"No recipe found matching '{name}'."


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


def _notify_parents_individual(kid_handle: str, kid_name: str, meal: str, food_ok, food_reason: str, day_label: str, config: dict) -> None:
    name_to_handle = {v: k for k, v in config.get("security", {}).get("handle_to_person", {}).items()}
    parent_handles = [h for h in [name_to_handle.get("David"), name_to_handle.get("Ashley")] if h]

    if food_ok is True:
        msg = (
            f"{kid_name} wants to bring a friend to dinner on {day_label} ({meal}). "
            f"Schedule's clear and there's plenty of food. Reply yes or no and I'll pass it on!"
        )
    elif food_ok == "tight":
        msg = (
            f"{kid_name} wants to bring a friend to dinner on {day_label} ({meal}). "
            f"Schedule's clear but it {food_reason}. Reply yes or no and I'll pass it on!"
        )
    else:
        msg = (
            f"{kid_name} wants to bring a friend to dinner on {day_label} ({meal}). "
            f"Schedule's clear but I don't have serving info. Reply yes or no and I'll pass it on!"
        )

    outbox = json.loads(OUTBOX_FILE.read_text()) if OUTBOX_FILE.exists() else []
    for h in parent_handles:
        outbox.append({"handle": h, "text": msg})
    OUTBOX_FILE.write_text(json.dumps(outbox))

    import time as _time
    pending = json.loads(PENDING_FRIEND_FILE.read_text()) if PENDING_FRIEND_FILE.exists() else []
    pending = [p for p in pending if p.get("kid_handle") != kid_handle]
    pending.append({
        "kid_handle": kid_handle,
        "kid_name": kid_name,
        "meal": meal,
        "day": day_label,
        "asked_at": _time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    PENDING_FRIEND_FILE.write_text(json.dumps(pending))


def _tool_check_friend_dinner(handle: str, config: dict, date_str: Optional[str] = None) -> str:
    today = date.today()
    check_date = date.fromisoformat(date_str) if date_str else today
    day_name = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][check_date.weekday()]
    day_label = "tonight" if check_date == today else day_name

    handle_map = config.get("security", {}).get("handle_to_person", {})
    kid_name = handle_map.get(handle, "One of the kids")

    schedule = _load_schedule()
    kids = {"Eleanor", "Wren"}
    activities = []

    for entry in schedule.get("weekly_overrides", {}).get(check_date.isoformat(), []):
        if entry.get("person") in kids:
            activities.append(f"{entry['person']} has {entry.get('note', 'an activity')}")

    for entry in schedule.get("standing", {}).get(day_name, []):
        if entry.get("person") in kids:
            activities.append(f"{entry['person']} has {entry.get('activity', 'an activity')}")

    if activities:
        return json.dumps({"can_join": False, "reason": "busy_schedule", "activities": activities})

    meal_result = _find_in_meal_plan(day_name.lower() if check_date != today else "tonight")
    if not meal_result:
        return json.dumps({"can_join": False, "reason": "no_dinner_planned"})

    meal_name, _ = meal_result

    try:
        meta_data = json.loads(METADATA_FILE.read_text()).get("recipes", {})
    except Exception:
        meta_data = {}

    servings_str = ""
    meal_lower = meal_name.lower()
    for rname, meta in meta_data.items():
        if not isinstance(meta, dict):
            continue
        rname_lower = rname.lower()
        if rname_lower == meal_lower:
            servings_str = meta.get("servings", "")
            break
        words = [w for w in meal_lower.split() if len(w) > 3]
        if words and sum(1 for w in words if w in rname_lower) >= max(1, len(words) // 2):
            candidate = meta.get("servings", "")
            if candidate:
                servings_str = candidate
                break

    min_servings = _parse_min_servings(servings_str)
    if min_servings is None:
        food_ok = "unknown"
        food_reason = "no serving info"
    elif min_servings >= 5:
        food_ok = True
        food_reason = f"serves {servings_str}"
    else:
        food_ok = "tight"
        food_reason = f"only serves {servings_str}"

    _notify_parents_individual(handle, kid_name, meal_name, food_ok, food_reason, day_label, config)

    return json.dumps({
        "can_join": food_ok is True,
        "food_ok": food_ok,
        "meal": meal_name,
        "food_reason": food_reason,
        "day": day_label,
        "parents_notified": True,
    })


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


def _tool_swap_meal(day: str, outgoing: str, incoming: str,
                    incoming_content: Optional[dict] = None) -> str:
    import os
    is_url = incoming.startswith("http")
    kwargs = {"day": day, "outgoing_recipe": outgoing}
    if incoming_content:
        kwargs["incoming_content"] = incoming_content
    elif is_url:
        kwargs["incoming_url"] = incoming
    else:
        kwargs["incoming_name"] = incoming

    orig = os.environ.get("HOME")
    os.environ["HOME"] = _DAVID_HOME
    try:
        from meal_swap import execute_swap
        result = execute_swap(**kwargs)
    except Exception as e:
        log.error(f"execute_swap error: {e}", exc_info=True)
        return f"Swap failed: {e}"
    finally:
        if orig is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = orig

    if not result.get("success"):
        if result.get("message") == "fetch_failed":
            return (
                "fetch_failed: Couldn't fetch that recipe automatically. "
                "Ask the user to paste the full recipe content (title, cook time, servings, "
                "ingredients as a list, and instructions), then call swap_meal again with "
                "incoming_content set to {title, time, servings, ingredients: [str], instructions: [str]}."
            )
        return f"Swap failed: {result.get('message', 'unknown error')}"

    return result.get("message", f"Swapped {outgoing} → {incoming} on {day}.")


def _tool_get_prep_guide() -> str:
    result = _call_menubuilder_tool("get_prep_guide")
    if "error" in result:
        log.error(f"get_prep_guide bridge error: {result['error']}")
        return "Sorry, couldn't fetch the prep guide right now."
    return result.get("prep_guide", "No prep guide available for this week.")


def execute_tool(name: str, inputs: dict, handle: str, config: dict) -> str:
    try:
        if name == "get_meal_plan":
            return _tool_get_meal_plan()
        if name == "get_recipe":
            return _tool_get_recipe(inputs["name"], handle)
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
        if name == "check_friend_dinner":
            return _tool_check_friend_dinner(handle, config, inputs.get("date"))
        if name == "relay_message":
            return _tool_relay_message(inputs["recipient"], inputs["message"], config)
        if name == "swap_meal":
            return _tool_swap_meal(inputs["day"], inputs["outgoing"], inputs["incoming"],
                                   inputs.get("incoming_content"))
        if name == "get_prep_guide":
            return _tool_get_prep_guide()
        return f"Unknown tool: {name}"
    except Exception as e:
        log.error(f"Tool error ({name}): {e}")
        return f"Tool error: {e}"
