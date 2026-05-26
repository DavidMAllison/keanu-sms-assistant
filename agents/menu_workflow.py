"""
menu_workflow.py — SMS-triggered weekly menu state machine.

State lives in /Users/Shared/cooking/menu_session.json.
Each SMS from the admin advances one state.
"""

import csv
import io
import json
import logging
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from agents.menu_agent import (
    COOKING_BASE,
    FEEDBACK_CURRENT_FILE,
    IDEAS_DIR,
    METADATA_FILE,
    RECIPES_DIR,
    WEEKLYPLAN_DIR,
)

log = logging.getLogger(__name__)

DRY_RUN = False

MENU_SESSION_FILE = Path("/Users/Shared/cooking/menu_session.json")
OUTBOX_FILE = Path("/Users/Shared/sms-assistant/.outbox.json")
MENUBUILDER_DIR = Path("/Users/davidallison/projects/personal/MenuBuilder")

DAYS_ORDER = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

DAY_NAME_MAP = {
    "monday": "Mon", "tuesday": "Tue", "wednesday": "Wed", "thursday": "Thu",
    "friday": "Fri", "saturday": "Sat", "sunday": "Sun",
    "mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu",
    "fri": "Fri", "sat": "Sat", "sun": "Sun",
}


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

_MB_VENV_PYTHON = "/Users/davidallison/projects/personal/MenuBuilder/.venv/bin/python3.12"
_MB_SERVER_PATH = "/Users/davidallison/projects/personal/MenuBuilder/mcp/menu_server.py"
_MB_PROJECT_PATH = "/Users/davidallison/projects/personal/MenuBuilder"


