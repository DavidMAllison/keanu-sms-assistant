"""
Unit tests for the menu workflow state machine (agents/menu_workflow.py).

Mocks:
  - call_menubuilder_tool  → canned fixture dicts (no subprocess, no MenuBuilder)
  - MENU_SESSION_FILE      → temp file (no writes to /Users/Shared/cooking-state/)
  - tools.OUTBOX_DIR       → temp dir (no writes to the real outbox spool)

Claude API is never called — pre-agent guards are tested by asserting on the
return value before the agent loop is reached.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import agents.menu_workflow as mw
from tests.mb_fixtures import (
    APPROVE_MISMATCH,
    FAKE_CONFIG,
    START_ALREADY_PLANNED,
    START_DEFAULT,
    START_ERROR,
    START_SUNDAY_DATE,
    START_WITH_FIRSTCOOK,
    START_WITH_KNOWN_NO_FEEDBACK,
    SWAP_ERROR,
    SWAP_NEEDS_CONFIRMATION,
    SWAP_SUCCESS,
    SWAP_SUCCESS_WITH_NOTE,
)


# ── Base class ────────────────────────────────────────────────────────────────

class _Base(unittest.TestCase):
    """
    Patches MENU_SESSION_FILE and the outbox spool dir to temp paths for every
    test. Subclasses get self.session_path and self.outbox_dir as Path objects.
    (queue_outbox reads tools.OUTBOX_DIR at call time, so patching the module
    global redirects menu_workflow's _send_outbox too.)
    """

    def setUp(self):
        fd, p = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.session_path = Path(p)
        self.session_path.unlink()  # start with no file — matches real idle state

        self.outbox_dir = Path(tempfile.mkdtemp())

        self._p_sess = patch("agents.menu_workflow.MENU_SESSION_FILE", self.session_path)
        self._p_outbox = patch("tools.OUTBOX_DIR", self.outbox_dir)
        self._p_sess.start()
        self._p_outbox.start()

    def tearDown(self):
        self._p_sess.stop()
        self._p_outbox.stop()
        try:
            self.session_path.unlink()
        except FileNotFoundError:
            pass
        shutil.rmtree(self.outbox_dir, ignore_errors=True)

    def _outbox_entries(self) -> list:
        """Read all spool entries, oldest first."""
        return [json.loads(p.read_text()) for p in sorted(self.outbox_dir.glob("*.json"))]

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

    def test_already_planned_offers_replan_and_leaves_session(self):
        self._write_session({"state": "", "week_start": "2026-06-29"})
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=START_ALREADY_PLANNED):
            reply = mw.handle_start(FAKE_CONFIG)
        self.assertEqual(reply, mw.ALREADY_PLANNED_REPLY)
        self.assertEqual(self._session(), {"state": "", "week_start": "2026-06-29"})

    def test_plain_start_never_passes_replan(self):
        mock_mb = MagicMock(return_value=START_DEFAULT)
        with patch("agents.menu_workflow.call_menubuilder_tool", mock_mb):
            mw.handle_start(FAKE_CONFIG, for_week_str="next week")
        self.assertNotIn("replan", mock_mb.call_args[1])

    def test_replan_passes_replan_true(self):
        mock_mb = MagicMock(return_value=START_DEFAULT)
        with patch("agents.menu_workflow.call_menubuilder_tool", mock_mb):
            mw.handle_start(FAKE_CONFIG, replan=True)
        self.assertIs(mock_mb.call_args[1].get("replan"), True)


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

    def _base_session(self, state=None) -> dict:
        # No "state" key by default — bridge phases (e.g. awaiting_meal_approval)
        # are owned by MenuBuilder and never stored in menu_session.json.
        s = {"week_start": "2026-07-07", "selected_meals": {}}
        if state:
            s["state"] = state
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
        session = self._base_session()
        mw._handle_needs_confirmation(
            SWAP_NEEDS_CONFIRMATION,
            "swap_meal",
            {"day": "Tue", "reason": "swap"},
            session,
        )
        self.assertEqual(session["pending_suggested"], "Test Chicken Tikka Masala")
        self.assertEqual(session["pending_confirmation"]["tool"], "swap_meal")
        self.assertEqual(session["pending_confirmation"]["args"]["day"], "Tue")
        # Confirmation began during a bridge phase — no local state to restore
        self.assertIsNone(session["pre_confirmation_state"])

    def test_saves_local_pre_confirmation_state_when_present(self):
        session = self._base_session("awaiting_schedule")
        mw._handle_needs_confirmation(
            SWAP_NEEDS_CONFIRMATION, "swap_meal", {"day": "Tue"}, session
        )
        self.assertEqual(session["pre_confirmation_state"], "awaiting_schedule")

    def test_returns_message_from_mcp(self):
        session = self._base_session()
        msg = mw._handle_needs_confirmation(SWAP_NEEDS_CONFIRMATION, "swap_meal", {}, session)
        self.assertIn("Test Chicken Tikka Masala", msg)

    def test_success_result_returns_none_and_leaves_state_unchanged(self):
        session = self._base_session()
        result = mw._handle_needs_confirmation(SWAP_SUCCESS, "swap_meal", {}, session)
        self.assertIsNone(result)
        self.assertNotIn("state", session)

    def test_error_result_returns_none(self):
        session = self._base_session()
        result = mw._handle_needs_confirmation({"error": "oops"}, "swap_meal", {}, session)
        self.assertIsNone(result)


# ── TestRecipeConfirmation ────────────────────────────────────────────────────

class TestRecipeConfirmation(_Base):

    def _confirmation_session(self, prev_state=None) -> dict:
        # prev_state=None models a confirmation that began during a bridge phase
        # (e.g. awaiting_meal_approval) — no local state to restore afterwards.
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

    def test_yes_with_error_result_reports_error_and_clears_pending(self):
        session = self._confirmation_session()
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=SWAP_ERROR):
            reply = mw._handle_recipe_confirmation("yes", session, FAKE_CONFIG)
        self.assertIn("Couldn't make that swap", reply)
        self.assertNotIn("Test Pasta", reply)
        saved = self._session()
        self.assertNotIn("pending_confirmation", saved)
        self.assertNotIn("state", saved)

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

    def test_yes_clears_local_state_when_no_previous_state(self):
        session = self._confirmation_session()
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=SWAP_SUCCESS):
            mw._handle_recipe_confirmation("yes", session, FAKE_CONFIG)
        self.assertNotIn("state", self._session())

    def test_no_restores_previous_local_state(self):
        session = self._confirmation_session(prev_state="awaiting_schedule")
        reply = mw._handle_recipe_confirmation("no", session, FAKE_CONFIG)
        self.assertIsNone(reply)  # the agent answers it
        self.assertEqual(self._session()["state"], "awaiting_schedule")

    def test_no_clears_local_state_when_no_previous_state(self):
        session = self._confirmation_session()
        reply = mw._handle_recipe_confirmation("no", session, FAKE_CONFIG)
        self.assertIsNone(reply)
        self.assertNotIn("state", self._session())

    def test_no_clears_all_pending_keys(self):
        session = self._confirmation_session()
        mw._handle_recipe_confirmation("no", session, FAKE_CONFIG)
        saved = self._session()
        self.assertNotIn("pending_confirmation", saved)
        self.assertNotIn("pending_suggested", saved)
        self.assertNotIn("pre_confirmation_state", saved)

    def test_arbitrary_text_restores_state_and_hands_to_agent(self):
        session = self._confirmation_session(prev_state="awaiting_schedule")
        reply = mw._handle_recipe_confirmation("hmm not sure", session, FAKE_CONFIG)
        self.assertIsNone(reply)
        self.assertEqual(self._session()["state"], "awaiting_schedule")

    def test_looks_good_is_not_a_yes(self):
        # "looks" contains "ok"
        session = self._confirmation_session()
        with patch("agents.menu_workflow.call_menubuilder_tool") as call:
            reply = mw._handle_recipe_confirmation("looks good but not that one", session, FAKE_CONFIG)
        self.assertIsNone(reply)
        call.assert_not_called()


# ── TestSwapMealIntegration ───────────────────────────────────────────────────

class TestSwapMealIntegration(_Base):
    """
    Tests _execute_menu_tool("swap_meal", ...) end-to-end within the workflow.
    Verifies that needs_confirmation from MCP correctly sets state, and that
    a successful swap returns a formatted plan.
    """

    def _approval_session(self) -> dict:
        # Meal approval is a bridge phase — no "state" key in the local session.
        s = {
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
        self.assertIsNone(saved["pre_confirmation_state"])

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

    def test_error_returns_message_not_unchanged_plan(self):
        session = self._approval_session()
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=SWAP_ERROR):
            result = mw._execute_menu_tool(
                "swap_meal",
                {"day": "Tue", "reason": "swap", "replacement": "Soy Salmon"},
                session,
                FAKE_CONFIG,
            )
        self.assertIn("Couldn't make that swap", result)
        self.assertIn("Test Soy Salmon", result)
        self.assertNotIn("Test Tacos", result)

    def test_success_does_not_set_confirmation_state(self):
        session = self._approval_session()
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=SWAP_SUCCESS):
            mw._execute_menu_tool(
                "swap_meal",
                {"day": "Tue", "reason": "swap", "replacement": ""},
                session,
                FAKE_CONFIG,
            )
        # Bridge state from the swap result is never written into the session
        self.assertNotIn("state", self._session())

    def test_approve_menu_clears_local_state(self):
        session = self._approval_session()
        session["state"] = "awaiting_schedule"  # leftover local sub-state
        self._write_session(session)
        mock_mb = MagicMock(return_value={"ok": True})
        with patch("agents.menu_workflow.call_menubuilder_tool", mock_mb):
            result = mw._execute_menu_tool("approve_menu", {}, session, FAKE_CONFIG)
        self.assertIn("Partner", result)
        # awaiting_partner_signoff is bridge-owned — local state must be cleared
        self.assertNotIn("state", self._session())
        # Guardrail: the local plan snapshot must be sent along for drift detection.
        # (call_args is the *last* call — generate_shopping_list also fires — so
        # find the approve_menu call specifically.)
        approve_call = next(
            c for c in mock_mb.call_args_list if c.args[0] == "approve_menu"
        )
        self.assertEqual(
            approve_call.kwargs["expected_selected_meals"], session["selected_meals"]
        )

    def test_approve_menu_mismatch_blocks_send_and_shows_actual_plan(self):
        session = self._approval_session()
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=APPROVE_MISMATCH):
            result = mw._execute_menu_tool("approve_menu", {}, session, FAKE_CONFIG)
        self.assertIn("changed", result.lower())
        self.assertIn("Test Chicken Tikka Masala", result)
        self.assertNotIn("Partner", result)  # never claims it was sent
        # Local mirror is corrected to the actual bridge state
        self.assertEqual(self._session()["selected_meals"], APPROVE_MISMATCH["actual"])


# ── TestPendingUrlSwap ────────────────────────────────────────────────────────

class TestPendingUrlSwap(_Base):
    """
    _handle_pending_url_swap had its own independent instance of the
    selected_meals-drift bug: it never refreshed session["selected_meals"]
    before calling approve_menu. Verifies it now pulls fresh state from
    get_workflow_state and passes it as expected_selected_meals.
    """

    def _session_with_pending(self) -> dict:
        s = {
            "week_start": "2026-07-07",
            "selected_meals": {"Wed": "Test Stir Fry"},  # stale local mirror
            "pending_url_swap": {
                "url": "https://example.com/recipe",
                "day": "Wed",
                "existing_recipe": "Test Stir Fry",
            },
        }
        self._write_session(s)
        return s

    def test_refreshes_selected_meals_from_bridge_before_approving(self):
        session = self._session_with_pending()
        fresh_meals = {"Wed": "Test Chicken Tikka Masala", "Thu": "Test Tacos"}

        def fake_mb(tool_name, **kwargs):
            if tool_name == "get_workflow_state":
                return {"selected_meals": fresh_meals}
            return {"ok": True}

        mock_mb = MagicMock(side_effect=fake_mb)
        with patch("agents.menu_workflow.call_menubuilder_tool", mock_mb), \
             patch("agents.menu_workflow._send_to_partner"):
            mw._handle_pending_url_swap("no thanks, keep it", session, FAKE_CONFIG)

        self.assertEqual(self._session()["selected_meals"], fresh_meals)
        approve_call = next(
            c for c in mock_mb.call_args_list if c.args[0] == "approve_menu"
        )
        self.assertEqual(approve_call.kwargs["expected_selected_meals"], fresh_meals)

    def test_missing_bridge_selected_meals_keeps_prior_local_mirror(self):
        session = self._session_with_pending()

        def fake_mb(tool_name, **kwargs):
            if tool_name == "get_workflow_state":
                return {"selected_meals": {}}  # bridge has nothing to offer
            return {"ok": True}

        mock_mb = MagicMock(side_effect=fake_mb)
        with patch("agents.menu_workflow.call_menubuilder_tool", mock_mb), \
             patch("agents.menu_workflow._send_to_partner"):
            mw._handle_pending_url_swap("no thanks, keep it", session, FAKE_CONFIG)

        # Falsy selected_meals from the bridge must not clobber the local mirror
        self.assertEqual(self._session()["selected_meals"], {"Wed": "Test Stir Fry"})


class TestAddRecipeUrl(_Base):
    """
    A recipe URL in the planning chat goes to process_recipe_url — never a
    name guessed from the URL slug for swap_meal (Oct 25 Fri mix-up).
    """

    def _planning_session(self) -> dict:
        s = {
            "week_start": "2026-07-07",
            "selected_meals": {"Mon": "Test Pasta", "Thu": "Test Tacos"},
            "quick_days": [],
        }
        self._write_session(s)
        return s

    def test_calls_process_recipe_url_with_url_and_day(self):
        session = self._planning_session()
        mock_mb = MagicMock(return_value={"status": "added", "recipe": "Test Pork", "swapped_day": "Thu"})
        with patch("agents.menu_workflow.call_menubuilder_tool", mock_mb):
            mw._execute_menu_tool("add_recipe_url", {"url": "https://example.com/r", "day": "Thu"}, session, FAKE_CONFIG)
        mock_mb.assert_called_once_with("process_recipe_url", url="https://example.com/r", day="Thu", force_add=False)

    def test_added_updates_local_plan_and_shows_it(self):
        session = self._planning_session()
        with patch("agents.menu_workflow.call_menubuilder_tool",
                   return_value={"status": "added", "recipe": "Test Pork", "swapped_day": "Thu"}):
            reply = mw._execute_menu_tool("add_recipe_url", {"url": "https://example.com/r", "day": "Thu"}, session, FAKE_CONFIG)
        self.assertIn("Test Pork", reply)
        self.assertEqual(self._session()["selected_meals"]["Thu"], "Test Pork")

    def test_similar_exists_asks_instead_of_swapping(self):
        session = self._planning_session()
        with patch("agents.menu_workflow.call_menubuilder_tool",
                   return_value={"status": "similar_exists", "recipe": "Test Tacos", "match_score": 0.8}):
            reply = mw._execute_menu_tool("add_recipe_url", {"url": "https://example.com/r", "day": "Thu"}, session, FAKE_CONFIG)
        self.assertIn("Test Tacos", reply)
        self.assertIn("Ask David", reply)
        self.assertEqual(self._session(), {"week_start": "2026-07-07",
                                           "selected_meals": {"Mon": "Test Pasta", "Thu": "Test Tacos"},
                                           "quick_days": []})

    def test_force_add_is_passed_through(self):
        session = self._planning_session()
        mock_mb = MagicMock(return_value={"status": "added", "recipe": "Test Pork", "swapped_day": "Thu"})
        with patch("agents.menu_workflow.call_menubuilder_tool", mock_mb):
            mw._execute_menu_tool("add_recipe_url",
                                  {"url": "https://example.com/r", "day": "Thu", "force_add": True}, session, FAKE_CONFIG)
        self.assertTrue(mock_mb.call_args.kwargs["force_add"])

    def test_fetch_failure_reports_error(self):
        session = self._planning_session()
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value={"error": "fetch failed: 403"}):
            reply = mw._execute_menu_tool("add_recipe_url", {"url": "https://example.com/r", "day": "Thu"}, session, FAKE_CONFIG)
        self.assertIn("Couldn't add that recipe", reply)
        self.assertIn("403", reply)

    def test_finalized_note_is_passed_through(self):
        session = self._planning_session()
        with patch("agents.menu_workflow.call_menubuilder_tool",
                   return_value={"status": "added", "recipe": "Test Pork",
                                 "note": "Plan is finalized -- 'Test Pork' was added but not swapped in."}):
            reply = mw._execute_menu_tool("add_recipe_url", {"url": "https://example.com/r", "day": "Thu"}, session, FAKE_CONFIG)
        self.assertIn("not swapped in", reply)
        self.assertEqual(self._session()["selected_meals"]["Thu"], "Test Tacos")

    def test_swap_meal_plan_finalized_gives_readable_reply(self):
        session = self._planning_session()
        with patch("agents.menu_workflow.call_menubuilder_tool",
                   return_value={"error": "plan_finalized", "message": "already finalized"}):
            reply = mw._execute_menu_tool("swap_meal", {"day": "Thu", "reason": "x", "replacement": "Test Pork"},
                                          session, FAKE_CONFIG)
        self.assertIn("already finalized", reply)
        self.assertNotIn("plan_finalized", reply)


class TestTypedNameConfirmation(_Base):
    """
    MenuBuilder only applies a typed recipe name on a confirmed=True re-call
    (Oct 27 2026). The reply picks from the options it offered.
    """

    _NEEDS = {
        "status": "needs_confirmation",
        "suggested": "Test Salad A",
        "options": [{"recipe": "Test Salad A", "url": "u1"}, {"recipe": "Test Salad B", "url": "u2"}],
        "message": "Which one did you mean? 1) Test Salad A - u1 2) Test Salad B - u2",
    }

    def _pending_session(self) -> dict:
        s = {"week_start": "2026-07-07", "selected_meals": {"Tue": "Test Tacos"}, "quick_days": []}
        self._write_session(s)
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=self._NEEDS):
            msg = mw._execute_menu_tool("swap_meal", {"day": "Tue", "reason": "x", "replacement": "salad"},
                                        s, FAKE_CONFIG)
        self.assertEqual(msg, self._NEEDS["message"])
        return json.loads(self.session_path.read_text())

    def test_options_are_saved_with_pending(self):
        session = self._pending_session()
        self.assertEqual(session["pending_confirmation"]["options"], ["Test Salad A", "Test Salad B"])

    def _reply(self, text):
        session = self._pending_session()
        mock_mb = MagicMock(return_value={"selected_meals": {"Tue": "Test Salad B"}})
        with patch("agents.menu_workflow.call_menubuilder_tool", mock_mb):
            mw._handle_recipe_confirmation(text, session, FAKE_CONFIG)
        return mock_mb

    def test_number_picks_that_option_with_confirmed(self):
        mock_mb = self._reply("2")
        self.assertEqual(mock_mb.call_args.kwargs["replacement"], "Test Salad B")
        self.assertTrue(mock_mb.call_args.kwargs["confirmed"])

    def test_option_name_picks_that_option(self):
        mock_mb = self._reply("the test salad b one")
        self.assertEqual(mock_mb.call_args.kwargs["replacement"], "Test Salad B")

    def test_yes_picks_suggested_with_confirmed(self):
        mock_mb = self._reply("yes")
        self.assertEqual(mock_mb.call_args.kwargs["replacement"], "Test Salad A")
        self.assertTrue(mock_mb.call_args.kwargs["confirmed"])

    def test_out_of_range_number_does_not_swap(self):
        mock_mb = self._reply("5")
        mock_mb.assert_not_called()

    def test_add_recipe_url_exists_puts_it_on_the_day(self):
        s = {"week_start": "2026-07-07", "selected_meals": {"Thu": "Test Tacos"}, "quick_days": []}
        self._write_session(s)
        with patch("agents.menu_workflow.call_menubuilder_tool",
                   return_value={"status": "exists", "recipe": "Test Pork", "swapped_day": "Thu"}):
            reply = mw._execute_menu_tool("add_recipe_url", {"url": "https://example.com/p", "day": "Thu"},
                                          s, FAKE_CONFIG)
        self.assertIn("already in the collection", reply)
        self.assertEqual(json.loads(self.session_path.read_text())["selected_meals"]["Thu"], "Test Pork")

    def test_partner_link_derived_swap_is_confirmed(self):
        self._write_session({"week_start": "2026-07-07"})
        session = json.loads(self.session_path.read_text())
        calls = []

        def fake_mb(tool_name, **kwargs):
            calls.append((tool_name, kwargs))
            if tool_name == "handle_partner_reply":
                return {"has_url": True, "url": "https://example.com/r", "extracted_day": "Thu"}
            if tool_name == "process_recipe_url":
                return {"status": "added", "recipe": "Test Tacos"}
            return {"selected_meals": {}}

        with patch("agents.menu_workflow.call_menubuilder_tool", side_effect=fake_mb), \
             patch("agents.menu_workflow._send_to_partner"):
            mw.handle_partner_reply("thursday https://example.com/r", session, FAKE_CONFIG)
        swap = next(kw for name, kw in calls if name == "swap_meal")
        self.assertTrue(swap["confirmed"])


class TestPartnerSwapErrors(_Base):
    """
    the partner's URL swaps used to discard the swap_meal result and confirm
    success regardless. A failed swap must tell her so and alert the admin.
    """

    def test_pending_url_swap_existing_recipe_error(self):
        self._write_session({
            "week_start": "2026-07-07",
            "selected_meals": {"Wed": "Test Stir Fry"},
            "pending_url_swap": {
                "url": "https://example.com/recipe",
                "day": "Wed",
                "existing_recipe": "Test Stir Fry",
            },
        })
        session = self._session()

        def fake_mb(tool_name, **kwargs):
            if tool_name == "swap_meal":
                return SWAP_ERROR
            if tool_name == "get_workflow_state":
                return {"selected_meals": {}}
            return {"ok": True}

        with patch("agents.menu_workflow.call_menubuilder_tool", side_effect=fake_mb), \
             patch("agents.menu_workflow._send_to_partner") as mock_send:
            mw._handle_pending_url_swap("no thanks, keep it", session, FAKE_CONFIG)

        self.assertIn("Couldn't swap Wed", mock_send.call_args_list[0].args[0])
        admin_texts = [e["text"] for e in self._outbox_entries() if e.get("handle") == "+10000000001"]
        self.assertTrue(any("Test Soy Salmon" in t for t in admin_texts))

    def test_added_url_swap_error(self):
        self._write_session({"week_start": "2026-07-07"})
        session = self._session()

        def fake_mb(tool_name, **kwargs):
            if tool_name == "handle_partner_reply":
                return {"has_url": True, "url": "https://example.com/r", "extracted_day": "Thu"}
            if tool_name == "process_recipe_url":
                return {"status": "added", "recipe": "Test Tacos"}
            if tool_name == "swap_meal":
                return SWAP_ERROR
            return {}

        with patch("agents.menu_workflow.call_menubuilder_tool", side_effect=fake_mb), \
             patch("agents.menu_workflow._send_to_partner") as mock_send:
            mw.handle_partner_reply("use this for thursday https://example.com/r", session, FAKE_CONFIG)

        partner_texts = [c.args[0] for c in mock_send.call_args_list]
        self.assertTrue(any("couldn't swap it onto Thu" in t for t in partner_texts))
        self.assertFalse(any("swapped Thu" in t for t in partner_texts))
        admin_texts = [e["text"] for e in self._outbox_entries() if e.get("handle") == "+10000000001"]
        self.assertTrue(any("failed" in t and "Test Soy Salmon" in t for t in admin_texts))


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

    def test_awaiting_partner_signoff_returns_holding_message(self):
        # Signoff is a bridge state — passed in by server.py routing, not stored locally
        self._write_session({"conversation": []})
        session = self._session()
        reply = mw.menu_agent_reply("what's the status?", session, FAKE_CONFIG,
                                    bridge_state="awaiting_partner_signoff")
        self.assertIn("Partner", reply)

    def test_awaiting_finalization_calls_finalize_and_replies(self):
        self._write_session({"conversation": []})
        session = self._session()
        mock_mb = MagicMock(return_value={"state": "complete"})
        with patch("agents.menu_workflow.call_menubuilder_tool", mock_mb):
            reply = mw.menu_agent_reply("done?", session, FAKE_CONFIG,
                                        bridge_state="awaiting_finalization")
        self.assertEqual(reply, "Plan ready!")
        self.assertEqual(mock_mb.call_args[0][0], "finalize_plan")
        # No bridge state may leak into the local session file
        saved = self._session()
        self.assertNotIn("state", saved)

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


# ── TestAgentLoop ─────────────────────────────────────────────────────────────

class _FakeBlock:
    """Plain attribute holder — MagicMock can't be used for content blocks
    because hasattr(mock, 'text') is always True."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _tool_use_response(name="swap_meal", tool_input=None, tool_id="tu_1"):
    resp = _FakeBlock(
        stop_reason="tool_use",
        content=[_FakeBlock(type="tool_use", name=name,
                            input=tool_input or {"day": "Monday"}, id=tool_id)],
    )
    return resp


def _text_response(text):
    return _FakeBlock(stop_reason="end_turn",
                      content=[_FakeBlock(type="text", text=text)])


class TestAgentLoop(_Base):
    """Exercises menu_agent_reply's API loop with a fake Anthropic client:
    the 8-round cap, the tool_choice:none finisher, its static fallback, and
    the persisted [already done: ...] tool notes (Task 2)."""

    def _run(self, responses, text="swap everything"):
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = responses
        fake_anthropic = MagicMock()
        fake_anthropic.Anthropic.return_value = fake_client
        with patch.dict(sys.modules, {"anthropic": fake_anthropic}), \
             patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), \
             patch("agents.menu_workflow._execute_menu_tool", return_value="ok"):
            reply = mw.menu_agent_reply(text, session={}, config=FAKE_CONFIG)
        return reply, fake_client

    def test_text_response_returned_directly(self):
        reply, client = self._run([_text_response("All set!")])
        self.assertEqual(reply, "All set!")
        self.assertEqual(client.messages.create.call_count, 1)

    def test_loop_runs_up_to_eight_rounds_before_finisher(self):
        responses = [_tool_use_response(tool_id=f"tu_{i}") for i in range(8)]
        responses.append(_text_response("Did all eight."))
        reply, client = self._run(responses)
        self.assertEqual(reply, "Did all eight.")
        # 8 tool rounds + 1 finisher
        self.assertEqual(client.messages.create.call_count, 9)

    def test_finisher_uses_tool_choice_none(self):
        responses = [_tool_use_response(tool_id=f"tu_{i}") for i in range(8)]
        responses.append(_text_response("Wrapped up."))
        _, client = self._run(responses)
        finisher_kwargs = client.messages.create.call_args_list[-1][1]
        self.assertEqual(finisher_kwargs["tool_choice"], {"type": "none"})

    def test_finisher_error_falls_back_to_next_step(self):
        responses = [_tool_use_response(tool_id=f"tu_{i}") for i in range(8)]
        responses.append(RuntimeError("api down"))
        reply, _ = self._run(responses)
        self.assertEqual(reply, "Got it! What's next?")
        self.assertNotIn("text 'menu'", reply)

    def test_finisher_gets_reply_nudge_in_last_user_turn(self):
        responses = [_tool_use_response(), _text_response(""), _text_response("Logged! How was the curry?")]
        reply, client = self._run(responses)
        self.assertEqual(reply, "Logged! How was the curry?")
        last = client.messages.create.call_args_list[-1][1]["messages"][-1]
        self.assertEqual(last["role"], "user")
        self.assertIn("Reply to David now", last["content"][-1]["text"])

    def test_blank_finisher_asks_about_next_meal(self):
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = [_tool_use_response(), _text_response(""), _text_response("")]
        fake_anthropic = MagicMock()
        fake_anthropic.Anthropic.return_value = fake_client
        session = {"feedback_queue": ["Adobo Fish Tacos"], "week_start": "2026-11-23"}
        with patch.dict(sys.modules, {"anthropic": fake_anthropic}), \
             patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), \
             patch("agents.menu_workflow._execute_menu_tool", return_value="ok"):
            reply = mw.menu_agent_reply("great", session=session, config=FAKE_CONFIG)
        self.assertEqual(reply, "Got it! How was Adobo Fish Tacos?")

    def test_fallback_shows_plan_when_meals_selected(self):
        reply = mw._fallback_reply({"week_start": "2026-11-23", "selected_meals": {"Mon": "Test Pasta"}})
        self.assertIn("Test Pasta", reply)
        self.assertTrue(reply.endswith("Anything to change?"))

    def test_tool_rounds_persist_already_done_notes(self):
        responses = [
            _tool_use_response("swap_meal", {"day": "Monday"}, "tu_1"),
            _tool_use_response("swap_meal", {"day": "Tuesday"}, "tu_2"),
            _text_response("Swapped both."),
        ]
        self._run(responses)
        convo = self._session()["conversation"]
        notes = [m["content"] for m in convo
                 if m["role"] == "assistant" and m["content"].startswith("[already done:")]
        self.assertEqual(len(notes), 2)
        self.assertIn("swap_meal", notes[0])
        self.assertIn("Monday", notes[0])
        self.assertIn("Tuesday", notes[1])

    def test_notes_precede_final_reply_in_conversation(self):
        responses = [
            _tool_use_response("generate_meal_plan", {}, "tu_1"),
            _text_response("Here's the plan."),
        ]
        self._run(responses)
        convo = self._session()["conversation"]
        self.assertTrue(convo[-2]["content"].startswith("[already done:"))
        self.assertEqual(convo[-1]["content"], "Here's the plan.")


