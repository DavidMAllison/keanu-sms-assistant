"""
Unit tests for the menu workflow state machine (agents/menu_workflow.py).

Mocks:
  - call_menubuilder_tool  → canned fixture dicts (no subprocess, no MenuBuilder)
  - MENU_SESSION_FILE      → temp file (no writes to /Users/Shared/cooking/)
  - OUTBOX_FILE            → temp file (no writes to .outbox.json)

Claude API is never called — pre-agent guards are tested by asserting on the
return value before the agent loop is reached.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import agents.menu_workflow as mw
from tests.mb_fixtures import (
    FAKE_CONFIG,
    START_DEFAULT,
    START_ERROR,
    START_SUNDAY_DATE,
    START_WITH_FIRSTCOOK,
    START_WITH_KNOWN_NO_FEEDBACK,
    SWAP_NEEDS_CONFIRMATION,
    SWAP_SUCCESS,
    SWAP_SUCCESS_WITH_NOTE,
)


# ── Base class ────────────────────────────────────────────────────────────────

class _Base(unittest.TestCase):
    """
    Patches MENU_SESSION_FILE and OUTBOX_FILE to temp files for every test.
    Subclasses get self.session_path and self.outbox_path as Path objects.
    """

    def setUp(self):
        fd, p = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.session_path = Path(p)
        self.session_path.unlink()  # start with no file — matches real idle state

        fd2, p2 = tempfile.mkstemp(suffix=".json")
        os.close(fd2)
        self.outbox_path = Path(p2)
        self.outbox_path.unlink()

        self._p_sess = patch("agents.menu_workflow.MENU_SESSION_FILE", self.session_path)
        self._p_outbox = patch("agents.menu_workflow.OUTBOX_FILE", self.outbox_path)
        self._p_sess.start()
        self._p_outbox.start()

    def tearDown(self):
        self._p_sess.stop()
        self._p_outbox.stop()
        for p in (self.session_path, self.outbox_path):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    def _session(self) -> dict:
        """Read the current temp session file."""
        return json.loads(self.session_path.read_text()) if self.session_path.exists() else {}

    def _write_session(self, data: dict):
        """Pre-populate the temp session file."""
        self.session_path.write_text(json.dumps(data))


# ── TestHandleStart ───────────────────────────────────────────────────────────

class TestHandleStart(_Base):

    def test_reply_contains_week_label_and_confirmation_question(self):
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=START_DEFAULT):
            reply = mw.handle_start(FAKE_CONFIG)
        self.assertIn("Jul", reply)
        self.assertIn("Does that sound right", reply)

    def test_state_is_awaiting_week_confirmation(self):
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=START_DEFAULT):
            mw.handle_start(FAKE_CONFIG)
        self.assertEqual(self._session()["state"], "awaiting_week_confirmation")

    def test_week_start_normalized_to_monday_when_mcp_returns_sunday(self):
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=START_SUNDAY_DATE):
            mw.handle_start(FAKE_CONFIG)
        ws = date.fromisoformat(self._session()["week_start"])
        self.assertEqual(ws.weekday(), 0, "week_start must be Monday (weekday=0)")

    def test_no_first_cooks_produces_empty_feedback_queue(self):
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=START_DEFAULT):
            mw.handle_start(FAKE_CONFIG)
        self.assertEqual(self._session()["feedback_queue"], [])

    def test_first_cook_meal_appears_in_feedback_queue(self):
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=START_WITH_FIRSTCOOK):
            mw.handle_start(FAKE_CONFIG)
        self.assertEqual(self._session()["feedback_queue"], ["Brand New Recipe"])

    def test_known_meal_without_feedback_is_silently_skipped(self):
        # times_cooked > 0, no sms_feedback — should NOT appear in queue
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=START_WITH_KNOWN_NO_FEEDBACK):
            mw.handle_start(FAKE_CONFIG)
        self.assertEqual(self._session()["feedback_queue"], [])

    def test_for_week_str_passes_monday_iso_date_to_mcp(self):
        mock_mb = MagicMock(return_value=START_DEFAULT)
        with patch("agents.menu_workflow.call_menubuilder_tool", mock_mb):
            mw.handle_start(FAKE_CONFIG, for_week_str="July 7")
        kwargs = mock_mb.call_args[1]
        self.assertIn("week_start", kwargs)
        ws = date.fromisoformat(kwargs["week_start"])
        self.assertEqual(ws.weekday(), 0, "week_start kwarg passed to MCP must be a Monday")

    def test_for_week_str_next_week_passes_iso_date(self):
        mock_mb = MagicMock(return_value=START_DEFAULT)
        with patch("agents.menu_workflow.call_menubuilder_tool", mock_mb):
            mw.handle_start(FAKE_CONFIG, for_week_str="next week")
        kwargs = mock_mb.call_args[1]
        self.assertIn("week_start", kwargs)
        ws = date.fromisoformat(kwargs["week_start"])
        self.assertEqual(ws.weekday(), 0)

    def test_no_for_week_str_omits_week_start_kwarg(self):
        mock_mb = MagicMock(return_value=START_DEFAULT)
        with patch("agents.menu_workflow.call_menubuilder_tool", mock_mb):
            mw.handle_start(FAKE_CONFIG)
        kwargs = mock_mb.call_args[1]
        self.assertNotIn("week_start", kwargs)

    def test_mcp_error_returns_user_facing_error(self):
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=START_ERROR):
            reply = mw.handle_start(FAKE_CONFIG)
        self.assertIn("couldn't start", reply.lower())


# ── TestWeekConfirmation ──────────────────────────────────────────────────────

class TestWeekConfirmation(_Base):

    def _session_with_queue(self, feedback_queue=None) -> dict:
        s = {
            "state": "awaiting_week_confirmation",
            "week_start": "2026-07-07",
            "feedback_queue": feedback_queue if feedback_queue is not None else [],
            "schedule_notes": [],
            "conversation": [
                {"role": "user", "content": "[Menu build started]"},
                {"role": "assistant", "content": "Let's plan the week of Jul 7–13."},
            ],
        }
        self._write_session(s)
        return s

    def test_yes_empty_queue_advances_to_awaiting_schedule(self):
        session = self._session_with_queue()
        reply = mw._handle_week_confirmation("yes", session, FAKE_CONFIG)
        self.assertIn("schedule", reply.lower())
        self.assertEqual(self._session()["state"], "awaiting_schedule")

    def test_yes_with_queue_advances_to_awaiting_meal_logging(self):
        session = self._session_with_queue(["Brand New Recipe"])
        reply = mw._handle_week_confirmation("yes", session, FAKE_CONFIG)
        self.assertIn("Brand New Recipe", reply)
        self.assertEqual(self._session()["state"], "awaiting_meal_logging")

    def test_sounds_good_confirms_like_yes(self):
        session = self._session_with_queue()
        reply = mw._handle_week_confirmation("sounds good", session, FAKE_CONFIG)
        self.assertEqual(self._session()["state"], "awaiting_schedule")
        self.assertIn("schedule", reply.lower())

    def test_next_week_reruns_handle_start(self):
        session = self._session_with_queue()
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=START_DEFAULT):
            reply = mw._handle_week_confirmation("next week", session, FAKE_CONFIG)
        # Must re-confirm, not advance
        self.assertIn("Does that sound right", reply)
        self.assertEqual(self._session()["state"], "awaiting_week_confirmation")

    def test_month_date_reruns_handle_start(self):
        session = self._session_with_queue()
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=START_DEFAULT):
            reply = mw._handle_week_confirmation("actually July 14", session, FAKE_CONFIG)
        self.assertIn("Does that sound right", reply)
        self.assertEqual(self._session()["state"], "awaiting_week_confirmation")

    def test_change_keyword_reruns_handle_start(self):
        session = self._session_with_queue()
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=START_DEFAULT):
            reply = mw._handle_week_confirmation("different week please", session, FAKE_CONFIG)
        self.assertIn("Does that sound right", reply)

    def test_confirmation_appends_exchange_to_conversation(self):
        session = self._session_with_queue()
        mw._handle_week_confirmation("yep", session, FAKE_CONFIG)
        convo = self._session()["conversation"]
        roles = [m["role"] for m in convo]
        # Confirmation turn should add one user + one assistant entry
        self.assertEqual(roles.count("user"), 2)
        self.assertEqual(roles.count("assistant"), 2)


# ── TestHandleNeedsConfirmation ───────────────────────────────────────────────

class TestHandleNeedsConfirmation(_Base):

    def _base_session(self, state="awaiting_meal_approval") -> dict:
        s = {"state": state, "week_start": "2026-07-07", "selected_meals": {}}
        self._write_session(s)
        return s

    def test_sets_awaiting_recipe_confirmation_state(self):
        session = self._base_session()
        mw._handle_needs_confirmation(
            SWAP_NEEDS_CONFIRMATION, "swap_meal", {"day": "Tue"}, session
        )
        self.assertEqual(session["state"], "awaiting_recipe_confirmation")
        self.assertEqual(self._session()["state"], "awaiting_recipe_confirmation")

    def test_saves_all_three_pending_keys(self):
        session = self._base_session("awaiting_meal_approval")
        mw._handle_needs_confirmation(
            SWAP_NEEDS_CONFIRMATION,
            "swap_meal",
            {"day": "Tue", "reason": "swap"},
            session,
        )
        self.assertEqual(session["pending_suggested"], "Test Chicken Tikka Masala")
        self.assertEqual(session["pending_confirmation"]["tool"], "swap_meal")
        self.assertEqual(session["pending_confirmation"]["args"]["day"], "Tue")
        self.assertEqual(session["pre_confirmation_state"], "awaiting_meal_approval")

    def test_returns_message_from_mcp(self):
        session = self._base_session()
        msg = mw._handle_needs_confirmation(SWAP_NEEDS_CONFIRMATION, "swap_meal", {}, session)
        self.assertIn("Test Chicken Tikka Masala", msg)

    def test_success_result_returns_none_and_leaves_state_unchanged(self):
        session = self._base_session("awaiting_meal_approval")
        result = mw._handle_needs_confirmation(SWAP_SUCCESS, "swap_meal", {}, session)
        self.assertIsNone(result)
        self.assertEqual(session["state"], "awaiting_meal_approval")

    def test_error_result_returns_none(self):
        session = self._base_session()
        result = mw._handle_needs_confirmation({"error": "oops"}, "swap_meal", {}, session)
        self.assertIsNone(result)


# ── TestRecipeConfirmation ────────────────────────────────────────────────────

class TestRecipeConfirmation(_Base):

    def _confirmation_session(self, prev_state="awaiting_meal_approval") -> dict:
        s = {
            "state": "awaiting_recipe_confirmation",
            "week_start": "2026-07-07",
            "selected_meals": {"Mon": "Test Pasta"},
            "quick_days": [],
            "pending_confirmation": {
                "tool": "swap_meal",
                "args": {
                    "day": "Tue",
                    "reason": "try something new",
                    "replacement": "",
                    "cuisine_direction": "",
                },
            },
            "pending_suggested": "Test Chicken Tikka Masala",
            "pre_confirmation_state": prev_state,
        }
        self._write_session(s)
        return s

    def test_yes_recalls_tool_with_suggested_name(self):
        session = self._confirmation_session()
        mock_mb = MagicMock(return_value=SWAP_SUCCESS)
        with patch("agents.menu_workflow.call_menubuilder_tool", mock_mb):
            mw._handle_recipe_confirmation("yes", session, FAKE_CONFIG)
        kwargs = mock_mb.call_args[1]
        self.assertEqual(kwargs["replacement"], "Test Chicken Tikka Masala")

    def test_yeah_also_confirms(self):
        session = self._confirmation_session()
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=SWAP_SUCCESS):
            reply = mw._handle_recipe_confirmation("yeah", session, FAKE_CONFIG)
        self.assertNotEqual(reply, "What's the recipe name?")

    def test_yep_also_confirms(self):
        session = self._confirmation_session()
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=SWAP_SUCCESS):
            reply = mw._handle_recipe_confirmation("yep", session, FAKE_CONFIG)
        self.assertNotEqual(reply, "What's the recipe name?")

    def test_yes_updates_selected_meals_in_session(self):
        session = self._confirmation_session()
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=SWAP_SUCCESS):
            mw._handle_recipe_confirmation("yes", session, FAKE_CONFIG)
        saved = self._session()
        self.assertEqual(saved["selected_meals"], SWAP_SUCCESS["selected_meals"])

    def test_yes_includes_note_in_reply_when_present(self):
        session = self._confirmation_session()
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=SWAP_SUCCESS_WITH_NOTE):
            reply = mw._handle_recipe_confirmation("yes", session, FAKE_CONFIG)
        self.assertIn("Swapped to an idea recipe", reply)

    def test_yes_clears_all_pending_keys(self):
        session = self._confirmation_session()
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=SWAP_SUCCESS):
            mw._handle_recipe_confirmation("yes", session, FAKE_CONFIG)
        saved = self._session()
        self.assertNotIn("pending_confirmation", saved)
        self.assertNotIn("pending_suggested", saved)
        self.assertNotIn("pre_confirmation_state", saved)

    def test_no_restores_previous_state(self):
        session = self._confirmation_session(prev_state="awaiting_meal_approval")
        reply = mw._handle_recipe_confirmation("no", session, FAKE_CONFIG)
        self.assertEqual(reply, "What's the recipe name?")
        self.assertEqual(self._session()["state"], "awaiting_meal_approval")

    def test_no_clears_all_pending_keys(self):
        session = self._confirmation_session()
        mw._handle_recipe_confirmation("no", session, FAKE_CONFIG)
        saved = self._session()
        self.assertNotIn("pending_confirmation", saved)
        self.assertNotIn("pending_suggested", saved)
        self.assertNotIn("pre_confirmation_state", saved)

    def test_arbitrary_text_restores_state_and_asks_for_name(self):
        session = self._confirmation_session(prev_state="awaiting_meal_approval")
        reply = mw._handle_recipe_confirmation("hmm not sure", session, FAKE_CONFIG)
        self.assertEqual(reply, "What's the recipe name?")
        self.assertEqual(self._session()["state"], "awaiting_meal_approval")


# ── TestSwapMealIntegration ───────────────────────────────────────────────────

class TestSwapMealIntegration(_Base):
    """
    Tests _execute_menu_tool("swap_meal", ...) end-to-end within the workflow.
    Verifies that needs_confirmation from MCP correctly sets state, and that
    a successful swap returns a formatted plan.
    """

    def _approval_session(self) -> dict:
        s = {
            "state": "awaiting_meal_approval",
            "week_start": "2026-07-07",
            "selected_meals": {"Mon": "Test Pasta", "Tue": "Test Tacos"},
            "quick_days": [],
            "cuisine_direction": "Italian",
        }
        self._write_session(s)
        return s

    def test_needs_confirmation_sets_state_and_returns_question(self):
        session = self._approval_session()
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=SWAP_NEEDS_CONFIRMATION):
            result = mw._execute_menu_tool(
                "swap_meal",
                {"day": "Tue", "reason": "try something new", "replacement": "chicken tik"},
                session,
                FAKE_CONFIG,
            )
        self.assertIn("Test Chicken Tikka Masala", result)
        self.assertEqual(self._session()["state"], "awaiting_recipe_confirmation")

    def test_needs_confirmation_saves_pending_tool_and_args(self):
        session = self._approval_session()
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=SWAP_NEEDS_CONFIRMATION):
            mw._execute_menu_tool(
                "swap_meal",
                {"day": "Tue", "reason": "try something new", "replacement": "chicken tik"},
                session,
                FAKE_CONFIG,
            )
        saved = self._session()
        self.assertEqual(saved["pending_confirmation"]["tool"], "swap_meal")
        self.assertEqual(saved["pending_suggested"], "Test Chicken Tikka Masala")
        self.assertEqual(saved["pre_confirmation_state"], "awaiting_meal_approval")

    def test_success_returns_formatted_day_list(self):
        session = self._approval_session()
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=SWAP_SUCCESS):
            result = mw._execute_menu_tool(
                "swap_meal",
                {"day": "Tue", "reason": "try something new", "replacement": ""},
                session,
                FAKE_CONFIG,
            )
        self.assertIn("Mon", result)
        self.assertIn("Tue", result)

    def test_success_does_not_set_confirmation_state(self):
        session = self._approval_session()
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=SWAP_SUCCESS):
            mw._execute_menu_tool(
                "swap_meal",
                {"day": "Tue", "reason": "swap", "replacement": ""},
                session,
                FAKE_CONFIG,
            )
        self.assertEqual(self._session()["state"], "awaiting_meal_approval")


# ── TestAgentReplyRouting ─────────────────────────────────────────────────────

class TestAgentReplyRouting(_Base):
    """
    Tests pre-agent state guards in menu_agent_reply.
    Claude API is never reached for any of these cases.
    """

    def test_hold_text_intercepted_before_agent(self):
        self._write_session({"state": "awaiting_schedule", "conversation": []})
        session = self._session()
        reply = mw.menu_agent_reply("hold on", session, FAKE_CONFIG)
        self.assertIn("pick it up", reply.lower())

    def test_pause_text_intercepted_before_agent(self):
        self._write_session({"state": "awaiting_schedule", "conversation": []})
        session = self._session()
        reply = mw.menu_agent_reply("pause for now", session, FAKE_CONFIG)
        self.assertIn("pick it up", reply.lower())

    def test_awaiting_ashley_signoff_returns_holding_message(self):
        self._write_session({"state": "awaiting_ashley_signoff", "conversation": []})
        session = self._session()
        reply = mw.menu_agent_reply("what's the status?", session, FAKE_CONFIG)
        self.assertIn("Ashley", reply)

    def test_awaiting_week_confirmation_routed_without_api(self):
        s = {
            "state": "awaiting_week_confirmation",
            "week_start": "2026-07-07",
            "feedback_queue": [],
            "schedule_notes": [],
            "conversation": [
                {"role": "user", "content": "[Menu build started]"},
                {"role": "assistant", "content": "Let's plan the week of Jul 7–13."},
            ],
        }
        self._write_session(s)
        # "yes" should advance through _handle_week_confirmation, never hitting Claude
        reply = mw.menu_agent_reply("yes", dict(s), FAKE_CONFIG)
        self.assertIn("schedule", reply.lower())

    def test_awaiting_recipe_confirmation_routed_without_api(self):
        s = {
            "state": "awaiting_recipe_confirmation",
            "week_start": "2026-07-07",
            "selected_meals": {"Mon": "Test Pasta"},
            "quick_days": [],
            "pending_confirmation": {
                "tool": "swap_meal",
                "args": {
                    "day": "Mon",
                    "reason": "swap",
                    "replacement": "",
                    "cuisine_direction": "",
                },
            },
            "pending_suggested": "Test Chicken Tikka Masala",
            "pre_confirmation_state": "awaiting_meal_approval",
            "conversation": [],
        }
        self._write_session(s)
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=SWAP_SUCCESS):
            reply = mw.menu_agent_reply("yes", dict(s), FAKE_CONFIG)
        # Should get a formatted plan back, not a Claude API error
        self.assertIn("Mon", reply)


if __name__ == "__main__":
    unittest.main(verbosity=2)