def call_menubuilder_tool(tool_name: str, **kwargs) -> dict:
    """Call a MenuBuilder MCP tool via subprocess bridge (handles Python 3.9/3.12 gap)."""
    script = f"""
import sys, json
sys.path.insert(0, {repr(_MB_PROJECT_PATH)})
import importlib.util
spec = importlib.util.spec_from_file_location('menu_server', {repr(_MB_SERVER_PATH)})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
result = getattr(mod, {repr(tool_name)})(**{repr(kwargs)})
print(json.dumps(result))
"""
    try:
        r = subprocess.run(
            [_MB_VENV_PYTHON, "-c", script],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            log.error(f"MenuBuilder tool {tool_name} failed: {r.stderr.strip()}")
            return {"error": r.stderr.strip()}
        return json.loads(r.stdout.strip())
    except Exception as e:
        log.error(f"MenuBuilder bridge error ({tool_name}): {e}")
        return {"error": str(e)}


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


# ── Last-week plan parsing ────────────────────────────────────────────────────

_PLAN_LINE_RE = re.compile(
    r"^(Sun|Mon|Tue|Wed|Thu|Fri|Sat)\s+\d+/\d+\s+(.+?)\s*(?:\[|$)"
)


def _parse_last_plan() -> list:
    """Return [{name, day, sms_feedback}] from the most recent mealplan file."""
    if not WEEKLYPLAN_DIR.exists():
        return []

    today = date.today()
    dated = []
    for f in WEEKLYPLAN_DIR.glob("mealplan_*.txt"):
        try:
            d = date.fromisoformat(f.stem.replace("mealplan_", ""))
            dated.append((d, f))
        except ValueError:
            continue

    dated.sort(key=lambda x: x[0], reverse=True)
    plan_file = next((f for d, f in dated if d <= today), None)
    if not plan_file:
        return []

    meals = []
    for line in plan_file.read_text().splitlines():
        m = _PLAN_LINE_RE.match(line.strip())
        if m:
            meals.append({"name": m.group(2).strip(), "day": m.group(1), "sms_feedback": None})
    return meals


def _merge_feedback_into_meals(meals: list) -> list:
    """Overlay feedback_current.json entries onto matching meals."""
    if not FEEDBACK_CURRENT_FILE.exists():
        return meals
    try:
        data = json.loads(FEEDBACK_CURRENT_FILE.read_text())
        for entry in data.get("entries", []):
            recipe = entry.get("recipe", "")
            note = entry.get("note", "")
            sentiment = entry.get("sentiment", "")
            if not recipe or not note:
                continue
            recipe_lower = recipe.lower()
            for meal in meals:
                if recipe_lower in meal["name"].lower() or meal["name"].lower() in recipe_lower:
                    if not meal["sms_feedback"]:
                        meal["sms_feedback"] = f"{sentiment}: {note}" if sentiment else note
                    break
    except Exception as e:
        log.warning(f"Could not merge feedback: {e}")
    return meals


def _format_meal_list(meals: list) -> str:
    lines = []
    for m in meals:
        fb = f" — {m['sms_feedback']}" if m.get("sms_feedback") else ""
        lines.append(f"{m['day']}: {m['name']}{fb}")
    return "\n".join(lines)


# ── Metadata helpers ──────────────────────────────────────────────────────────

def _load_metadata() -> dict:
    if not METADATA_FILE.exists():
        return {}
    try:
        return json.loads(METADATA_FILE.read_text()).get("recipes", {})
    except Exception:
        return {}


def _save_metadata(recipes: dict):
    if DRY_RUN:
        log.info("[DRY_RUN] Would save metadata updates.")
        return
    try:
        raw = json.loads(METADATA_FILE.read_text()) if METADATA_FILE.exists() else {}
        raw["recipes"] = recipes
        raw["last_updated"] = date.today().isoformat()
        METADATA_FILE.write_text(json.dumps(raw, indent=2))
    except Exception as e:
        log.error(f"Could not save metadata: {e}")


def _find_recipe_key(name: str, recipes: dict) -> Optional[str]:
    """Return the best-matching key in recipes dict, or None."""
    name_lower = name.lower()
    for key in recipes:
        if key.lower() == name_lower:
            return key
    for key in recipes:
        words = [w for w in key.lower().split() if len(w) > 3]
        if len(words) >= 2 and sum(1 for w in words if w in name_lower) >= 2:
            return key
    return None


def _update_metadata_for_cooked_meals(meals: list):
    recipes = _load_metadata()
    today_str = date.today().isoformat()
    for meal in meals:
        key = _find_recipe_key(meal["name"], recipes)
        if not key:
            log.warning(f"Could not find '{meal['name']}' in metadata")
            continue
        recipes[key]["times_cooked"] = recipes[key].get("times_cooked", 0) + 1
        recipes[key]["last_cooked_date"] = today_str
        log.info(f"Updated '{key}': times_cooked={recipes[key]['times_cooked']}")
    _save_metadata(recipes)


# ── Meal suggestion / selection ───────────────────────────────────────────────

_CANDIDATE_RE = re.compile(
    r"^\s+- (.+?)(?:\s+\[(?:GRILL|NEW|KID-FRIENDLY|ADULT:[^\]]+)\])*\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+)"
)


def _run_suggest_meals(quick_days: list) -> list:
    """
    Run suggest_meals.py and return parsed candidates.
    Each candidate: {name, cuisine, health, minutes, is_quick, meal_type}
    """
    cmd = [sys.executable, str(MENUBUILDER_DIR / "suggest_meals.py")]
    if quick_days:
        cmd += ["--quick", ",".join(d.lower() for d in quick_days)]

    if DRY_RUN:
        log.info(f"[DRY_RUN] Would run: {' '.join(cmd)}")
        return []

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log.error(f"suggest_meals.py stderr: {result.stderr.strip()}")
            return []
        return _parse_suggest_output(result.stdout)
    except Exception as e:
        log.error(f"Could not run suggest_meals.py: {e}")
        return []


def _parse_suggest_output(output: str) -> list:
    candidates = []
    current_section = ""
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("=== "):
            current_section = stripped.upper()
            continue
        m = _CANDIDATE_RE.match(line)
        if not m:
            continue
        name_raw = m.group(1).strip()
        name = re.sub(r'\s*\[(?:GRILL|NEW|KID-FRIENDLY|ADULT:[^\]]+)\]\s*', '', name_raw).strip()
        cuisine = m.group(2).strip()
        health = m.group(3).strip()
        time_str = m.group(4).strip()

        is_slow = "slow" in time_str.lower()
        minutes = 0 if is_slow else 999
        if not is_slow:
            mins_m = re.search(r'(\d+)\s*min', time_str)
            if mins_m:
                minutes = int(mins_m.group(1))

        is_quick = minutes <= 35 or is_slow
        is_weekend = "WEEKEND" in current_section

        candidates.append({
            "name": name,
            "cuisine": cuisine,
            "health": health,
            "minutes": minutes,
            "time_str": time_str,
            "is_quick": is_quick,
            "meal_type": "Weekend" if is_weekend else "Weeknight",
        })
    return candidates


_PROTEIN_KEYWORDS = [
    ("salmon", "Fish"), ("fish", "Fish"), ("shrimp", "Shrimp"), ("cod", "Fish"),
    ("tilapia", "Fish"), ("pork", "Pork"), ("lamb", "Lamb"), ("beef", "Beef"),
    ("chicken", "Chicken"), ("turkey", "Turkey"), ("tofu", "Vegetarian"),
    ("chickpea", "Vegetarian"), ("mushroom", "Vegetarian"), ("lentil", "Vegetarian"),
    ("bean", "Vegetarian"), ("vegetarian", "Vegetarian"),
    ("pasta", "Pasta"), ("spaghetti", "Pasta"), ("noodle", "Pasta"),
]


def _get_protein(name: str) -> str:
    lower = name.lower()
    for keyword, label in _PROTEIN_KEYWORDS:
        if keyword in lower:
            return label
    return "Other"


def _select_meals(candidates: list, quick_days: list, cuisine_direction: Optional[str], metadata: dict) -> dict:
    """
    Select up to 7 meals for Sun–Sat.
    Returns {day: recipe_name}.
    """
    # Add idea recipes matching cuisine direction as extra candidates
    extra = []
    if cuisine_direction and cuisine_direction.lower() not in ("what we've got", ""):
        c_lower = cuisine_direction.lower()
        for name, meta in metadata.items():
            if meta.get("status") == "idea" and c_lower in meta.get("cuisine", "").lower():
                extra.append({
                    "name": name,
                    "cuisine": meta.get("cuisine", ""),
                    "health": meta.get("health", "Moderate"),
                    "minutes": 30,
                    "time_str": meta.get("time", "30 min"),
                    "is_quick": True,
                    "meal_type": "Weeknight",
                })

    # Prefer cuisine direction at front of pool
    pool = list(candidates) + extra
    if cuisine_direction and cuisine_direction.lower() not in ("what we've got", ""):
        c_lower = cuisine_direction.lower()
        pool.sort(key=lambda c: 0 if c_lower in c.get("cuisine", "").lower() else 1)

    # Deduplicate by name
    seen = set()
    unique_pool = []
    for c in pool:
        if c["name"] not in seen:
            seen.add(c["name"])
            unique_pool.append(c)
    pool = unique_pool

    selected = {}
    used_proteins = set()
    heart_healthy_count = 0
    quick_set = {d.lower() for d in quick_days}

    def pick_for(days_subset, require_quick=False, require_weekend=False):
        nonlocal heart_healthy_count
        for day in days_subset:
            if day in selected:
                continue
            is_quick_day = day.lower() in quick_set or day[:3].lower() in quick_set
            for c in pool:
                if c["name"] in selected.values():
                    continue
                if require_quick and not c["is_quick"]:
                    continue
                if require_weekend and c["meal_type"] != "Weekend":
                    continue
                # Soft protein dedup — only enforce if we have room to be picky
                protein = _get_protein(c["name"])
                if protein in used_proteins and len(pool) > len(days_subset) * 2:
                    continue
                selected[day] = c["name"]
                used_proteins.add(protein)
                if c["health"] == "Heart-Healthy":
                    heart_healthy_count += 1
                break

    # 1. Weekend slots from weekend candidates
    pick_for(["Sat", "Sun"], require_weekend=True)

    # 2. Quick nights
    quick_abbrevs = [a for a in DAYS_ORDER if a.lower() in quick_set or a[:3].lower() in quick_set]
    pick_for(quick_abbrevs, require_quick=True)

    # 3. Fill remaining with any candidate
    pick_for(DAYS_ORDER)

    # 4. Fill any still-empty slots with leftovers (no constraints)
    leftovers = [c for c in pool if c["name"] not in selected.values()]
    for day in DAYS_ORDER:
        if day not in selected and leftovers:
            c = leftovers.pop(0)
            selected[day] = c["name"]

    return selected


def _get_week_start() -> date:
    """Return next Monday (or this Monday if today is Monday)."""
    today = date.today()
    days_ahead = (0 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)


def _day_date_map(week_start: date) -> dict:
    """Map day abbreviation → date for a week starting on Monday."""
    m = {}
    m["Sun"] = week_start - timedelta(days=1)
    for i, day in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]):
        m[day] = week_start + timedelta(days=i)
    return m


