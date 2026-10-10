"""
kidquiz_bridge.py — Subprocess bridge for calling KidQuiz's quiz generator.

Handles the Python 3.9 (sms-assistant) → 3.12 (KidQuiz) version gap.
Import call_quiz_generator from here — do not redefine it in other modules.
"""

import json
import logging
import subprocess

log = logging.getLogger(__name__)

_KQ_PYTHON = "/Users/davidallison/projects/personal/KidQuiz/.venv/bin/python3"
_KQ_SCRIPT = "/Users/davidallison/projects/personal/KidQuiz/quiz_generator.py"


def call_quiz_generator(image_paths: list, caption: str = "") -> dict:
    """Generate a practice quiz from study-guide photos via subprocess bridge.

    Args:
        image_paths: Absolute paths to JPEG/PNG image files (one per study-guide page).
        caption: Original message text, passed through as context.

    Returns:
        Parsed dict from quiz_generator.py stdout. Keys:
          - Success: {"url", "key_url", "subject", "grade", "title", "email", "reply"}
          - Not study material: {"error": "not study material", "reply": "..."}
          - Bridge error: {"error": "<message>"}
    """
    args = [_KQ_PYTHON, _KQ_SCRIPT, *image_paths]
    if caption:
        args += ["--caption", caption]

    try:
        r = subprocess.run(
            args,
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            stdout_err = r.stdout.strip()
            stderr_err = r.stderr.strip()
            log.error(f"KidQuiz quiz_generator failed (stdout={stdout_err!r}, stderr={stderr_err!r})")
            try:
                parsed = json.loads(stdout_err)
                return parsed if "error" in parsed else {"error": f"quiz_generator exited {r.returncode}"}
            except (json.JSONDecodeError, ValueError):
                return {"error": stderr_err or stdout_err or f"quiz_generator exited {r.returncode}"}
        raw = r.stdout.strip()
        if not raw:
            log.error("KidQuiz quiz_generator returned empty output")
            return {"error": "empty output from quiz_generator"}
        return json.loads(raw)
    except subprocess.TimeoutExpired:
        log.error("KidQuiz quiz_generator timed out after 300s")
        return {"error": "quiz_generator timed out"}
    except json.JSONDecodeError as e:
        log.error(f"KidQuiz quiz_generator returned invalid JSON: {e}\n  raw={raw!r}")
        return {"error": f"invalid JSON from quiz_generator: {e}"}
    except Exception as e:
        log.error(f"KidQuiz bridge error: {e}")
        return {"error": str(e)}