class TestNov22Fixes(_Base):
    """Eating-out nights, our own recipe-site links, and confirmation turns."""

    def _approval_session(self) -> dict:
        s = {"week_start": "2026-11-23", "selected_meals": {"Mon": "Test Pasta"},
             "quick_days": [], "cuisine_direction": ""}
        self._write_session(s)
        return s

    def test_eating_out_replacement_becomes_reason(self):
        session = self._approval_session()
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=SWAP_SUCCESS) as call:
            mw._execute_menu_tool("swap_meal", {"day": "Mon", "reason": "going out to eat",
                                                "replacement": "Going Out to Eat"}, session, FAKE_CONFIG)
        kwargs = call.call_args[1]
        self.assertEqual(kwargs["replacement"], "")
        self.assertEqual(kwargs["reason"], "eating out")
        self.assertNotIn("confirmed", kwargs)

    def test_real_recipe_replacement_untouched(self):
        session = self._approval_session()
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=SWAP_SUCCESS) as call:
            mw._execute_menu_tool("swap_meal", {"day": "Mon", "reason": "x", "replacement": "Test Tacos"},
                                  session, FAKE_CONFIG)
        self.assertEqual(call.call_args[1]["replacement"], "Test Tacos")

    def test_own_site_link_swaps_by_name_confirmed(self):
        session = self._approval_session()
        with patch("agents.menu_workflow.recipe_name_for_site_url", return_value="Thai Red Curry Noodles"), \
             patch("agents.menu_workflow.call_menubuilder_tool", return_value=SWAP_SUCCESS) as call:
            mw._execute_menu_tool("add_recipe_url", {"url": "https://site/Thai_Red_Curry_Noodles", "day": "Fri"},
                                  session, FAKE_CONFIG)
        call.assert_called_once()
        self.assertEqual(call.call_args[0][0], "swap_meal")
        self.assertEqual(call.call_args[1]["replacement"], "Thai Red Curry Noodles")
        self.assertTrue(call.call_args[1]["confirmed"])

    def test_own_site_link_without_day_asks_for_day(self):
        session = self._approval_session()
        with patch("agents.menu_workflow.recipe_name_for_site_url", return_value="Thai Red Curry Noodles"), \
             patch("agents.menu_workflow.call_menubuilder_tool") as call:
            reply = mw._execute_menu_tool("add_recipe_url", {"url": "https://site/x"}, session, FAKE_CONFIG)
        call.assert_not_called()
        self.assertIn("which day", reply)

    def test_other_links_still_fetched(self):
        session = self._approval_session()
        with patch("agents.menu_workflow.recipe_name_for_site_url", return_value=None), \
             patch("agents.menu_workflow.call_menubuilder_tool",
                   return_value={"status": "added", "recipe": "Chili", "swapped_day": "Mon"}) as call:
            mw._execute_menu_tool("add_recipe_url", {"url": "https://example.com/chili", "day": "Mon"},
                                  session, FAKE_CONFIG)
        self.assertEqual(call.call_args[0][0], "process_recipe_url")

    def _run(self, responses, tool_results, session=None, text="move curry to friday"):
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = responses
        fake_anthropic = MagicMock()
        fake_anthropic.Anthropic.return_value = fake_client
        results = iter(tool_results)

        def fake_tool(name, tool_input, sess, config):
            result, asks = next(results)
            if asks:
                sess["state"] = "awaiting_recipe_confirmation"
            return result

        with patch.dict(sys.modules, {"anthropic": fake_anthropic}), \
             patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), \
             patch("agents.menu_workflow._execute_menu_tool", side_effect=fake_tool):
            reply = mw.menu_agent_reply(text, session=session if session is not None else {},
                                        config=FAKE_CONFIG)
        return reply, fake_client

    def test_confirmation_question_ends_the_turn(self):
        question = "Is this the right recipe? Test Curry - https://site/Test_Curry"
        reply, client = self._run([_tool_use_response(), _text_response("Yep, that's the one!")],
                                  [(question, True)])
        self.assertEqual(reply, question)
        self.assertEqual(client.messages.create.call_count, 1)
        self.assertEqual(self._session()["conversation"][-1]["content"], question)

    def test_failure_earlier_in_turn_is_reported_with_question(self):
        question = "Is this the right recipe? Test Curry - https://site/Test_Curry"
        reply, _ = self._run(
            [_tool_use_response(tool_id="a"), _tool_use_response(tool_id="b"), _text_response("All done!")],
            [("Couldn't make that swap: nope", False), (question, True)])
        self.assertEqual(reply, "Couldn't make that swap: nope\n\n" + question)

    def test_unclear_confirmation_reply_goes_to_agent_with_context(self):
        session = {"state": "awaiting_recipe_confirmation", "week_start": "2026-11-23",
                   "pending_confirmation": {"tool": "swap_meal", "args": {}, "options": ["Test Curry"]},
                   "pending_suggested": "Test Curry", "conversation": []}
        self._write_session(session)
        reply, client = self._run([_text_response("Got it, a red curry pasta then?")], [],
                                  session=session, text="Friday is not right, should be a pasta")
        self.assertEqual(reply, "Got it, a red curry pasta then?")
        self.assertNotEqual(self._session().get("state"), "awaiting_recipe_confirmation")

    def test_confirmed_pick_is_remembered_in_conversation(self):
        session = {"state": "awaiting_recipe_confirmation", "week_start": "2026-11-23",
                   "selected_meals": {"Fri": "Test Pasta"}, "quick_days": [],
                   "pending_confirmation": {"tool": "swap_meal", "args": {"day": "Fri"}, "options": ["Test Curry"]},
                   "pending_suggested": "Test Curry", "conversation": []}
        self._write_session(session)
        with patch("agents.menu_workflow.call_menubuilder_tool", return_value=SWAP_SUCCESS):
            mw.menu_agent_reply("yes", session=session, config=FAKE_CONFIG)
        convo = self._session()["conversation"]
        self.assertTrue(convo[-2]["content"].endswith("] yes"))
        self.assertEqual(convo[-1]["role"], "assistant")


if __name__ == "__main__":
    unittest.main(verbosity=2)
