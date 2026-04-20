import json
import os
import re
import yaml
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pypdf

def _load_paths() -> dict:
    config_file = Path(__file__).parent.parent / "config/settings.yaml"
    config = yaml.safe_load(config_file.read_text())
    return config.get("paths", {})

_paths = _load_paths()

COOKING_BASE = Path(_paths.get("cooking_base", "/Users/Shared/cooking"))
RECIPES_DIR = COOKING_BASE / "Recipes"
WEEKLYPLAN_DIR = COOKING_BASE / "weeklyplan"
IDEAS_DIR = COOKING_BASE / "recipeideas"
INVENTORY_FILE = COOKING_BASE / "inventory.md"
METADATA_FILE = COOKING_BASE / "recipe_metadata.json"
SYSTEM_PROMPT_FILE = Path(__file__).parent.parent / "system_prompts/menu.txt"
DROPBOX_RECIPES_BASE = _paths.get("dropbox_recipes_url", "")

DAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "tonight": None, "today": None, "tomorrow": None,
}


def load_system_prompt() -> str:
    return SYSTEM_PROMPT_FILE.read_text()


def load_context() -> str:
    """Build a context string from today's meal plan, metadata summary, and inventory."""
    sections = []

    # Today's date and current time
    from datetime import datetime
    now = datetime.now()
    today = now.date()
    sections.append(f"Current date and time: {now.strftime('%A, %B %d, %Y at %-I:%M %p')}")

    # Current week's meal plan
    plan = _load_current_meal_plan(today)
    if plan:
        sections.append(f"\n--- CURRENT WEEK MEAL PLAN ---\n{plan}")
    else:
        sections.append("\n--- CURRENT WEEK MEAL PLAN ---\nNo meal plan found for this week.")

    # Recipe metadata (names and cuisine types only — keep context lean)
    metadata_summary = _load_recipe_metadata_summary()
    if metadata_summary:
        sections.append(f"\n--- RECIPE COLLECTION SUMMARY ---\n{metadata_summary}")

    # Inventory
    inventory = _load_inventory()
    if inventory:
        sections.append(f"\n--- FOOD INVENTORY ---\n{inventory}")

    return "\n".join(sections)


def _load_current_meal_plan(today: date) -> Optional[str]:
    """Load the active meal plan, plus the next upcoming plan if one exists."""
    if not WEEKLYPLAN_DIR.exists():
        return None

    dated = []
    for f in WEEKLYPLAN_DIR.glob("mealplan_*.txt"):
        try:
            file_date = date.fromisoformat(f.stem.replace("mealplan_", ""))
            dated.append((file_date, f))
        except ValueError:
            continue
    dated.sort(key=lambda x: x[0])

    def read_plan(f):
        return f.read_text()

    current = next((f for d, f in reversed(dated) if d <= today), None)
    upcoming = next((f for d, f in dated if d > today), None)

    parts = []
    if current:
        parts.append(read_plan(current))
    if upcoming:
        parts.append(f"--- NEXT WEEK ---\n{read_plan(upcoming)}")
    return "\n\n".join(parts) if parts else None


def _load_recipe_metadata_summary() -> Optional[str]:
    """Load a compact summary of the recipe collection."""
    if not METADATA_FILE.exists():
        return None

    try:
        data = json.loads(METADATA_FILE.read_text())
        lines = []
        for name, meta in data.items():
            if isinstance(meta, dict) and meta.get("status") == "active":
                cuisine = meta.get("cuisine_type", "")
                timing = meta.get("meal_timing", "")
                lines.append(f"- {name} ({cuisine}, {timing})")
        return "\n".join(lines) if lines else None
    except (json.JSONDecodeError, Exception):
        return None


def _load_inventory() -> Optional[str]:
    if not INVENTORY_FILE.exists():
        return None
    return INVENTORY_FILE.read_text()


