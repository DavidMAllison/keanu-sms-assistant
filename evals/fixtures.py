"""
Canned tool responses for eval runs.

Each function returns a realistic string that matches what the real tool returns,
but with static/controlled data so test assertions are predictable.
"""

MEAL_PLAN = """Current date and time: Thursday, April 30, 2026 at 6:00 PM

--- CURRENT WEEK MEAL PLAN ---
WEEKLY MEAL PLAN: April 27 - May 3, 2026

Mon 4/27  Pesto Pantesco with Spaghetti | 45 min
Tue 4/28  Lemon Garlic Chicken Breasts | 25 min
Wed 4/29  Pescado Agridulce (Sweet and Sour Fish) | 40 min
Thu 4/30  Lamb Ragu with Pappardelle | 30 min
Fri 5/1   Breakfast for Dinner - Pancakes | 30 min
Sat 5/2   Beef and Broccoli | 55 min
Sun 5/3   Pork Tenderloin with Roasted Vegetables | 45 min

--- FOOD INVENTORY ---
Frozen - Chicken: 6 Costco chicken breast packages
Pantry Staples: pasta, olive oil, garlic

--- FAMILY PREFERENCES ---
Child1 doesn't like fish.
"""

SCHEDULE = """Practice: Thursday, May 1 — Soccer practice, 5:00–6:30 PM (Miller Park)
Game: Friday, May 2 — Soccer game, 7:00–8:30 PM (Riverside Fields)"""

RECIPE = """Lamb Ragu with Pappardelle

Ingredients:
- 500g ground lamb
- 1 onion, diced
- 3 cloves garlic
- 400g crushed tomatoes
- 250g pappardelle pasta
- Fresh rosemary

Instructions:
1. Brown lamb in a large pan over medium-high heat.
2. Add onion and garlic, cook until soft.
3. Add tomatoes and rosemary, simmer 20 minutes.
4. Cook pasta, toss with ragu, serve.
"""


def get_tool_response(tool_name: str, inputs: dict) -> str:
    """Return a canned response for a given tool call."""
    if tool_name == "get_meal_plan":
        return MEAL_PLAN
    if tool_name == "get_recipe":
        return RECIPE
    if tool_name == "get_schedule":
        return SCHEDULE
    if tool_name in ("log_feedback", "log_preference", "save_recipe_idea",
                     "check_recipe_similarity", "update_meal_plan",
                     "update_inventory", "add_schedule_event", "log_capability_gap"):
        return "OK"
    return f"Unknown tool: {tool_name}"
