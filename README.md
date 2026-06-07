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
- **Sunday menu trigger** — launchd job fires at 9 AM Sunday to kick off the weekly menu workflow automatically

## How it works

A Python server polls `chat.db` (iMessage's local SQLite database) every 3 seconds for new messages. Incoming messages are handled by a Claude agent via tool use — Claude decides which tools to call based on the message, rather than keyword routing.

```
iMessage → chat.db → server.py → agent.py → Claude API (tool use) → tools.py → AppleScript → iMessage
```

## Mac setup

Keanu requires two Mac user accounts:

- **Your main account** — where you edit code and manage config
- **A bot account** — runs the server 24/7, has iMessage logged in as the bot's Apple ID

The code lives in `/Users/Shared/sms-assistant/` so both accounts can read and write it without any deploy step.

### Prerequisites

- macOS with iMessage
- A dedicated Apple ID for the bot (e.g. `mybot@icloud.com`) logged into iMessage on the bot account
- Python 3.11+ installed on the bot account (via Homebrew: `brew install python@3.11`)
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

From your main account (prompts for Mac password):
```bash
osascript -e 'do shell script "kill $(pgrep -u botaccount -f server.py)" with administrator privileges'
```
launchd's `KeepAlive: true` restarts it automatically after the kill.

## Project structure

```
server.py                  # Main loop — polls chat.db, routes messages, sends replies; HTTP API on :5050
agent.py                   # Conversation loop — Claude tool use, per-handle history
tools.py                   # Tool definitions and implementations
trigger_menu.py            # Sunday 9 AM launchd entry point — fires handle_start() and sends opening SMS
groceryagent_bridge.py     # Subprocess bridge to GroceryAgent receipt parser
menubuilder_bridge.py      # Subprocess bridge to MenuBuilder MCP (Python 3.9→3.12)
agents/
  menu_workflow.py         # Weekly menu workflow — local phase (feedback/schedule/cuisine) + MenuBuilder MCP bridge
  menu_agent.py            # Meal plans, recipes, inventory, feedback
system_prompts/
  menu.txt                 # Keanu's main personality prompt
config/
  settings.yaml.example    # Config template — copy to settings.yaml
evals/
  dataset.json             # Eval test cases (fake handles only — no real numbers)
  runner.py                # Eval harness
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
