"""
menubuilder_bridge.py — Subprocess bridge for calling MenuBuilder MCP tools.

Handles the Python 3.9 (sms-assistant) → 3.12 (MenuBuilder venv) version gap.
Import call_menubuilder_tool from here — do not redefine it in other modules.
"""

import json
import logging
import subprocess

log = logging.getLogger(__name__)

_MB_VENV_PYTHON = "/Users/davidallison/projects/personal/MenuBuilder/.venv/bin/python3.12"
_MB_SERVER_PATH = "/Users/davidallison/projects/personal/MenuBuilder/mcp/menu_server.py"
_MB_PROJECT_PATH = "/Users/davidallison/projects/personal/MenuBuilder"


_BRIDGE_SCRIPT = f"""
import sys, json
sys.path.insert(0, {repr(_MB_PROJECT_PATH)})
import importlib.util
spec = importlib.util.spec_from_file_location('menu_server', {repr(_MB_SERVER_PATH)})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
payload = json.loads(sys.stdin.read())
result = getattr(mod, payload['tool'])(**payload['kwargs'])
print(json.dumps(result))
"""


def call_menubuilder_tool(tool_name: str, **kwargs) -> dict:
    """Call a MenuBuilder MCP tool via subprocess bridge (handles Python 3.9/3.12 gap).

    Passes tool name and kwargs via stdin to avoid ARG_MAX limits for large payloads
    such as base64-encoded images.
    """
    try:
        r = subprocess.run(
            [_MB_VENV_PYTHON, "-c", _BRIDGE_SCRIPT],
            input=json.dumps({"tool": tool_name, "kwargs": kwargs}),
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            log.error(f"MenuBuilder tool {tool_name} failed: {r.stderr.strip()}")
            return {"error": r.stderr.strip()}
        return json.loads(r.stdout.strip())
    except Exception as e:
        log.error(f"MenuBuilder bridge error ({tool_name}): {e}")
        return {"error": str(e)}