def _format_numbered_list(selected: dict, week_start: date, quick_days: Optional[list] = None) -> str:
    """Format selected meals as a numbered list for SMS approval."""
    metadata = _load_metadata()
    day_to_date = _day_date_map(week_start)
    quick_set = {d.lower() for d in (quick_days or [])}
    ordered = [(day, selected[day]) for day in DAYS_ORDER if day in selected]

    lines = []
    for i, (day, name) in enumerate(ordered, 1):
        dt = day_to_date.get(day)
        date_str = dt.strftime("%-m/%-d") if dt else ""
        key = _find_recipe_key(name, metadata)
        time_str = metadata[key].get("time", "") if key else ""

        quick_flag = ""
        if day.lower() in quick_set or day[:3].lower() in quick_set:
            quick_flag = " [quick night]"

        line = f"{i}. {day} {date_str}: {name}"
        if time_str:
            line += f" ({time_str}){quick_flag}"
        else:
            line += quick_flag
        lines.append(line)

    return "\n".join(lines)


def _parse_swap(text: str, selected: dict, week_start: date) -> Optional[dict]:
    """
    Parse a swap instruction like 'swap 3 to pasta' or 'change tuesday to tacos'.
    Returns updated selected dict or None.
    """
    lowered = text.lower()
    ordered = [(day, selected[day]) for day in DAYS_ORDER if day in selected]

    # Numbered swap: "swap 3 to X" / "change 2 with Y"
    num_m = re.search(r'(?:swap|change|replace)\s+(\d+)\s+(?:to|with|for)\s+(.+)', lowered)
    if num_m:
        idx = int(num_m.group(1)) - 1
        new_name = num_m.group(2).strip().rstrip(".").title()
        if 0 <= idx < len(ordered):
            day = ordered[idx][0]
            new_selected = dict(selected)
            new_selected[day] = new_name
            return new_selected

    # Day-named swap: "change tuesday to X"
    for day_name, abbrev in DAY_NAME_MAP.items():
        if day_name in lowered and abbrev in selected:
            day_m = re.search(rf'{re.escape(day_name)}\s+(?:to|with|for)\s+(.+)', lowered)
            if day_m:
                new_name = day_m.group(1).strip().rstrip(".").title()
                new_selected = dict(selected)
                new_selected[abbrev] = new_name
                return new_selected

    return None


