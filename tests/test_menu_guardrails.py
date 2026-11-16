"""
Unit tests for the Phase 3 guardrails.

  - start_menu_workflow tool (tools._tool_start_menu_workflow via execute_tool):
    must refuse without calling handle_start when a local sub-state or an
    active bridge state exists, delegate verbatim otherwise, and be exposed
    to the admin only.
  - "menu status" keyword (server.maybe_handle_menu_status): admin-only exact
    match; must reply mid-workflow and fall through for everyone else.

Mocks: bridge, session loader, handle_start, send_imessage — no subprocess,
no API calls, no real file writes.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import agents.menu_workflow as mw
import server
import tools
from tests.mb_fixtures import FAKE_CONFIG

_ADMIN = FAKE_CONFIG["security"]["menu_admin"]
_PARTNER = FAKE_CONFIG["security"]["partner_handle"]


class TestStartMenuWorkflowTool(unittest.TestCase):

    def _run(self, session, bridge_result, start_reply="Let's plan the week of Jul 20–26."):
        """Execute the tool with a canned session + bridge state.
        Returns (result, bridge_mock, handle_start_mock)."""
        bridge_mock = MagicMock(return_value=bridge_result)
        start_mock = MagicMock(return_value=start_reply)
        with patch.object(mw, "_load_session", return_value=session), \
             patch.object(mw, "handle_start", start_mock), \
             patch.object(tools, "_call_menubuilder_tool", bridge_mock):
            result = tools.execute_tool("start_menu_workflow", {}, _ADMIN, FAKE_CONFIG)
        return result, bridge_mock, start_mock

    def test_local_substate_refuses_without_bridge_call(self):
        result, bridge, start = self._run({"state": "awaiting_schedule"}, {"state": "idle"})
        self.assertEqual(result, tools._MENU_BUILD_IN_PROGRESS)
        bridge.assert_not_called()
        start.assert_not_called()

    def test_active_bridge_state_refuses(self):
        result, bridge, start = self._run({}, {"state": "awaiting_partner_signoff"})
        self.assertEqual(result, tools._MENU_BUILD_IN_PROGRESS)
        start.assert_not_called()

    def test_idle_delegates_to_handle_start_verbatim(self):
        result, bridge, start = self._run({}, {"state": "idle"})
        self.assertEqual(result, "Let's plan the week of Jul 20–26.")
        start.assert_called_once_with(FAKE_CONFIG)

    def test_bridge_error_still_delegates(self):
        # A broken get_workflow_state must not block a manual start —
        # handle_start surfaces its own bridge failure to the user.
        result, bridge, start = self._run({}, {"error": "bridge exploded"})
        start.assert_called_once_with(FAKE_CONFIG)

    def test_exposed_only_to_admin(self):
        admin = [t["name"] for t in tools.build_tool_list(False, True, True)]
        non_admin = [t["name"] for t in tools.build_tool_list(False, False, True)]
        kid = [t["name"] for t in tools.build_tool_list(True, False, False)]
        self.assertIn("start_menu_workflow", admin)
        self.assertNotIn("start_menu_workflow", non_admin)
        self.assertNotIn("start_menu_workflow", kid)


class TestMenuStatusKeyword(unittest.TestCase):

    def _run(self, text, handle, session=None, bridge_result=None, state=None):
        """Run maybe_handle_menu_status with canned session/bridge/state.
        Returns (handled, sent) where sent is a list of (handle, text)."""
        sent = []
        bridge_mock = MagicMock(return_value=bridge_result or {"state": "idle"})
        with patch.object(server.menu_workflow, "_load_session", return_value=session or {}), \
             patch.object(server, "_call_menubuilder_tool", bridge_mock), \
             patch.object(server, "send_imessage",
                          side_effect=lambda h, t: sent.append((h, t))):
            handled = server.maybe_handle_menu_status(text, handle, FAKE_CONFIG, state or {})
        return handled, sent

    def test_admin_mid_workflow_gets_status(self):
        handled, sent = self._run(
            "menu status", _ADMIN,
            session={"state": "awaiting_schedule", "week_start": "2026-07-20"},
            bridge_result={"state": "idle"},
            state={"last_menu_trigger_date": "2026-07-12",
                   "last_preflight_date": "2026-07-12"},
        )
        self.assertTrue(handled)
        self.assertEqual(len(sent), 1)
        to_handle, text = sent[0]
        self.assertEqual(to_handle, _ADMIN)
        self.assertIn("awaiting_schedule", text)
        self.assertIn("2026-07-12", text)
        self.assertIn("2026-07-20", text)

    def test_non_admin_falls_through(self):
        handled, sent = self._run("menu status", _PARTNER)
        self.assertFalse(handled)
        self.assertEqual(sent, [])

    def test_non_exact_text_falls_through(self):
        handled, sent = self._run("what's the menu status looking like", _ADMIN)
        self.assertFalse(handled)
        self.assertEqual(sent, [])

    def test_case_and_whitespace_insensitive(self):
        handled, sent = self._run("  Menu Status ", _ADMIN)
        self.assertTrue(handled)
        self.assertEqual(len(sent), 1)

    def test_empty_state_reads_as_none_and_never(self):
        handled, sent = self._run("menu status", _ADMIN)
        self.assertTrue(handled)
        text = sent[0][1]
        self.assertIn("Local session state: none", text)
        self.assertIn("Last Sunday trigger: never", text)
        self.assertIn("Last pre-flight: never", text)
        self.assertNotIn("Planning week of", text)

    def test_bridge_error_reported_not_raised(self):
        handled, sent = self._run("menu status", _ADMIN,
                                  bridge_result={"error": "bridge exploded"})
        self.assertTrue(handled)
        self.assertIn("bridge exploded", sent[0][1])


class TestReplanKeyword(unittest.TestCase):
    def test_matches_explicit_replan(self):
        self.assertTrue(server.is_menu_replan("replan"))
        self.assertTrue(server.is_menu_replan("  Replan for next week"))

    def test_ignores_other_mentions(self):
        self.assertFalse(server.is_menu_replan("start menu"))
        self.assertFalse(server.is_menu_replan("should we replan?"))
        self.assertFalse(server.is_menu_replan("replanning"))


class TestStaleMenuSession(unittest.TestCase):
    """A local session left over from a week that was then built from the
    MenuBuilder terminal must not keep routing the admin into the workflow."""

    def setUp(self):
        import json
        import tempfile
        self.json = json
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session_file = Path(self.tmp.name) / "menu_session.json"

    def _run(self, session, bridge_result):
        self.session_file.write_text(self.json.dumps(session))
        with patch.object(server, "MENU_SESSION_FILE", self.session_file), \
             patch.object(server, "_call_menubuilder_tool", MagicMock(return_value=bridge_result)):
            return server.load_menu_routing_state()

    def test_week_mismatch_discards_session(self):
        session, local, bridge = self._run(
            {"state": "awaiting_meal_logging", "week_start": "2026-11-09"},
            {"state": "complete", "week_start": "2026-11-16"})
        self.assertEqual((session, local, bridge), ({}, "", "complete"))
        self.assertEqual(self.json.loads(self.session_file.read_text()), {})

    def test_same_week_keeps_session_and_blank_bridge_state(self):
        session, local, bridge = self._run(
            {"state": "awaiting_meal_logging", "week_start": "2026-11-16"},
            {"state": "awaiting_partner_signoff", "week_start": "2026-11-16"})
        self.assertEqual(local, "awaiting_meal_logging")
        self.assertEqual(bridge, "")
        self.assertEqual(session["week_start"], "2026-11-16")

    def test_bridge_error_keeps_session(self):
        session, local, bridge = self._run(
            {"state": "awaiting_meal_logging", "week_start": "2026-11-09"},
            {"error": "bridge exploded"})
        self.assertEqual(local, "awaiting_meal_logging")
        self.assertIn("state", self.json.loads(self.session_file.read_text()))

    def test_no_session_uses_bridge_state(self):
        session, local, bridge = self._run({}, {"state": "awaiting_finalization",
                                                "week_start": "2026-11-16"})
        self.assertEqual((local, bridge), ("", "awaiting_finalization"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
