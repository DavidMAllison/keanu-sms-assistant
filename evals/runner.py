"""
Eval runner for Keanu tool selection.

Loads test cases from dataset.json, mocks execute_tool so no real files are
touched, runs get_reply() for each case, then checks the recorded tool calls
against expected_calls.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

# Add parent dir to path so we can import agent.py
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import get_reply
from evals.fixtures import get_tool_response

# ── Fake config (no real PII, no real handles) ────────────────────────────────

FAKE_CONFIG = {
    "agents": {"menu": {"enabled": True}},
    "security": {
        "allowed_numbers": ["+10000000001", "+10000000002", "+10000000003"],
        "menu_admin": "+10000000001",
        "idea_submitters": ["+10000000001", "+10000000002"],
        "kids": ["+10000000003"],
        "rate_limit_per_hour": 100,
        "handle_to_person": {
            "+10000000001": "Admin",
            "+10000000002": "Parent",
            "+10000000003": "Child1",
        },
    },
}

# Map dataset handles to fake handles so we preserve persona (admin/kid/etc)
# without using real phone numbers in the test run
HANDLE_MAP = {
    "+15132950588": "+10000000001",  # admin
    "+15132528285": "+10000000002",  # idea submitter
    "theallisonfamilia@gmail.com": "+10000000003",  # kid
    "wren.allison@icloud.com": "+10000000003",
}


# ── Assertion helpers ─────────────────────────────────────────────────────────

def _check_arg(actual_value: str, expected_value) -> bool:
    """Check a single argument value. Supports exact and contains matching."""
    if isinstance(expected_value, dict) and "contains" in expected_value:
        return expected_value["contains"].lower() in str(actual_value).lower()
    return str(actual_value) == str(expected_value)


def _check_call(actual: dict, expected: dict) -> tuple[bool, str]:
    """Check one recorded call against one expected call spec."""
    if actual["tool"] != expected["tool"]:
        return False, f"expected tool={expected['tool']!r}, got {actual['tool']!r}"

    for arg_name, expected_val in expected.get("args", {}).items():
        actual_val = actual["inputs"].get(arg_name)
        if actual_val is None:
            return False, f"tool={expected['tool']!r} missing arg {arg_name!r}"
        if not _check_arg(actual_val, expected_val):
            return False, f"tool={expected['tool']!r} arg {arg_name!r}: expected {expected_val!r}, got {actual_val!r}"

    return True, "ok"


def evaluate(actual_calls: list, expected_calls: list) -> tuple[bool, str]:
    """Compare the full recorded call list against the expected call list."""
    if len(actual_calls) != len(expected_calls):
        actual_names = [c["tool"] for c in actual_calls]
        expected_names = [c["tool"] for c in expected_calls]
        return False, f"expected {len(expected_calls)} call(s) {expected_names}, got {len(actual_calls)} {actual_names}"

    for i, (actual, expected) in enumerate(zip(actual_calls, expected_calls)):
        ok, reason = _check_call(actual, expected)
        if not ok:
            return False, f"call[{i}]: {reason}"

    return True, "ok"


# ── Runner ────────────────────────────────────────────────────────────────────

def run_case(case: dict) -> dict:
    recorded_calls = []

    def mock_execute_tool(name: str, inputs: dict, handle: str, config: dict) -> str:
        recorded_calls.append({"tool": name, "inputs": inputs})
        return get_tool_response(name, inputs)

    handle = HANDLE_MAP.get(case["handle"], case["handle"])

    with patch("agent.execute_tool", side_effect=mock_execute_tool):
        try:
            from agent import _conversation_history
            _conversation_history.clear()
            get_reply(handle, case["message"], FAKE_CONFIG)
        except Exception as e:
            return {"id": case["id"], "passed": False, "reason": f"exception: {e}"}

    passed, reason = evaluate(recorded_calls, case["expected_calls"])
    return {"id": case["id"], "passed": passed, "reason": reason, "calls": recorded_calls}


def main():
    dataset_path = Path(__file__).parent / "dataset.json"
    cases = json.loads(dataset_path.read_text())

    results = [run_case(c) for c in cases]

    passed = sum(r["passed"] for r in results)
    total = len(results)

    print(f"\nResults: {passed}/{total} passed\n")
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"  [{status}] {r['id']}")
        if not r["passed"]:
            print(f"         {r['reason']}")
            actual_names = [c["tool"] for c in r.get("calls", [])]
            if actual_names:
                print(f"         actual calls: {actual_names}")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
