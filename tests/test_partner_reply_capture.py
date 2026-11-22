"""
Unit tests for server.maybe_capture_partner_reply.

2026-10-18: week 2026-10-19 got stuck in awaiting_partner_signoff all day
because the pending-reply block captured whichever message from the
partner handle arrived FIRST, regardless of content — an unrelated
recipe-idea URL consumed the capture before the partner's real "Looks good"
showed up six hours later. Fixed by classifying before capturing.

Mocks:
  - server.MENU_PENDING_FILE / MENU_RESPONSE_FILE / MENU_SESSION_FILE → temp files
  - server._call_menubuilder_tool  → canned classify_partner_reply / get_workflow_state replies
  - server.send_imessage / server.queue_outbox → recorded, never sent
  - server.menu_workflow.handle_partner_reply → recorded, never actually run
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


class TestMaybeCapturePartnerReply(unittest.TestCase):

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
        self.handle_partner_reply_calls = []

        self._p_send = patch.object(
            server, "send_imessage",
            side_effect=lambda h, t: self.sent.append((h, t)))
        self._p_outbox = patch.object(
            server, "queue_outbox",
            side_effect=lambda entry: self.outbox.append((entry["handle"], entry["text"])))
        self._p_har = patch.object(
            server.menu_workflow, "handle_partner_reply",
            side_effect=lambda text, session, config: (
                self.handle_partner_reply_calls.append(text), self._answered)[1])
        self._answered = True
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
            return server.maybe_capture_partner_reply(_PARTNER, text, FAKE_CONFIG)

    # ── Case 1: unrelated URL, classified unclear — the Oct 18 regression ──────

    def test_unrelated_url_classified_unclear_is_not_captured(self):
        result = self._run(
            "https://americastestkitchen.com/recipes/slow-cooker-chicken-tortilla-soup",
            {"classify_partner_reply": "unclear"},
        )
        self.assertFalse(result)
        self.assertTrue(self.pending_file.exists(), "pending file must survive an unclear reply")
        self.assertFalse(self.response_file.exists())
        self.assertEqual(self.handle_partner_reply_calls, [])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.outbox, [])

    # ── Case 2: a real approval ──────────────────────────────────────────────

    def test_approval_is_captured_and_dispatched_once(self):
        result = self._run(
            "Looks good",
            {
                "classify_partner_reply": "approve",
                "get_workflow_state": {"state": "awaiting_partner_signoff"},
            },
        )
        self.assertTrue(result)
        self.assertFalse(self.pending_file.exists(), "pending file must be consumed on approval")
        self.assertTrue(self.response_file.exists())
        response = json.loads(self.response_file.read_text())
        self.assertEqual(response["message"], "Looks good")
        self.assertEqual(self.handle_partner_reply_calls, ["Looks good"])

    # 2026-11-22: each reply sent David a generic notice on top of the
    # workflow's own, and told her "Passed it on" when Keanu made the change

    def test_answered_reply_sends_no_generic_texts(self):
        self._answered = True
        self._run("Looks good", {
            "classify_partner_reply": "approve",
            "get_workflow_state": {"state": "awaiting_partner_signoff"},
        })
        self.assertEqual(self.sent, [])
        self.assertEqual(self.outbox, [])

    def test_unanswered_reply_gets_passed_on_but_no_generic_admin_notice(self):
        self._answered = False
        self._run("Hmm not sure", {
            "classify_partner_reply": "swap_request",
            "get_workflow_state": {"state": "awaiting_partner_signoff"},
        })
        self.assertEqual(self.sent, [(_PARTNER, "Thanks! Passed it on to David.")])
        self.assertEqual(self.outbox, [])

    def test_workflow_error_falls_back_to_both_generic_texts(self):
        self._p_har.stop()
        with patch.object(server.menu_workflow, "handle_partner_reply", side_effect=RuntimeError("bridge down")):
            self._run("Looks good", {
                "classify_partner_reply": "approve",
                "get_workflow_state": {"state": "awaiting_partner_signoff"},
            })
        self._p_har.start()
        self.assertEqual(self.sent, [(_PARTNER, "Thanks! Passed it on to David.")])
        self.assertEqual(self.outbox, [(_ADMIN, "Partner replied to the menu: Looks good")])

    def test_approval_outside_awaiting_signoff_does_not_dispatch_to_workflow(self):
        result = self._run(
            "Looks good",
            {
                "classify_partner_reply": "approve",
                "get_workflow_state": {"state": "idle"},
            },
        )
        self.assertTrue(result)
        self.assertEqual(self.handle_partner_reply_calls, [])
        self.assertEqual(self.sent, [(_PARTNER, "Thanks! Passed it on to David.")])
        self.assertEqual(self.outbox, [(_ADMIN, "Partner replied to the menu: Looks good")])

    # ── Case 3: bridge failure fails safe ───────────────────────────────────

    def test_bridge_failure_treated_as_unclear_and_kept(self):
        result = self._run(
            "Looks good",
            {"classify_partner_reply": {"error": "subprocess timed out"}},
        )
        self.assertFalse(result)
        self.assertTrue(self.pending_file.exists())
        self.assertFalse(self.response_file.exists())
        self.assertEqual(self.handle_partner_reply_calls, [])

    # ── Not the partner / no pending file ────────────────────────────────────────

    def test_message_from_someone_else_is_ignored(self):
        bridge_mock = MagicMock(return_value="approve")
        with patch.object(server, "_call_menubuilder_tool", bridge_mock):
            result = server.maybe_capture_partner_reply(_ADMIN, "Looks good", FAKE_CONFIG)
        self.assertFalse(result)
        self.assertTrue(self.pending_file.exists())
        bridge_mock.assert_not_called()

    def test_no_pending_file_is_a_noop(self):
        self.pending_file.unlink()
        bridge_mock = MagicMock(return_value="approve")
        with patch.object(server, "_call_menubuilder_tool", bridge_mock):
            result = server.maybe_capture_partner_reply(_PARTNER, "Looks good", FAKE_CONFIG)
        self.assertFalse(result)
        bridge_mock.assert_not_called()


class TestChangeRequestRearmsPending(unittest.TestCase):
    """menu_workflow.handle_partner_reply: the 'change request, menu re-sent' branch
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
            "state": "awaiting_partner_signoff",
            "parsed": True,
        })
        with patch.object(self.menu_workflow, "call_menubuilder_tool", bridge_mock):
            self.menu_workflow.handle_partner_reply(
                "Swap Monday for tacos", {}, FAKE_CONFIG)
        self.assertTrue(self.pending_file.exists())
        saved = json.loads(self.pending_file.read_text())
        self.assertEqual(saved["partner_handle"], _PARTNER)


