# Keanu — iMessage Family Assistant

Keanu is an iMessage bot that answers family questions about dinner, weekly schedules, recipes, and more. It runs 24/7 on a dedicated Mac account and responds via a shared Apple ID.

## What it does

- **Meal planning** — what's for dinner, ingredients, recipe details
- **Schedule** — upcoming soccer games, practices, events pulled from a calendar
- **Recipes** — fetches full recipes from a local PDF library or Dropbox
- **Fun** — jokes, riddles, trivia (kid-safe mode for younger family members)
- **Feedback** — logs family reactions to meals for future planning

## How it works

A Python server polls `chat.db` (iMessage's local SQLite database) every 3 seconds for new messages. Incoming messages are routed to the appropriate agent (menu, schedule, fun) based on keyword detection. Agents call the Anthropic API (Claude) and reply via AppleScript.

```
iMessage → chat.db → server.py → route → agent → Claude API → AppleScript → iMessage
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
server.py                  # Main loop — polls chat.db, routes messages, sends replies
agents/
  menu_agent.py            # Meal plans, recipes, inventory, feedback
  schedule_agent.py        # Games, practices, upcoming events
  fun_agent.py             # Jokes, riddles, trivia
system_prompts/
  menu.txt                 # Keanu's main personality prompt
  fun.txt                  # Prompt for fun/jokes mode
config/
  settings.yaml.example    # Config template — copy to settings.yaml
```

## Data files (not in repo)

Keanu reads from a few external data sources you set up separately:

| What | Default path | Format |
|------|-------------|--------|
| Meal plans | `cooking_base/weeklyplan/` | `.txt` files named `mealplan_YYYY-MM-DD.txt` |
| Recipes | `cooking_base/Recipes/` | PDF files |
| Inventory | `cooking_base/inventory.md` | Markdown |
| Family schedule | `paths.schedule_file` in settings | JSON |

Configure paths in `config/settings.yaml` under the `paths` key.