def _claude_swap(
    text: str,
    selected: dict,
    cuisine_direction: Optional[str],
    metadata: dict,
) -> Optional[dict]:
    """
    Fall back to Claude (Haiku) when _parse_swap can't read a structured command.
    Interprets natural language feedback, identifies which meals to replace, and
    picks replacements from active metadata (preferring uncooked + cuisine match).
    Returns updated selected dict, or None if no changes can be determined.
    """
    import anthropic as _anthropic

    ordered = [(day, selected[day]) for day in DAYS_ORDER if day in selected]
    meal_list = "\n".join(
        f"{i + 1}. {day}: {name}" for i, (day, name) in enumerate(ordered)
    )

    already_selected = set(selected.values())
    direction_lower = (cuisine_direction or "").lower()

    # Build replacement pool: active, not in use, sorted by least-cooked then cuisine match
    candidates = [
        (name, meta)
        for name, meta in metadata.items()
        if meta.get("status") == "active" and name not in already_selected
    ]

    def _score(item: tuple) -> tuple:
        name, meta = item
        times = meta.get("times_cooked", 0)
        cuisine_bonus = -1 if direction_lower and direction_lower in meta.get("cuisine", "").lower() else 0
        return (times, cuisine_bonus)

    candidates.sort(key=_score)
    candidate_names = [name for name, _ in candidates[:60]]

    try:
        client = _anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": (
                    "A user is reviewing a weekly dinner plan and giving natural language feedback.\n\n"
                    f"Current plan:\n{meal_list}\n\n"
                    f"User feedback: \"{text}\"\n\n"
                    f"Available replacements (in preference order): {json.dumps(candidate_names)}\n\n"
                    "Identify which meals the user wants replaced (by day abbreviation like Mon/Tue/Wed) "
                    "and choose the best replacement from the available list. "
                    "Return JSON array only, no explanation:\n"
                    '[{"day": "Mon", "to": "Replacement Meal Name"}, ...]\n\n'
                    "If the feedback doesn't clearly request any changes, return []."
                ),
            }],
        )
        raw = resp.content[0].text.strip()
        # Strip markdown fences if model wraps output
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)

        swaps = json.loads(raw)
        if not isinstance(swaps, list) or not swaps:
            return None

        new_selected = dict(selected)
        changed = False
        for swap in swaps:
            day = (swap.get("day") or "").strip()
            to_meal = (swap.get("to") or "").strip()
            if day and to_meal and day in new_selected:
                new_selected[day] = to_meal
                changed = True

        return new_selected if changed else None

    except Exception as e:
        log.error(f"Claude natural-language swap error: {e}")
        return None


# ── Plan generation ───────────────────────────────────────────────────────────

