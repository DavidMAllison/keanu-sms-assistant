#!/usr/bin/env python3
"""
trigger_menu.py — Sunday 9 AM launchd entry point.

Called by com.menubuilder.sundaymenu.plist. Calls handle_start() directly
and sends the opening message to the admin via Keanu's HTTP API.
Does not go through the SMS routing layer.
"""
import json
import logging
import sys
import urllib.request
import urllib.error
from pathlib import Path

# Add sms-assistant to path
sys.path.insert(0, str(Path(__file__).parent))

import yaml
from agents import menu_workflow

logging.basicConfig(
    filename="/Users/Shared/sms-assistant/trigger_menu.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def load_config() -> dict:
    settings_path = Path("/Users/Shared/sms-assistant/config/settings.yaml")
    with open(settings_path) as f:
        return yaml.safe_load(f)


def send_via_keanu(handle: str, text: str, port: int = 5050) -> bool:
    payload = json.dumps({"handle": handle, "text": text}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/send",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            log.info(f"Sent opening message to {handle}, status={resp.status}")
            return True
    except urllib.error.URLError as e:
        log.error(f"Could not reach Keanu at port {port}: {e}")
        return False


def main():
    log.info("Sunday menu trigger fired")
    try:
        config = load_config()
        admin_handle = config["security"].get("menu_admin")
        if not admin_handle:
            log.error("No menu_admin in config — aborting")
            return

        reply = menu_workflow.handle_start(config)
        log.info(f"handle_start returned: {reply[:80]}")

        if not send_via_keanu(admin_handle, reply):
            log.error("Failed to send opening message — Keanu may not be running")

    except Exception as e:
        log.exception(f"Trigger failed: {e}")


if __name__ == "__main__":
    main()