def get_recipe_content(recipe_name: str) -> Optional[str]:
    """
    Return recipe content for the given name.
    Checks metadata ingredients first; falls back to PDF extraction.
    """
    # Try metadata first
    if METADATA_FILE.exists():
        try:
            data = json.loads(METADATA_FILE.read_text())
            recipes = data.get("recipes", {})
            recipe_name_lower = recipe_name.lower()
            for name, meta in recipes.items():
                if recipe_name_lower in name.lower() or recipe_name_lower in meta.get("filename", "").lower().replace("_", " "):
                    ingredients = meta.get("ingredients")
                    if ingredients:
                        lines = [f"{name} - Ingredients:"]
                        for ing in ingredients:
                            qty = ing.get("quantity", "")
                            unit = ing.get("unit", "")
                            ing_name = ing.get("name", "")
                            lines.append(f"  {qty} {unit} {ing_name}".strip())
                        return "\n".join(lines)
        except Exception:
            pass

    # Fall back to PDF
    if not RECIPES_DIR.exists():
        return None

    recipe_name_lower = recipe_name.lower()
    for f in RECIPES_DIR.iterdir():
        if recipe_name_lower in f.name.lower().replace("_", " "):
            try:
                reader = pypdf.PdfReader(str(f))
                text = "\n".join(page.extract_text() or "" for page in reader.pages)
                return text.strip() or None
            except Exception:
                return None
    return None


def get_recipe_content_for_day(target_date: date) -> Optional[str]:
    """Parse the meal plan for target_date and return that recipe's content."""
    plan_text = _load_current_meal_plan(target_date)
    if not plan_text:
        return None

    day_str = target_date.strftime("%-m/%-d")
    for line in plan_text.splitlines():
        if day_str in line and "[" in line:
            parts = line.split(day_str, 1)
            if len(parts) < 2:
                continue
            recipe_name = parts[1].split("[")[0].strip()
            if recipe_name:
                return get_recipe_content(recipe_name)
    return None


def find_recipe_for_message(message: str) -> Optional[str]:
    """
    Return recipe content relevant to the message.
    Checks for day references first, then falls back to recipe name keywords.
    """
    from datetime import timedelta
    message_lower = message.lower()

    if "tomorrow" in message_lower:
        content = get_recipe_content_for_day(date.today() + timedelta(days=1))
        if content:
            return content

    if any(w in message_lower for w in ("tonight", "today", "dinner", "ingredients")):
        content = get_recipe_content_for_day(date.today())
        if content:
            return content

    # Fall back to matching recipe name keywords in the message
    if not RECIPES_DIR.exists():
        return None

    for f in RECIPES_DIR.iterdir():
        candidate = f.stem.replace("_", " ").lower()
        words = [w for w in candidate.split() if len(w) > 3]
        if len(words) >= 2 and sum(1 for w in words if w in message_lower) >= 2:
            try:
                reader = pypdf.PdfReader(str(f))
                text = "\n".join(page.extract_text() or "" for page in reader.pages)
                return text.strip() or None
            except Exception:
                return None
    return None


RECIPE_CHUNK_CHARS = 800


def find_all_recipe_matches(message: str) -> list[dict]:
    """Return list of {name, filename} for all recipes matching message keywords."""
    results = []
    msg_lower = message.lower()
    if not METADATA_FILE.exists():
        return results
    try:
        data = json.loads(METADATA_FILE.read_text())
        for name, meta in data.items():
            if not isinstance(meta, dict):
                continue
            name_lower = name.lower()
            words = [w for w in name_lower.split() if len(w) > 3]
            if len(words) >= 2 and sum(1 for w in words if w in msg_lower) >= 2:
                filename = meta.get("filename", "")
                if filename and (RECIPES_DIR / filename).exists():
                    results.append({"name": name, "filename": filename})
    except Exception:
        pass
    return results