class TestPartnerReplyOutcomeTexts(unittest.TestCase):
    """handle_partner_reply sends David one outcome text and reports whether
    she was answered, so server.py can skip its generic texts."""

    def setUp(self):
        import agents.menu_workflow as menu_workflow
        self.mw = menu_workflow
        self.tmp = Path(tempfile.mkdtemp())
        self.outbox = []
        self._ps = [
            patch.object(menu_workflow, "MENU_PENDING_FILE", self.tmp / "menu_pending.json"),
            patch.object(menu_workflow, "_send_outbox",
                         side_effect=lambda h, t: self.outbox.append((h, t))),
        ]
        for p in self._ps:
            p.start()

    def tearDown(self):
        for p in self._ps:
            p.stop()

    def _reply(self, text, bridge_result):
        with patch.object(self.mw, "call_menubuilder_tool", MagicMock(return_value=bridge_result)):
            return self.mw.handle_partner_reply(text, {}, FAKE_CONFIG)

    def test_change_request_is_answered_with_one_admin_text(self):
        answered = self._reply("Pick something else for Monday",
                               {"state": "awaiting_partner_signoff", "parsed": True})
        self.assertTrue(answered)
        self.assertEqual(self.outbox, [(_ADMIN, "Partner asked for a change: "
                                                "'Pick something else for Monday'. Updated and re-sent to her.")])

    def test_approval_is_answered_and_names_her_reply(self):
        answered = self._reply("Looks good", {"state": "complete"})
        self.assertTrue(answered)
        self.assertEqual(self.outbox, [(_ADMIN, "Partner approved the menu: 'Looks good'")])

    def test_unparsed_reply_is_not_answered(self):
        answered = self._reply("hmm", {"state": "awaiting_partner_signoff", "parsed": False})
        self.assertFalse(answered)
        self.assertEqual(len(self.outbox), 1)
        self.assertIn("Handle manually", self.outbox[0][1])


if __name__ == "__main__":
    unittest.main()
