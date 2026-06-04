"""
groceryagent_bridge.py — Subprocess bridge for calling GroceryAgent's receipt parser.

Handles the Python 3.9 (sms-assistant) → 3.12 (GroceryAgent) version gap.
Import call_receipt_parser from here — do not redefine it in other modules.
"""

import json
import logging
import subprocess

log = logging.getLogger(__name__)

_GA_PYTHON = "/Users/davidallison/projects/personal/GroceryAgent/.venv/bin/python3"
_GA_PARSER_PATH = "/Users/davidallison/projects/personal/GroceryAgent/receipt_parser.py"


def call_receipt_parser(image_path: str) -> dict:
    """Parse a grocery receipt image via subprocess bridge (handles Python 3.9/3.12 gap).

    Args:
        image_path: Absolute path to a JPEG/PNG image file.

    Returns:
        Parsed dict from receipt_parser.py stdout. Keys:
          - Success: {"receipt": {...}, "budget": {...}, "reply": "..."}
          - Not a receipt: {"error": "not a receipt", "reply": "..."}
          - Bridge error: {"error": "<message>"}
    """
    try:
        r = subprocess.run(
            [_GA_PYTHON, _GA_PARSER_PATH, image_path],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            log.error(f"GroceryAgent receipt_parser failed: {r.stderr.strip()}")
            return {"error": r.stderr.strip() or f"receipt_parser exited {r.returncode}"}
        raw = r.stdout.strip()
        if not raw:
            log.error("GroceryAgent receipt_parser returned empty output")
            return {"error": "empty output from receipt_parser"}
        return json.loads(raw)
    except subprocess.TimeoutExpired:
        log.error("GroceryAgent receipt_parser timed out after 60s")
        return {"error": "receipt_parser timed out"}
    except json.JSONDecodeError as e:
        log.error(f"GroceryAgent receipt_parser returned invalid JSON: {e}")
        return {"error": f"invalid JSON from receipt_parser: {e}"}
    except Exception as e:
        log.error(f"GroceryAgent bridge error: {e}")
        return {"error": str(e)}