def _build_plan_text(selected: dict, week_start: date, schedule_notes: list, config: dict) -> str:
    """Generate the mealplan_*.txt content via Claude (REMINDERS section)."""
    import anthropic as _anthropic

    try:
        mb_config = json.loads((MENUBUILDER_DIR / "config.json").read_text())
        base_url = mb_config.get("github_pages_base_url", "")
    except Exception:
        base_url = ""

    metadata = _load_metadata()
    day_to_date = _day_date_map(week_start)
    ordered = [(day, selected[day]) for day in DAYS_ORDER if day in selected]

    # Build per-meal info
    meals_info = []
    for day, name in ordered:
        key = _find_recipe_key(name, metadata)
        meta = metadata.get(key, {}) if key else {}
        health = meta.get("health", "Moderate")
        time_str = meta.get("time", "?")
        filename = meta.get("filename", "")

        if base_url and filename:
            stem = Path(filename).stem
            url = f"{base_url}/{stem}"
        else:
            url = ""

        dt = day_to_date.get(day)
        date_str = dt.strftime("%-m/%-d") if dt else ""
        meals_info.append({
            "day": day, "date": date_str, "name": name,
            "health": health, "time": time_str, "url": url,
        })

    # Week header range
    week_end = week_start + timedelta(days=5)
    week_start_display = (week_start - timedelta(days=1)).strftime("%B %d")
    week_end_display = week_end.strftime("%B %d, %Y")

    # DINNERS block
    dinners_lines = []
    for m in meals_info:
        dinners_lines.append(f"{m['day']} {m['date']}  {m['name']} [{m['health']}] | {m['time']}")
        if m["url"]:
            dinners_lines.append(f"          {m['url']}")
    dinners_text = "\n".join(dinners_lines)

    # BALANCE line
    health_counts: dict = {}
    for m in meals_info:
        h = m["health"]
        health_counts[h] = health_counts.get(h, 0) + 1
    balance_parts = [f"{v} {k}" for k, v in sorted(health_counts.items())]
    balance_line = "BALANCE: " + ", ".join(balance_parts)

    # Ask Claude to generate REMINDERS
    schedule_context = "\n".join(schedule_notes) if schedule_notes else "No special schedule notes."
    meal_lines = "\n".join(
        f"{m['day']} {m['date']}: {m['name']} ({m['health']}, {m['time']})"
        for m in meals_info
    )

    reminder_prompt = (
        f"Generate the REMINDERS section for this weekly meal plan.\n\n"
        f"Week: {week_start_display} - {week_end_display}\n"
        f"Schedule notes: {schedule_context}\n\n"
        f"Meals:\n{meal_lines}\n\n"
        "Format: One line per day that has a meal, like:\n"
        "- MON: one-line timing/prep note\n\n"
        "Rules:\n"
        "- Only include days that have meals\n"
        "- Include suggested start times for weeknight meals over 30 min\n"
        "- Note schedule constraints from the schedule notes\n"
        "- Note special prep (marinating, chilling, slow cooker setup)\n"
        "- One line per day — these populate calendar events\n\n"
        "Return ONLY the reminder lines, no header."
    )

    try:
        client = _anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{"role": "user", "content": reminder_prompt}],
        )
        reminders = response.content[0].text.strip()
    except Exception as e:
        log.error(f"Claude reminders generation error: {e}")
        reminders = "\n".join(f"- {m['day'].upper()}: {m['name']}" for m in meals_info)

    return (
        f"WEEKLY MEAL PLAN: {week_start_display} - {week_end_display}\n\n"
        f"========================================\n"
        f"DINNERS\n"
        f"========================================\n\n"
        f"{dinners_text}\n\n"
        f"{balance_line}\n\n"
        f"========================================\n"
        f"REMINDERS\n"
        f"========================================\n"
        f"{reminders}"
    )


def _build_shopping_csv(selected: dict) -> str:
    metadata = _load_metadata()
    rows = []
    for name in selected.values():
        key = _find_recipe_key(name, metadata)
        if not key:
            continue
        for ing in metadata[key].get("ingredients", []):
            rows.append({
                "Recipe": name,
                "Item": ing.get("name", ""),
                "Quantity": ing.get("quantity", ""),
                "Unit": ing.get("unit", ""),
                "Category": ing.get("category", ""),
            })

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["Recipe", "Item", "Quantity", "Unit", "Category"])
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def _build_prep_guide(selected: dict) -> str:
    metadata = _load_metadata()
    lines = ["PREP GUIDE:"]
    for name in selected.values():
        key = _find_recipe_key(name, metadata)
        if not key:
            continue
        prep = metadata[key].get("prep_components", [])
        if prep:
            lines.append(f"\n{name}:")
            for p in prep:
                lines.append(f"  - {p}")
    return "\n".join(lines) if len(lines) > 1 else ""


# ── Idea activation ───────────────────────────────────────────────────────────

def _check_ideas_on_menu(selected: dict, metadata: dict) -> list:
    return [
        name for name in selected.values()
        if _find_recipe_key(name, metadata) and
           metadata[_find_recipe_key(name, metadata)].get("status") == "idea"
    ]


