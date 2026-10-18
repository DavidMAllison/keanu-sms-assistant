"""
Unit tests for server.maybe_capture_ashley_reply.

2026-10-18: week 2026-10-19 got stuck in awaiting_ashley_signoff all day
because the pending-reply block captured whichever message from the
partner handle arrived FIRST, regardless of content — an unrelated
recipe-idea URL consumed the capture before Ashley's real "Looks good"
showed up six hours later. Fixed by classifying before capturing.

Mocks:
  - server.MENU_PENDING_FILE / MENU_RESPONSE_FILE / MENU_SESSION_FILE → temp files
  - server._call_menubuilder_tool  → canned classify_ashley_reply / get_workflow_state replies
  - server.send_imessage / server.queue_outbox → recorded, never sent
  - server.menu_workflow.handle_ashley_reply → recorded, never actually run
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import server
from tests.mb_fixtures import FAKE_CONFIG

_PARTNER = FAKE_CONFIG["security"]["partner_handle"]
_ADMIN = FAKE_CONFIG["security"]["menu_admin"]


class TestMaybeCaptureAshleyReply(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.pending_file = self.tmp / "menu_pending.json"
        self.response_file = self.tmp / "menu_feedback_response.json"
        self.session_file = self.tmp / "menu_session.json"

        self._patches = [
            patch.object(server, "MENU_PENDING_FILE", self.pending_file),
            patch.object(server, "MENU_RESPONSE_FILE", self.response_file),
            patch.object(server, "MENU_SESSION_FILE", self.session_file),
        ]
        for p in self._patches:
            p.start()

        self.pending_file.write_text(json.dumps({
            "sent_at": "2026-10-19T09:00:00",
            "partner_handle": _PARTNER,
        }))

        self.sent = []  # (handle, text)
        self.outbox = []  # (handle, text)
        self.handle_ashley_reply_calls = []

        self._p_send = patch.object(
            server, "send_imessage",
            side_effect=lambda h, t: self.sent.append((h, t)))
        self._p_outbox = patch.object(
            server, "queue_outbox",
            side_effect=lambda entry: self.outbox.append((entry["handle"], entry["text"])))
        self._p_har = patch.object(
            server.menu_workflow, "handle_ashley_reply",
            side_effect=lambda text, session, config: self.handle_ashley_reply_calls.append(text))
        self._p_send.start()
        self._p_outbox.start()
        self._p_har.start()

    def tearDown(self):
        for p in self._patches + [self._p_send, self._p_outbox, self._p_har]:
            p.stop()

    def _run(self, text, bridge_results):
        """bridge_results: dict mapping tool name -> return value for _call_menubuilder_tool."""
        bridge_mock = MagicMock(side_effect=lambda tool, **kw: bridge_results.get(tool))
        with patch.object(server, "_call_menubuilder_tool", bridge_mock):
            return server.maybe_capture_ashley_reply(_PARTNER, text, FAKE_CONFIG)

    # ── Case 1: unrelated URL, classified unclear — the Oct 18 regression ──────

    def test_unrelated_url_classified_unclear_is_not_captured(self):
        result = self._run(
            "https://americastestkitchen.com/recipes/slow-cooker-chicken-tortilla-soup",
            {"classify_ashley_reply": "unclear"},
        )
        self.assertFalse(result)
        self.assertTrue(self.pending_file.exists(), "pending file must survive an unclear reply")
        self.assertFalse(self.response_file.exists())
        self.assertEqual(self.handle_ashley_reply_calls, [])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.outbox, [])

    # ── Case 2: a real approval ──────────────────────────────────────────────

    def test_approval_is_captured_and_dispatched_once(self):
        result = self._run(
            "Looks good",
            {
                "classify_ashley_reply": "approve",
                "get_workflow_state": {"state": "awaiting_ashley_signoff"},
            },
        )
        self.assertTrue(result)
        self.assertFalse(self.pending_file.exists(), "pending file must be consumed on approval")
        self.assertTrue(self.response_file.exists())
        response = json.loads(self.response_file.read_text())
        self.assertEqual(response["message"], "Looks good")
        self.assertEqual(self.handle_ashley_reply_calls, ["Looks good"])
        self.assertEqual(self.sent, [(_PARTNER, "Thanks! Passed it on to David.")])
        self.assertEqual(self.outbox, [(_ADMIN, "Ashley replied to the menu: Looks good")])

    def test_approval_outside_awaiting_signoff_does_not_dispatch_to_workflow(self):
        result = self._run(
            "Looks good",
            {
                "classify_ashley_reply": "approve",
                "get_workflow_state": {"state": "idle"},
            },
        )
        self.assertTrue(result)
        self.assertEqual(self.handle_ashley_reply_calls, [])

    # ── Case 3: bridge failure fails safe ───────────────────────────────────

    def test_bridge_failure_treated_as_unclear_and_kept(self):
        result = self._run(
            "Looks good",
            {"classify_ashley_reply": {"error": "subprocess timed out"}},
        )
        self.assertFalse(result)
        self.assertTrue(self.pending_file.exists())
        self.assertFalse(self.response_file.exists())
        self.assertEqual(self.handle_ashley_reply_calls, [])

    # ── Not Ashley / no pending file ────────────────────────────────────────

    def test_message_from_someone_else_is_ignored(self):
        bridge_mock = MagicMock(return_value="approve")
        with patch.object(server, "_call_menubuilder_tool", bridge_mock):
            result = server.maybe_capture_ashley_reply(_ADMIN, "Looks good", FAKE_CONFIG)
        self.assertFalse(result)
        self.assertTrue(self.pending_file.exists())
        bridge_mock.assert_not_called()

    def test_no_pending_file_is_a_noop(self):
        self.pending_file.unlink()
        bridge_mock = MagicMock(return_value="approve")
        with patch.object(server, "_call_menubuilder_tool", bridge_mock):
            result = server.maybe_capture_ashley_reply(_PARTNER, "Looks good", FAKE_CONFIG)
        self.assertFalse(result)
        bridge_mock.assert_not_called()


class TestChangeRequestRearmsPending(unittest.TestCase):
    """menu_workflow.handle_ashley_reply: the 'change request, menu re-sent' branch
    must recreate the pending file so her reply to the re-sent menu is captured too —
    only the unparseable-reply branch did this before the 2026-10-18 fix."""

    def setUp(self):
        import agents.menu_workflow as menu_workflow
        self.menu_workflow = menu_workflow
        self.tmp = Path(tempfile.mkdtemp())
        self.pending_file = self.tmp / "menu_pending.json"
        self._p_pending = patch.object(menu_workflow, "MENU_PENDING_FILE", self.pending_file)
        self._p_pending.start()
        self._p_outbox = patch.object(menu_workflow, "_send_outbox", MagicMock())
        self._p_outbox.start()

    def tearDown(self):
        self._p_pending.stop()
        self._p_outbox.stop()

    def test_change_request_recreates_pending_file(self):
        bridge_mock = MagicMock(return_value={
            "state": "awaiting_ashley_signoff",
            "parsed": True,
        })
        with patch.object(self.menu_workflow, "call_menubuilder_tool", bridge_mock):
            self.menu_workflow.handle_ashley_reply(
                "Swap Monday for tacos", {}, FAKE_CONFIG)
        self.assertTrue(self.pending_file.exists())
        saved = json.loads(self.pending_file.read_text())
        self.assertEqual(saved["partner_handle"], _PARTNER)


if __name__ == "__main__":
    unittest.main()
