#!/usr/bin/env python3
"""
trigger_menu.py — Sunday 9 AM launchd entry point.

Called by com.menubuilder.sundaymenu.plist as the davidallison user.
POSTs to Keanu's HTTP API so all file writes happen inside the allisonbot
process, which owns the relevant files.
"""
import json
import logging
import urllib.request
import urllib.error

logging.basicConfig(
    filename="/Users/Shared/sms-assistant/trigger_menu.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def trigger_via_keanu(port: int = 5050) -> bool:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/start_menu_workflow",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info(f"Menu workflow triggered via Keanu API, status={resp.status}")
            return True
    except urllib.error.URLError as e:
        log.error(f"Could not reach Keanu at port {port}: {e}")
        return False


def main():
    log.info("Sunday menu trigger fired")
    if not trigger_via_keanu():
        log.error("Failed to trigger menu workflow — Keanu may not be running")


if __name__ == "__main__":
    main()
