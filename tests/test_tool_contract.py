"""
Contract test between sms-assistant call sites and MenuBuilder MCP signatures.

Signature drift between the two repos has shipped silently before (the 6/8
"log_meal_feedback() got an unexpected keyword argument 'meal_name'" failure).
This test makes drift fail loudly, naming the exact call site.

Half 1 — AST-parse every .py in the repo root and agents/ for
call_menubuilder_tool(...) calls (including server.py's _call_menubuilder_tool
alias) with a literal string tool name. Records literal kwarg names; calls
using a **splat are recorded as existence-only checks.

Half 2 — subprocess the MenuBuilder venv python, load menu_server.py the same
way the bridge's _BRIDGE_SCRIPT does, and dump inspect.signature for every
module-level function (the bridge resolves tools via getattr(mod, name), so
module-level functions ARE the contract).

Deliberately NOT validated: the _build_menu_tools() agent schemas —
_execute_menu_tool translates that layer, so its schemas legitimately differ
from MCP signatures.

Also run by the Sunday pre-flight (server._preflight_failures check 4), so
drift introduced mid-week is caught at 8:30 Sunday before the menu run.
"""

import ast
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from menubuilder_bridge import _MB_SERVER_PATH, _MB_VENV_PYTHON

REPO_ROOT = Path(__file__).parent.parent

# server.py imports the bridge as _call_menubuilder_tool — same contract
_BRIDGE_CALL_NAMES = ("call_menubuilder_tool", "_call_menubuilder_tool")

_SIGNATURE_SCRIPT = """
import importlib.util, inspect, json, sys
spec = importlib.util.spec_from_file_location('menu_server', {server_path!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
out = {{}}
for name, fn in vars(mod).items():
    if not inspect.isfunction(fn) or fn.__module__ != 'menu_server':
        continue
    params, required, has_var_kw = [], [], False
    for p in inspect.signature(fn).parameters.values():
        if p.kind == p.VAR_KEYWORD:
            has_var_kw = True
            continue
        if p.kind == p.VAR_POSITIONAL:
            continue
        params.append(p.name)
        if p.default is p.empty:
            required.append(p.name)
    out[name] = {{"params": params, "required": required, "has_var_kw": has_var_kw}}
print(json.dumps(out))
""".format(server_path=_MB_SERVER_PATH)


def _collect_call_sites():
    """Return [(relpath, lineno, tool_name, kwarg_names, has_splat)] for every
    bridge call with a literal string tool name in repo root and agents/."""
    sites = []
    files = sorted(REPO_ROOT.glob("*.py")) + sorted((REPO_ROOT / "agents").glob("*.py"))
    for path in files:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            else:
                continue
            if name not in _BRIDGE_CALL_NAMES:
                continue
            if (not node.args or not isinstance(node.args[0], ast.Constant)
                    or not isinstance(node.args[0].value, str)):
                continue  # tool name is a variable — signature unknowable here
            tool = node.args[0].value
            kwargs = [kw.arg for kw in node.keywords if kw.arg is not None]
            has_splat = any(kw.arg is None for kw in node.keywords)
            sites.append((path.relative_to(REPO_ROOT), node.lineno, tool, kwargs, has_splat))
    return sites


class TestToolContract(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        r = subprocess.run(
            [_MB_VENV_PYTHON, "-c", _SIGNATURE_SCRIPT],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"could not load MenuBuilder signatures: {r.stderr.strip()[-500:]}")
        cls.signatures = json.loads(r.stdout)
        cls.sites = _collect_call_sites()

    def test_collector_finds_call_sites(self):
        # If this drops to zero the contract test is vacuous — most likely the
        # bridge import name or alias changed. Update _BRIDGE_CALL_NAMES.
        self.assertGreaterEqual(len(self.sites), 10,
                                f"only found {len(self.sites)} bridge call sites")

    def test_call_sites_match_menubuilder_signatures(self):
        problems = []
        for relpath, lineno, tool, kwargs, has_splat in self.sites:
            site = f"{relpath}:{lineno} {tool}("
            sig = self.signatures.get(tool)
            if sig is None:
                problems.append(f"{site}...) — no such module-level function in menu_server.py")
                continue
            for kw in kwargs:
                if kw not in sig["params"] and not sig["has_var_kw"]:
                    problems.append(
                        f"{site}{kw}=...) — kwarg not accepted; params are {sig['params']}")
            if not has_splat:  # splat may carry the required params — unknowable
                for req in sig["required"]:
                    if req not in kwargs:
                        problems.append(f"{site}...) — required param '{req}' not supplied")
        self.assertEqual([], problems,
                         "\ntool contract violations:\n  " + "\n  ".join(problems))


if __name__ == "__main__":
    unittest.main(verbosity=2)
