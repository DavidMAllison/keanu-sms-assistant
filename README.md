# Keanu — iMessage Family Assistant

Keanu is an iMessage bot that answers family questions about dinner, weekly schedules, recipes, and more. It runs 24/7 on a dedicated Mac account and responds via a shared Apple ID.

The key design goal: every family member uses the device they already have. No app to install, no account to create, no new tool to learn — just iMessage.

## What it does

- **Meal planning** — what's for dinner, ingredients, recipe details
- **Schedule** — upcoming soccer games, practices, events pulled from a calendar
- **Recipes** — searches local collection first (all statuses, scored by keyword match); falls back to online search (Rick Bayless, Pati Jinich, Smitten Kitchen, Serious Eats, and more) when nothing is found locally; returns GitHub Pages URL for collection hits
- **Fun** — jokes, riddles, trivia (kid-safe mode for younger family members)
- **Feedback** — logs family reactions to meals for future planning
- **Proactive messages** — holiday morning messages, trash reminders, game-day good-luck texts
- **Relay** — admin can ask Keanu to forward a message to another family member
- **Grocery receipts** — send a photo of a receipt and Keanu parses it into the shopping inventory
- **Recipe ideas from photos** — caption a recipe photo with "save this as an idea" and Keanu drops the raw image into the recipe ideas inbox for later review
- **Image vision** — reads images sent via iMessage (HEIC auto-converted to JPEG)
- **Sunday menu trigger** — the polling loop fires the weekly menu workflow on the first poll after 9 AM Sunday (no launchd dependency — if the Mac was asleep at 9:00 it fires as soon as Keanu is back up). An 8:30 AM pre-flight checks API credits, the MenuBuilder bridge, shared-file writability, and the tool contract; it texts the admin only on failure — success is log-only
- **Ashley's weekly lunch** — Saturday 10 AM launchd job sends Ashley 3 lunch suggestions; she replies to pick one; 6 PM nudge if no pick by then; Keanu handles pick and feedback via `set_lunch_pick` and `log_lunch_feedback` (bridge to MenuBuilder)
- **URL-based meal swaps** — Ashley (or David) can send a recipe URL during menu signoff to add a new recipe and schedule it for a specific day; Keanu checks for similar existing recipes and asks which to use if a close match is found
- **Menu workflow cancel confirmation** — cancel intent ("stop", "never mind", "already planned", etc.) during any menu-build state prompts for a yes/no confirmation before cancelling, so an offhand remark doesn't nuke real progress; "no" resumes exactly where the session left off
- **Ashley recipe batch send** — idea submitters can ask Keanu to send Ashley 5 fresh recipe candidates from the idea queue to review; kicks off a queue refresh automatically if the queue is empty
- **Music playlist seeding** — admin can text a song or artist to seed the week's Spotify discovery playlist, or ask Keanu to build the playlist on demand instead of waiting for the scheduled build

## How it works