def extract_recipe_text(pdf_path: Path) -> Optional[str]:
    try:
        reader = pypdf.PdfReader(str(pdf_path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        return text.strip() or None
    except Exception:
        return None


def split_into_chunks(text: str, max_chars: int = RECIPE_CHUNK_CHARS) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks = []
    lines = text.splitlines()
    current: list[str] = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1
        if current_len + line_len > max_chars and current:
            chunks.append("\n".join(current))
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def get_dropbox_preview_url(filename: str) -> str:
    return f"{DROPBOX_RECIPES_BASE}?preview={filename}"


def save_recipe_idea(idea_text: str) -> bool:
    """Save a recipe idea to the recipeideas folder. Returns True on success."""
    try:
        IDEAS_DIR.mkdir(exist_ok=True)
        timestamp = date.today().strftime("%Y-%m-%d")
        short = re.sub(r"[^\w\s-]", "", idea_text[:40]).strip().replace(" ", "_")
        filename = IDEAS_DIR / f"{timestamp}_{short}.txt"
        filename.write_text(idea_text)
        return True
    except Exception:
        return False


def is_recipe_idea(message: str) -> bool:
    """Detect if the message is submitting a recipe idea."""
    lowered = message.lower()
    return any(p in lowered for p in (
        "recipe idea", "add idea", "save idea", "idea for", "have you tried",
        "we should make", "can we make", "add to ideas", "put this in ideas",
    ))


def is_menu_change(message: str) -> bool:
    """Detect if the message is trying to change the meal plan."""
    lowered = message.lower()
    return any(p in lowered for p in (
        "change the menu", "update the menu", "change tonight", "change tomorrow",
        "change monday", "change tuesday", "change wednesday", "change thursday",
        "change friday", "change saturday", "change sunday",
        "swap tonight", "swap tomorrow", "swap monday", "swap tuesday",
        "swap wednesday", "swap thursday", "swap friday", "swap saturday", "swap sunday",
    ))


def update_meal_plan(message: str) -> Optional[str]:
    """
    Parse a menu change request and update the meal plan file.
    Returns the new recipe name on success, None on failure.
    """
    today = date.today()
    lowered = message.lower()

    # Determine target date
    target_date = today
    if "tomorrow" in lowered:
        target_date = today + timedelta(days=1)
    else:
        for day_name, weekday in DAY_NAMES.items():
            if weekday is not None and day_name in lowered:
                days_ahead = (weekday - today.weekday()) % 7
                target_date = today + timedelta(days=days_ahead)
                break

    # Find current meal plan file
    plan_files = sorted(WEEKLYPLAN_DIR.glob("mealplan_*.txt"), reverse=True)
    plan_file = None
    for f in plan_files:
        try:
            file_date = date.fromisoformat(f.stem.replace("mealplan_", ""))
            if file_date <= today:
                plan_file = f
                break
        except ValueError:
            continue

    if not plan_file:
        return None

    # Extract new recipe name — text after "to", "for", "with"
    match = re.search(
        r"(?:change|swap|switch)\s+\w+\s+(?:to|for|with)\s+(.+)",
        message, re.IGNORECASE
    )
    if not match:
        match = re.search(
            r"(?:tonight|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+(?:to\s+)?(.+)",
            message, re.IGNORECASE
        )
    if not match:
        return None

    new_recipe = match.group(1).strip().rstrip(".")

    # Update the matching line in the plan file
    day_str = target_date.strftime("%-m/%-d")
    lines = plan_file.read_text().splitlines()
    updated = False
    for i, line in enumerate(lines):
        if day_str in line and "[" in line:
            prefix = line.split(day_str)[0] + day_str
            lines[i] = f"{prefix}  {new_recipe}"
            updated = True
            break

    if updated:
        plan_file.write_text("\n".join(lines))
        return new_recipe
    return None


# ── Meal feedback ─────────────────────────────────────────────────────────────

HANDLE_TO_NAME = {
    "+15132950588": "David",
    "+15132528285": "Ashley",
    "theallisonfamilia@gmail.com": "Eleanor",
    "wren.allison@icloud.com": "Wren",
}

# Past-tense verbs are strong standalone feedback signals.
# Adjectives need a past-tense context word to avoid false positives
# like "sounds good for dinner" or "great choice for tonight".
_POSITIVE_VERBS = {"loved", "liked", "enjoyed"}
_POSITIVE_ADJECTIVES = {"delicious", "amazing", "great", "brilliant", "fantastic",
                        "tasty", "yummy", "hit", "winner", "good", "favourite", "favorite"}
_NEGATIVE_VERBS = {"hated"}
_NEGATIVE_PHRASES = {"didn't like", "didnt like", "didn't enjoy", "didnt enjoy",
                     "wasn't great", "not good", "too spicy", "too salty", "too much",
                     "too little", "awful", "terrible", "bad", "nasty", "bland", "gross"}
_PAST_CONTEXT = {"was", "were", "had", "ate", "tasted", "turned out", "came out"}


def _word_in(word: str, text: str) -> bool:
    return bool(re.search(r'\b' + re.escape(word) + r'\b', text))


def detect_feedback(message: str) -> Optional[str]:
    """Returns 'positive', 'negative', or None."""
    lowered = message.lower()
    has_past = any(_word_in(w, lowered) for w in _PAST_CONTEXT)

    has_positive = (
        any(_word_in(w, lowered) for w in _POSITIVE_VERBS)
        or (has_past and any(_word_in(w, lowered) for w in _POSITIVE_ADJECTIVES))
    )
    has_negative = (
        any(_word_in(w, lowered) for w in _NEGATIVE_VERBS)
        or any(p in lowered for p in _NEGATIVE_PHRASES)
    )

    if not has_positive and not has_negative:
        return None
    return "negative" if has_negative else "positive"


def has_feedback_reason(message: str) -> bool:
    """True if the message seems to contain an explanation."""
    lowered = message.lower()
    reason_words = ("because", "since", "the sauce", "the meat", "the chicken", "the pasta",
                    "the flavour", "the flavor", "too ", "not enough", "kids", "everyone",
                    "texture", "spice", "salty", "sweet", "dry", "wet", "rich", "heavy", "light")
    return any(w in lowered for w in reason_words)


def guess_recipe_from_context(message: str) -> Optional[str]:
    """Try to identify the recipe being discussed — from message text or meal plan."""
    # Check if a recipe name is mentioned directly
    if METADATA_FILE.exists():
        try:
            data = json.loads(METADATA_FILE.read_text())
            msg_lower = message.lower()
            for name in data.get("recipes", {}):
                words = [w for w in name.lower().split() if len(w) > 3]
                if len(words) >= 2 and sum(1 for w in words if w in msg_lower) >= 2:
                    return name
        except Exception:
            pass

    # Determine which day they're referring to
    today = date.today()
    lowered = message.lower()
    if "last night" in lowered or "yesterday" in lowered:
        target = today - timedelta(days=1)
    else:
        target = today

    plan_text = _load_current_meal_plan(target)
    if plan_text:
        day_str = target.strftime("%-m/%-d")
        for line in plan_text.splitlines():
            if day_str in line and "[" in line:
                parts = line.split(day_str, 1)
                if len(parts) >= 2:
                    return parts[1].split("[")[0].strip()
    return None


def _get_current_feedback_file() -> Optional[Path]:
    """Find this week's feedback JSON file."""
    files = sorted(WEEKLYPLAN_DIR.glob("mealplan_*_feedback.json"), reverse=True)
    today = date.today()
    for f in files:
        try:
            week_start = date.fromisoformat(f.stem.replace("mealplan_", "").replace("_feedback", ""))
            if week_start <= today:
                return f
        except ValueError:
            continue
    return None


def parse_per_person_feedback(message: str, handle: str) -> list:
    """
    Use Claude to extract per-person reactions from a feedback message.
    Returns a list of {person, sentiment, note} dicts.
    Sentiment values: liked, disliked, mixed.
    """
    import anthropic as _anthropic
    import os as _os

    sender_name = HANDLE_TO_NAME.get(handle, handle)
    family = ", ".join(HANDLE_TO_NAME.values())

    prompt = f"""Extract individual family member reactions from this meal feedback message.
Family members: {family}
The person sending this message is: {sender_name}
Message: "{message}"

Return a JSON array of objects with exactly these fields:
- person: first name (use "{sender_name}" for "I" or "me")
- sentiment: one of "liked", "disliked", "mixed"
- note: brief specific observation (empty string if none)

Only include people explicitly mentioned. Return only the JSON array, no other text."""

    import logging as _logging
    _log = _logging.getLogger(__name__)

    try:
        client = _anthropic.Anthropic(api_key=_os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        _log.info(f"parse_per_person_feedback raw: {raw}")
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        entries = json.loads(raw)
        today = date.today().isoformat()
        for e in entries:
            e["date"] = today
        return entries
    except Exception as e:
        _log.error(f"parse_per_person_feedback failed: {e}")
        # Fallback: single entry for the sender
        return [{
            "date": date.today().isoformat(),
            "person": HANDLE_TO_NAME.get(handle, handle),
            "sentiment": "liked",
            "note": message,
        }]


def save_feedback(recipe_name: str, entries: list) -> bool:
    """
    Append per-person feedback entries to this week's feedback JSON.
    Matches by recipe name; adds as unplanned entry if not found.
    """
    feedback_file = _get_current_feedback_file()
    if not feedback_file:
        return False

    try:
        data = json.loads(feedback_file.read_text())
        meals = data.get("meals", {})

        matched_day = next(
            (day for day, meal in meals.items()
             if meal.get("recipe", "").lower() == recipe_name.lower()),
            None
        )

        if matched_day:
            meals[matched_day].setdefault("feedback", []).extend(entries)
        else:
            data.setdefault("unplanned", []).append({
                "recipe": recipe_name,
                "feedback": entries,
            })

        feedback_file.write_text(json.dumps(data, indent=2))
        return True
    except Exception:
        return False
