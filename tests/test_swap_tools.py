"""
Post-finalize swap tools in tools.py: switch_days (wraps meal_swap.reorder_days)
and swap_meal's already_planned handling. meal_swap is replaced with a fake
module so nothing touches the real plan, CSV or calendar.
"""
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import tools


def _fake_meal_swap(**funcs) -> ModuleType:
    mod = ModuleType("meal_swap")
    for name, fn in funcs.items():
        setattr(mod, name, fn)
    return mod


class TestSwitchDays(unittest.TestCase):

    def test_success_returns_message_and_passes_both_days(self):
        reorder = MagicMock(return_value={
            "success": True,
            "message": "Switched: Tue is now Test Salad, Wed is now Test Miso.",
        })
        with patch.dict(sys.modules, {"meal_swap": _fake_meal_swap(reorder_days=reorder)}):
            reply = tools._tool_switch_days("Tue 10/27", "Wed 10/28")
        reorder.assert_called_once_with("Tue 10/27", "Wed 10/28")
        self.assertIn("Switched", reply)

    def test_failure_surfaces_message(self):
        reorder = MagicMock(return_value={"success": False, "message": "Need two different planned days"})
        with patch.dict(sys.modules, {"meal_swap": _fake_meal_swap(reorder_days=reorder)}):
            reply = tools._tool_switch_days("Tue 10/27", "Tue 10/27")
        self.assertTrue(reply.startswith("Switch failed"))
        self.assertIn("two different", reply)

    def test_exception_is_caught(self):
        reorder = MagicMock(side_effect=RuntimeError("boom"))
        with patch.dict(sys.modules, {"meal_swap": _fake_meal_swap(reorder_days=reorder)}):
            reply = tools._tool_switch_days("Tue 10/27", "Wed 10/28")
        self.assertIn("boom", reply)

    def test_offered_to_idea_submitters_only(self):
        submitter = [t["name"] for t in tools.build_tool_list(False, False, True)]
        other_adult = [t["name"] for t in tools.build_tool_list(False, False, False)]
        self.assertIn("switch_days", submitter)
        self.assertNotIn("switch_days", other_adult)


class TestSwapMealAlreadyPlanned(unittest.TestCase):

    def test_already_planned_points_to_switch_days(self):
        swap = MagicMock(return_value={
            "success": False,
            "message": "already_planned",
            "detail": "Test Salad is already on Wed.",
        })
        with patch.dict(sys.modules, {"meal_swap": _fake_meal_swap(execute_swap=swap)}), \
             patch("tools.find_all_recipe_matches", return_value=[{"name": "Test Salad"}]):
            reply = tools._tool_swap_meal("Tue 10/27", "Test Miso", "Test Salad")
        self.assertTrue(reply.startswith("already_planned"))
        self.assertIn("already on Wed", reply)
        self.assertIn("switch_days", reply)


if __name__ == "__main__":
    unittest.main()
