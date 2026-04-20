"""
Schedule agent — answers questions about upcoming games and practices.
Reads FamilySchedule/schedule.json. Kids look up their own schedule;
adults can look up any family member by name.
"""

import json
import re
import yaml
from datetime import date, timedelta
from pathlib import Path

def _load_schedule_path() -> Path:
    config_file = Path(__file__).parent.parent / "config/settings.yaml"
    config = yaml.safe_load(config_file.read_text())
    return Path(config.get("paths", {}).get("schedule_file", ""))

SCHEDULE_FILE = _load_schedule_path()

_SCHEDULE_KEYWORDS = {
    "game", "match", "practice", "when is my", "when is", "what time is",
    "next game", "next practice", "do i have", "does she have", "does he have",
    "when do i", "when does",
}

_DAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def is_schedule_request(text: str) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in _SCHEDULE_KEYWORDS)


def _load_schedule() -> dict:
    return json.loads(SCHEDULE_FILE.read_text())


def _next_occurrence(day_name: str, from_date: date) -> date:
    target = _DAY_ORDER.index(day_name)
    delta = (target - from_date.weekday()) % 7
    return from_date + timedelta(days=delta)


def _biweekly_active(entry: dict, target_date: date) -> bool:
    if entry.get("frequency") != "biweekly":
        return True
    match = re.search(r"started\s+(\w+ \d+ \d{4})", entry.get("frequency_note", ""))
    if not match:
        return True
    try:
        from datetime import datetime
        start = datetime.strptime(match.group(1), "%b %d %Y").date()
    except ValueError:
        return True
    return ((target_date - start).days % 14) < 7


def _format_time(t: str) -> str:
    """Convert 24h 'HH:MM' to '3:00 PM'."""
    try:
        h, m = int(t[:2]), int(t[3:])
        suffix = "AM" if h < 12 else "PM"
        h = h % 12 or 12
        return f"{h}:{m:02d} {suffix}"
    except Exception:
        return t


def get_schedule_reply(handle: str, text: str, config: dict) -> str:
    schedule = _load_schedule()
    people = schedule.get("people", [])
    today = date.today()
    lowered = text.lower()

    # Resolve person
    handle_map = config.get("security", {}).get("handle_to_person", {})
    kids = config.get("security", {}).get("kids", [])
    is_kid = handle in kids

    if is_kid:
        person = handle_map.get(handle)
    else:
        # Extract a name from the message
        person = None
        for p in people:
            if p.lower() in lowered:
                person = p
                break

    if not person:
        return "Not sure whose schedule to check — try mentioning a name."

    want_game = any(w in lowered for w in ("game", "match", "tournament"))
    want_practice = "practice" in lowered
    want_any = not want_game and not want_practice

    results = []

    # Games come from weekly_overrides
    if want_game or want_any:
        for date_str in sorted(schedule.get("weekly_overrides", {}).keys()):
            event_date = date.fromisoformat(date_str)
            if event_date < today:
                continue
            for entry in schedule["weekly_overrides"][date_str]:
                if entry.get("person") != person:
                    continue
                note = entry.get("note", "")
                note_lower = note.lower()
                if any(w in note_lower for w in ("game", "match", "tournament", "cup")):
                    results.append(("game", event_date, note))

    # Practices come from standing
    if want_practice or want_any:
        for day_name, entries in schedule.get("standing", {}).items():
            for entry in entries:
                if entry.get("person") != person:
                    continue
                activity = entry.get("activity", "")
                next_date = _next_occurrence(day_name, today)
                # If biweekly and not active this week, advance two weeks
                if not _biweekly_active(entry, next_date):
                    next_date += timedelta(weeks=2)
                start = _format_time(entry.get("start", ""))
                end = _format_time(entry.get("end", ""))
                location = entry.get("note", "").split(" — ")[0] if entry.get("note") else ""
                desc = f"{activity}, {start}–{end}"
                if location:
                    desc += f" ({location})"
                results.append(("practice", next_date, desc))

    if not results:
        what = "game" if want_game else "practice" if want_practice else "event"
        return f"No upcoming {what} found for {person}."

    results.sort(key=lambda x: x[1])
    kind, event_date, desc = results[0]
    day_str = event_date.strftime("%A, %B %-d")

    if is_kid:
        return f"Your next {kind} is {day_str} — {desc}"
    else:
        return f"{person}'s next {kind} is {day_str} — {desc}"
