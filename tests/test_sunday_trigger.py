"""
Unit tests for the Sunday menu trigger guard (server.maybe_start_sunday_menu).

Phase 2.1 ownership rules: the trigger must skip when menu_session.json holds
a local sub-state (no bridge call — cheap file read per poll) or when
MenuBuilder reports an active bridge state via get_workflow_state, and fire
otherwise (idle/complete bridge state, no local sub-state).

Mocks:
  - server.date / server.datetime  → fixed Sunday 9:30 AM
  - menu_workflow._load_session    → canned session dicts
  - server._call_menubuilder_tool  → canned bridge responses (no subprocess)
  - menu_workflow.handle_start     → sentinel reply (no API, no session writes)
  - server.queue_outbox / save_state → mocks (no real spool writes, no state file)
"""

import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import server
from tests.mb_fixtures import FAKE_CONFIG

_SUNDAY = date(2026, 7, 19)
_SUNDAY_0930 = datetime(2026, 7, 19, 9, 30)


class TestSundayTriggerGuard(unittest.TestCase):

    def _run(self, session, bridge_result):
        """Run maybe_start_sunday_menu on a fake Sunday 9:30 AM.
        Returns (state, bridge_mock, handle_start_mock)."""
        state = {}
        fake_date = MagicMock()
        fake_date.today.return_value = _SUNDAY
        fake_datetime = MagicMock()
        fake_datetime.now.return_value = _SUNDAY_0930
        bridge_mock = MagicMock(return_value=bridge_result)
        start_mock = MagicMock(return_value="Let's plan the week!")

        with patch.object(server, "date", fake_date), \
             patch.object(server, "datetime", fake_datetime), \
             patch.object(server, "queue_outbox", MagicMock()), \
             patch.object(server, "save_state", MagicMock()), \
             patch.object(server, "_call_menubuilder_tool", bridge_mock), \
             patch.object(server.menu_workflow, "_load_session", return_value=session), \
             patch.object(server.menu_workflow, "handle_start", start_mock):
            server.maybe_start_sunday_menu(state, FAKE_CONFIG)

        return state, bridge_mock, start_mock

    def test_fires_with_empty_session_and_bridge_complete(self):
        state, bridge_mock, start_mock = self._run({}, {"state": "complete"})
        bridge_mock.assert_called_once_with("get_workflow_state")
        start_mock.assert_called_once()
        self.assertEqual(state.get("last_menu_trigger_date"), _SUNDAY.isoformat())

    def test_fires_with_bridge_idle(self):
        state, bridge_mock, start_mock = self._run({}, {"state": "idle"})
        start_mock.assert_called_once()
        self.assertEqual(state.get("last_menu_trigger_date"), _SUNDAY.isoformat())

    def test_skips_local_substate_without_bridge_call(self):
        state, bridge_mock, start_mock = self._run(
            {"state": "awaiting_schedule"}, {"state": "idle"})
        bridge_mock.assert_not_called()
        start_mock.assert_not_called()
        self.assertNotIn("last_menu_trigger_date", state)

    def test_bridge_active_stands_down_for_the_day(self):
        # An in-flight workflow means this week's build is already happening —
        # the skip must set the dedupe date so the trigger doesn't auto-fire a
        # duplicate build after the workflow completes (and doesn't re-run the
        # bridge call every poll for the rest of Sunday).
        state, bridge_mock, start_mock = self._run(
            {}, {"state": "awaiting_ashley_signoff"})
        bridge_mock.assert_called_once_with("get_workflow_state")
        start_mock.assert_not_called()
        self.assertEqual(state.get("last_menu_trigger_date"), _SUNDAY.isoformat())

    def test_fires_when_bridge_errors(self):
        # Bridge failure must not block the trigger — {"error": ...} has no
        # active state, so the workflow start proceeds (handle_start has its
        # own bridge error handling).
        state, bridge_mock, start_mock = self._run({}, {"error": "boom"})
        start_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
