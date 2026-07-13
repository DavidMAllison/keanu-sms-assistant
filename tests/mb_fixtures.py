"""
Canned MenuBuilder MCP responses for menu workflow tests.
All data is synthetic — no real recipe names, handles, or dates that could leak PII.
"""

FAKE_CONFIG = {
    "security": {
        "menu_admin": "+10000000001",
        "partner_handle": "+10000000002",
        "allowed_numbers": ["+10000000001", "+10000000002"],
        "kids": ["+10000000003"],
        "handle_to_person": {
            "+10000000001": "Admin",
            "+10000000002": "Partner",
            "+10000000003": "Kid",
        },
    },
}

# ── start_menu_workflow responses ─────────────────────────────────────────────

# All meals have feedback — feedback_queue should be empty after handle_start
START_DEFAULT = {
    "week_start": "2026-07-07",
    "last_week_meals": [
        {"day": "Mon", "name": "Test Pasta", "sms_feedback": "loved it", "times_cooked": 4},
        {"day": "Tue", "name": "Test Tacos", "sms_feedback": "good", "times_cooked": 6},
        {"day": "Wed", "name": "Test Stir Fry", "sms_feedback": "ok", "times_cooked": 2},
    ],
}

# One first-cook meal missing feedback — feedback_queue should have one entry
START_WITH_FIRSTCOOK = {
    "week_start": "2026-07-07",
    "last_week_meals": [
        {"day": "Mon", "name": "Brand New Recipe", "sms_feedback": "", "times_cooked": 0},
        {"day": "Tue", "name": "Test Tacos", "sms_feedback": "good", "times_cooked": 6},
    ],
}

# Known meal (times_cooked > 0) missing feedback — should be silently skipped
START_WITH_KNOWN_NO_FEEDBACK = {
    "week_start": "2026-07-07",
    "last_week_meals": [
        {"day": "Mon", "name": "Known Recipe", "sms_feedback": "", "times_cooked": 3},
        {"day": "Tue", "name": "Test Tacos", "sms_feedback": "good", "times_cooked": 6},
    ],
}

# MCP returned on Sunday — should be bumped to Monday in handle_start
START_SUNDAY_DATE = {
    "week_start": "2026-07-05",  # Sunday
    "last_week_meals": [],
}

START_ERROR = {"error": "MenuBuilder unavailable"}

# ── swap_meal responses ───────────────────────────────────────────────────────

SWAP_SUCCESS = {
    "state": "awaiting_meal_approval",
    "selected_meals": {
        "Mon": "Test Pasta",
        "Tue": "Test Chicken Tikka Masala",
        "Wed": "Test Stir Fry",
    },
    "note": "",
}

SWAP_SUCCESS_WITH_NOTE = {
    "state": "awaiting_meal_approval",
    "selected_meals": {
        "Mon": "Test Pasta",
        "Tue": "Test Chicken Tikka Masala",
    },
    "note": "Swapped to an idea recipe.",
}

SWAP_NEEDS_CONFIRMATION = {
    "status": "needs_confirmation",
    "suggested": "Test Chicken Tikka Masala",
    "message": "Did you mean 'Test Chicken Tikka Masala'?",
}

# ── update_plan_meal responses ────────────────────────────────────────────────

UPDATE_PLAN_SUCCESS = {"success": True, "day": "Thu", "title": "Test Chicken Tacos"}

UPDATE_PLAN_NEEDS_CONFIRMATION = {
    "status": "needs_confirmation",
    "suggested": "Test Chicken Tikka Masala",
    "message": "Did you mean 'Test Chicken Tikka Masala'?",
}

UPDATE_PLAN_ERROR = {
    "success": False,
    "day": "Thu",
    "title": "xyz",
    "error": "Day 'Thu' not found in plan.",
}