def _fetch_and_activate_idea(name: str, metadata: dict) -> bool:
    """Fetch recipe from source URL and create .md file. Returns True on success."""
    key = _find_recipe_key(name, metadata)
    if not key:
        return False

    source_url = metadata[key].get("source_url", "")
    if not source_url:
        return False

    try:
        import requests
        from bs4 import BeautifulSoup

        resp = requests.get(source_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        content = None
        for selector in ["article", ".recipe-card", ".recipe", "main"]:
            el = soup.select_one(selector)
            if el:
                content = el.get_text(separator="\n", strip=True)
                break
        if not content:
            content = soup.get_text(separator="\n", strip=True)

        if not content or len(content) < 100:
            return False

        filename = re.sub(r"[^\w\s-]", "", name).strip().replace(" ", "_") + ".md"
        md_path = RECIPES_DIR / filename
        source_label = metadata[key].get("source", source_url)

        if not DRY_RUN:
            md_path.write_text(
                f"# {name}\n\nAdapted from [{source_label}]({source_url})\n\n{content[:3000]}"
            )
            metadata[key]["status"] = "active"
            metadata[key]["filename"] = filename
            log.info(f"Activated idea '{name}' → {filename}")

        return True

    except Exception as e:
        log.warning(f"Could not fetch '{name}' from {source_url}: {e}")
        return False


# ── handle_finalize ───────────────────────────────────────────────────────────

def handle_finalize(session: dict, config: dict):
    """
    Called after Ashley approves. Not a state — invoked internally.
    Activates idea recipes, generates plan text, writes files, sends prep guide.
    """
    selected = session.get("selected_meals", {})
    week_start_str = session.get("week_start")
    week_start = date.fromisoformat(week_start_str) if week_start_str else _get_week_start()
    schedule_notes = session.get("schedule_notes", [])
    admin_handle = config["security"].get("menu_admin")

    metadata = _load_metadata()

    # 1. Check for ideas on menu
    ideas_on_menu = _check_ideas_on_menu(selected, metadata)
    session["ideas_on_menu"] = ideas_on_menu

    # 2. Activate ideas
    failed_ideas = []
    for name in ideas_on_menu:
        if not _fetch_and_activate_idea(name, metadata):
            failed_ideas.append(name)

    if failed_ideas:
        session["pending_idea"] = failed_ideas[0]
        session["remaining_ideas"] = failed_ideas[1:]
        session["state"] = "awaiting_idea_content"
        _save_session(session)
        if admin_handle:
            _send_outbox(
                admin_handle,
                f"Couldn't fetch '{failed_ideas[0]}' — paste the recipe content and I'll activate it.",
            )
        return

    # 3. Generate plan text
    plan_text = _build_plan_text(selected, week_start, schedule_notes, config)

    # 4. Write plan files
    plan_path = WEEKLYPLAN_DIR / f"mealplan_{week_start.isoformat()}.txt"
    shopping_path = WEEKLYPLAN_DIR / f"shopping_{week_start.isoformat()}.csv"

    if DRY_RUN:
        log.info(f"[DRY_RUN] Would write:\n  {plan_path}\n  {shopping_path}")
    else:
        WEEKLYPLAN_DIR.mkdir(exist_ok=True)
        plan_path.write_text(plan_text)
        shopping_path.write_text(_build_shopping_csv(selected))
        log.info(f"Wrote plan to {plan_path}")

    # 5. Write trigger file (launchd watcher not yet built — notify admin to run manually)
    trigger = Path("/Users/Shared/cooking/.run_apps_trigger")
    if not DRY_RUN:
        trigger.write_text("")

    # 6. Notify admin
    if admin_handle:
        summary_lines = plan_text.split("\n")[:12]
        summary = "\n".join(summary_lines)
        _send_outbox(admin_handle, f"Plan written. Run the apps manually.\n\n{summary}")

        prep = _build_prep_guide(selected)
        if prep:
            _send_outbox(admin_handle, prep)

    # 7. Complete
    session["state"] = "complete"
    _save_session(session)
    log.info("Menu workflow complete.")


# ── State handlers ────────────────────────────────────────────────────────────

def handle_start(config: dict) -> str:
    """idle → awaiting_meal_logging. Delegates to MenuBuilder via bridge."""
    result = call_menubuilder_tool("start_menu_workflow")
    if "error" in result:
        return "Sorry, couldn't start the menu workflow. Check the logs."

    _sync_session_state(result.get("state", "awaiting_meal_logging"))

    meals = result.get("last_week_meals", [])
    meal_list = _format_meal_list(meals)
    if meal_list:
        return (
            f"Last week's meals:\n{meal_list}\n\n"
            "Any feedback to add? Reply 'done' when finished."
        )
    return "Couldn't find last week's plan. Reply 'done' to skip meal logging."


def _handle_meal_logging(text: str, session: dict, config: dict) -> str:
    lowered = text.lower().strip()

    if "done" in lowered:
        meals = session.get("last_week_meals", [])
        _update_metadata_for_cooked_meals(meals)
        if not DRY_RUN:
            FEEDBACK_CURRENT_FILE.write_text(json.dumps({"entries": []}, indent=2))

        session["state"] = "awaiting_schedule"
        _save_session(session)
        return (
            "Any schedule changes this week? Busy nights, games, nights out? "
            "Reply 'no changes' if all good."
        )

    # Append feedback to the best-matching meal
    meals = session.get("last_week_meals", [])
    matched = False
    for meal in meals:
        words = [w for w in meal["name"].lower().split() if len(w) > 3]
        if words and any(w in lowered for w in words):
            existing = meal.get("sms_feedback") or ""
            meal["sms_feedback"] = (existing + " " + text).strip()
            matched = True
            break

    if not matched and meals:
        existing = meals[-1].get("sms_feedback") or ""
        meals[-1]["sms_feedback"] = (existing + " " + text).strip()

    session["last_week_meals"] = meals
    _save_session(session)

    meal_list = _format_meal_list(meals)
    return f"Got it. Updated:\n{meal_list}\n\nAnything else? Reply 'done' when finished."


def _handle_schedule(text: str, session: dict, config: dict) -> str:
    lowered = text.lower()
    notes = session.get("schedule_notes", [])

    if "no changes" not in lowered and "no change" not in lowered:
        notes.append(text)

    session["schedule_notes"] = notes
    session["state"] = "awaiting_cuisine"
    _save_session(session)
    return "What are you feeling this week? Mexican / Italian / Asian / Indian, or 'what we’ve got'?"


def _handle_cuisine(text: str, session: dict, config: dict) -> str:
    session["cuisine_direction"] = text.strip()

    # Parse quick days from schedule notes
    quick_days = []
    for note in session.get("schedule_notes", []):
        note_lower = note.lower()
        quick_signals = ("game", "practice", "busy", "quick", "early", "tournament")
        if any(s in note_lower for s in quick_signals):
            for day_name, abbrev in DAY_NAME_MAP.items():
                if day_name in note_lower and abbrev not in quick_days:
                    quick_days.append(abbrev)

    session["quick_days"] = quick_days

    candidates = _run_suggest_meals(quick_days)

    # Fall back to metadata if subprocess failed
    if not candidates:
        metadata = _load_metadata()
        for name, meta in metadata.items():
            if meta.get("status") == "active":
                candidates.append({
                    "name": name,
                    "cuisine": meta.get("cuisine", ""),
                    "health": meta.get("health", "Moderate"),
                    "minutes": 30,
                    "time_str": meta.get("time", "30 min"),
                    "is_quick": True,
                    "meal_type": meta.get("meal_type", "Weeknight"),
                })

    metadata = _load_metadata()
    selected = _select_meals(candidates, quick_days, session["cuisine_direction"], metadata)

    session["selected_meals"] = selected
    session["state"] = "awaiting_meal_approval"
    _save_session(session)

    week_start = date.fromisoformat(session["week_start"])
    return _format_numbered_list(selected, week_start, quick_days)


def _handle_meal_approval(text: str, session: dict, config: dict) -> str:
    lowered = text.lower().strip()
    approval = ("looks good", "good", "ok", "okay", "approved", "go ahead", "perfect",
                "great", "sounds good", "yes", "yep", "yeah")

    if any(lowered == p or lowered.startswith(p + " ") for p in approval):
        selected = session.get("selected_meals", {})
        week_start = date.fromisoformat(session["week_start"])
        day_to_date = _day_date_map(week_start)

        meals_json = []
        for day in DAYS_ORDER:
            if day in selected:
                dt = day_to_date.get(day)
                date_str = dt.strftime("%-m/%-d") if dt else ""
                meals_json.append({"day": f"{day} {date_str}", "recipe": selected[day]})

        cmd = [
            sys.executable, str(MENUBUILDER_DIR / "send_menu_partner.py"),
            "--meals", json.dumps(meals_json),
        ]
        if DRY_RUN:
            log.info(f"[DRY_RUN] Would run send_menu_partner.py with {len(meals_json)} meals")
        else:
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                if result.returncode != 0:
                    log.error(f"send_menu_partner.py: {result.stderr.strip()}")
            except Exception as e:
                log.error(f"Could not run send_menu_partner.py: {e}")

        session["state"] = "awaiting_ashley_signoff"
        _save_session(session)
        return "Sent to Ashley."

    # Try structured swap first ("swap 3 to X", "change tuesday to X")
    selected = session.get("selected_meals", {})
    week_start = date.fromisoformat(session["week_start"])
    new_selected = _parse_swap(text, selected, week_start)

    if new_selected:
        session["selected_meals"] = new_selected
        _save_session(session)
        quick_days = session.get("quick_days", [])
        return _format_numbered_list(new_selected, week_start, quick_days)

    # Fall back to Claude for natural language ("we've had X too much", "just had Y", etc.)
    metadata = _load_metadata()
    new_selected = _claude_swap(text, selected, session.get("cuisine_direction"), metadata)

    if new_selected:
        session["selected_meals"] = new_selected
        _save_session(session)
        quick_days = session.get("quick_days", [])
        return _format_numbered_list(new_selected, week_start, quick_days)

    # Can't parse — show current list with hints
    quick_days = session.get("quick_days", [])
    current_list = _format_numbered_list(selected, week_start, quick_days)
    return (
        "Say 'looks good' to approve, or just tell me what to change — e.g. 'swap 3 to pasta', "
        "'change tuesday to tacos', or 'we've had X too much'.\n\n"
        + current_list
    )


def handle_ashley_reply(text: str, session: dict, config: dict):
    """
    Called from server.py after Ashley replies while awaiting_ashley_signoff.
    Delegates to MenuBuilder via bridge.
    """
    admin_handle = config["security"].get("menu_admin")
    result = call_menubuilder_tool("handle_ashley_reply", reply=text)
    new_state = result.get("state", "")
    _sync_session_state(new_state)

    if new_state == "complete":
        if admin_handle:
            _send_outbox(admin_handle, "Plan written, apps launched.")
    elif new_state == "awaiting_idea_activation":
        pending_ideas = result.get("pending_ideas", [])
        if admin_handle and pending_ideas:
            _send_outbox(admin_handle, f"Couldn't fetch '{pending_ideas[0]}' — paste the recipe content and I'll activate it.")
    elif new_state == "awaiting_ashley_signoff":
        # Ashley requested a change — MenuBuilder re-sent the updated menu
        if admin_handle:
            _send_outbox(admin_handle, f"Ashley requested a change: '{text}'. Updated and re-sent.")
    else:
        if admin_handle:
            _send_outbox(admin_handle, f"Ashley replied but something went wrong. Handle manually: '{text}'")


def _handle_idea_content(text: str, session: dict, config: dict) -> str:
    """awaiting_idea_content — admin pasted recipe text for an idea that couldn't be fetched."""
    pending = session.get("pending_idea", "")
    if pending:
        metadata = _load_metadata()
        key = _find_recipe_key(pending, metadata)
        if key:
            filename = re.sub(r"[^\w\s-]", "", pending).strip().replace(" ", "_") + ".md"
            md_path = RECIPES_DIR / filename
            if not DRY_RUN:
                md_path.write_text(f"# {pending}\n\n{text}")
                metadata[key]["status"] = "active"
                metadata[key]["filename"] = filename
                _save_metadata(metadata)
            log.info(f"Activated idea '{pending}' from pasted content.")

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


# ── Main dispatch ─────────────────────────────────────────────────────────────

def dispatch(text: str, session: dict, config: dict) -> str:
    """Route to the correct MenuBuilder tool based on current workflow state."""
    # State is source-of-truth from MenuBuilder, not the local session file
    state_result = call_menubuilder_tool("get_workflow_state")
    state = state_result.get("state", "idle")

    if state in ("idle", "complete", None):
        return ""

    if state == "awaiting_meal_logging":
        result = call_menubuilder_tool("log_meal_feedback", feedback=text)
        _sync_session_state(result.get("state", state))
        if result.get("state") == "awaiting_suggestions":
            return "What are you feeling this week? Mediterranean / Asian / Mexican / surprise me?"
        meals = result.get("last_week_meals", [])
        meal_list = _format_meal_list(meals)
        return f"Got it. Updated:\n{meal_list}\n\nAnything else? Reply 'done' when finished."

    if state == "awaiting_suggestions":
        result = call_menubuilder_tool("get_meal_suggestions", cuisine_direction=text, constraints="")
        _sync_session_state(result.get("state", state))
        selected = result.get("selected_meals", {})
        week_start = date.fromisoformat(result.get("week_start", date.today().isoformat()))
        return _format_numbered_list(selected, week_start)

    if state == "awaiting_meal_approval":
        lowered = text.lower().strip()
        approval = ("looks good", "good", "ok", "okay", "approved", "go ahead", "perfect",
                    "great", "sounds good", "yes", "yep", "yeah")
        if any(lowered == p or lowered.startswith(p + " ") for p in approval):
            result = call_menubuilder_tool("approve_menu")
            _sync_session_state(result.get("state", "awaiting_ashley_signoff"))
            return "Sent to Ashley."
        else:
            result = call_menubuilder_tool("swap_meal", reason=text)
            _sync_session_state(result.get("state", state))
            selected = result.get("selected_meals", {})
            week_start = date.fromisoformat(result.get("week_start", date.today().isoformat()))
            if selected:
                return _format_numbered_list(selected, week_start)
            return (
                "Say 'looks good' to approve, or tell me what to change — "
                "e.g. 'swap 3 to pasta', 'change tuesday to tacos', or 'we've had X too much'.\n\n"
                + _format_numbered_list(state_result.get("selected_meals", {}), date.today())
            )

    if state == "awaiting_ashley_signoff":
        # Ashley's reply is handled separately via handle_ashley_reply() in server.py
        # David texting during this state gets a status update
        return "Waiting on Ashley's OK. I'll let you know when she replies."

    if state == "awaiting_idea_activation":
        pending = state_result.get("pending_idea", "")
        result = call_menubuilder_tool("activate_idea_recipe", name=pending, content=text)
        _sync_session_state(result.get("state", state))
        if result.get("remaining_pending", 0) == 0:
            call_menubuilder_tool("finalize_plan")
            _sync_session_state("complete")
            return "Thanks! Finishing up the plan..."
        next_idea = result.get("next_pending", "")
        return f"Got it! Now paste the content for '{next_idea}'."

    if state == "awaiting_finalization":
        result = call_menubuilder_tool("finalize_plan")
        _sync_session_state("complete")
        return "Plan ready!"

    log.warning(f"Unknown menu workflow state from MenuBuilder: {state}")
    return ""
