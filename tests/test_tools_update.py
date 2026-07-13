"""
Unit tests for _tool_update_meal_plan in tools.py.

Patches tools._update_plan so no real MCP calls or file writes occur.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import tools
from tests.mb_fixtures import (
    UPDATE_PLAN_ERROR,
    UPDATE_PLAN_NEEDS_CONFIRMATION,
    UPDATE_PLAN_SUCCESS,
)


class TestToolUpdateMealPlan(unittest.TestCase):

    def test_none_result_returns_parse_error(self):
        with patch("tools._update_plan", return_value=None):
            result = tools._tool_update_meal_plan("change xyz to something")
        self.assertIn("Couldn't parse", result)

    def test_needs_confirmation_returns_suggested_name(self):
        with patch("tools._update_plan", return_value=UPDATE_PLAN_NEEDS_CONFIRMATION):
            result = tools._tool_update_meal_plan("change Thursday to chicken tik")
        self.assertIn("Test Chicken Tikka Masala", result)
        self.assertIn("did you mean", result.lower())

    def test_needs_confirmation_asks_for_yes_or_exact_name(self):
        with patch("tools._update_plan", return_value=UPDATE_PLAN_NEEDS_CONFIRMATION):
            result = tools._tool_update_meal_plan("change Thursday to chicken tik")
        self.assertIn("yes", result.lower())
        self.assertIn("exact name", result.lower())

    def test_success_returns_updated_title(self):
        with patch("tools._update_plan", return_value=UPDATE_PLAN_SUCCESS):
            result = tools._tool_update_meal_plan("change Thursday to chicken tacos")
        self.assertIn("Updated", result)
        self.assertIn("Test Chicken Tacos", result)

    def test_mcp_error_returns_error_message(self):
        with patch("tools._update_plan", return_value=UPDATE_PLAN_ERROR):
            result = tools._tool_update_meal_plan("change Thursday to xyz")
        self.assertIn("Couldn't update", result)
        self.assertIn("not found", result.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