A Python server polls `chat.db` (iMessage's local SQLite database) every 3 seconds for new messages. Incoming messages are handled by a Claude agent via tool use — Claude decides which tools to call based on the message, rather than keyword routing.

```
iMessage → chat.db → server.py → agent.py → Claude API (tool use) → tools.py → AppleScript → iMessage
```

Outbound proactive messages go through the **outbox spool**: any process queues a message by dropping one JSON file into `/Users/Shared/cooking-state/outbox/` (see `tools.queue_outbox`), and the main loop drains the directory every poll. Unparseable entries are quarantined to `.bad/` instead of wedging the drain.

## Admin keywords

Texts from the menu admin that route to fixed handlers instead of the agent:

- **`start menu`** — kick off the weekly menu workflow by hand (it also fires automatically on Sunday; the workflow refuses to double-start if a build is already active)
- **`menu status`** — one-glance workflow status: local session state, bridge state, last Sunday trigger date, last pre-flight date, and the week being planned. Works mid-workflow.
- **`recycle koala`** — reset a stuck menu session back to normal chat

The agent also has an admin-only `start_menu_workflow` tool, so asking Keanu conversationally to "plan the menu" starts the real workflow instead of improvised meal planning.

## Mac setup

Keanu requires two Mac user accounts:

- **Your main account** — where you edit code and manage config
- **A bot account** — runs the server 24/7, has iMessage logged in as the bot's Apple ID

The code lives in `/Users/Shared/sms-assistant/` so both accounts can read and write it without any deploy step.

### Prerequisites

- macOS with iMessage
- A dedicated Apple ID for the bot (e.g. `mybot@icloud.com`) logged into iMessage on the bot account
- Python 3.9 (macOS Command Line Tools default — `python3 --version` should show 3.9.x).
  sms-assistant intentionally runs on the CLT Python; MenuBuilder and GroceryAgent each
  run in their own newer venv (3.12) and are called via subprocess bridges
  (`menubuilder_bridge.py`, `groceryagent_bridge.py`) rather than imported directly —
  see those files for why. Don't "fix" this by installing Python 3.11 on the bot
  account; it won't be used unless `start.sh` is also repointed to it.
- An [Anthropic API key](https://console.anthropic.com/)

### Installation

1. Clone the repo into `/Users/Shared/sms-assistant/`

2. Install dependencies (run as the bot account):
   ```bash
   pip3 install -r requirements.txt
   python3 -m playwright install chromium
   ```

3. Copy and fill in config:
   ```bash
   cp config/settings.yaml.example config/settings.yaml
   cp .env.example .env
   ```
   Edit `config/settings.yaml` with your family's phone numbers and paths.  
   Edit `.env` with your Anthropic API key.

4. Install the launchd service (run as the bot account) so Keanu starts on login and restarts automatically:
   ```bash
   cp com.keanu.sms-assistant.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.keanu.sms-assistant.plist
   ```

### Restarting

Preferred — self-restart via the HTTP API, no password prompt (runs as the bot account):
```bash
curl -X POST http://localhost:5050/restart
```
launchd's `KeepAlive: true` restarts it automatically after the exit.

Fallback — from your main account (prompts for Mac password):
```bash
osascript -e 'do shell script "kill $(pgrep -u botaccount -f server.py)" with administrator privileges'
```

## Project structure

```
server.py                  # Main loop — polls chat.db, routes messages, sends replies; HTTP API on :5050 (/send, /start_menu_workflow)
agent.py                   # Conversation loop — Claude tool use, per-handle history
tools.py                   # Tool definitions and implementations
trigger_menu.py            # Manual fallback — POSTs to /start_menu_workflow on Keanu's HTTP API (Sunday scheduling is handled in-loop now)
groceryagent_bridge.py     # Subprocess bridge to GroceryAgent receipt parser
menubuilder_bridge.py      # Subprocess bridge to MenuBuilder MCP (Python 3.9→3.12)
agents/
  menu_workflow.py         # Weekly menu workflow — feedback/schedule/cuisine via agent tool-use; all plan/selection/finalization delegates to MenuBuilder MCP; idea activation handles no-URL case (asks for URL first) and existing URL fetch failures (asks for paste)
  menu_agent.py            # Meal plans, recipes, inventory, feedback
system_prompts/
  menu.txt                 # Keanu's main personality prompt
config/
  settings.yaml.example    # Config template — copy to settings.yaml
evals/
  dataset.json             # Eval test cases (fake handles only — no real numbers)
  runner.py                # Eval harness
tests/
  test_menu_workflow.py    # Unit tests for menu workflow routing and state (no API calls)
  test_menu_guardrails.py  # start_menu_workflow tool guard + "menu status" keyword
  test_outbox_spool.py     # Outbox spool queue/drain/quarantine
  test_sunday_trigger.py   # In-loop Sunday trigger guard
  test_tool_contract.py    # Bridge call sites vs live MenuBuilder signatures (also pre-flight check 4)
  test_tools_update.py     # Unit tests for update_meal_plan confirmation flow
  mb_fixtures.py           # Shared fixtures — placeholder handles only
```

## Data files (not in repo)

Keanu reads from a few external data sources you set up separately:

| What | Default path | Format |
|------|-------------|--------|
| Meal plans | `cooking_base/weeklyplan/` | `.txt` files named `mealplan_YYYY-MM-DD.txt` |
| Recipes | `cooking_base/Recipes/` | PDF files |
| Inventory | `cooking_base/inventory.md` | Markdown |
| Condiments | `cooking_base/condiments.json` | JSON |
| Family schedule | `paths.schedule_file` in settings | JSON |

Configure paths in `config/settings.yaml` under the `paths` key.
