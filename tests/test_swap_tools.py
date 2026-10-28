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


class TestSwapMealConfirmation(unittest.TestCase):

    def _swap(self, return_value, confirmed=False):
        swap = MagicMock(return_value=return_value)
        with patch.dict(sys.modules, {"meal_swap": _fake_meal_swap(execute_swap=swap)}), \
             patch("tools.find_all_recipe_matches", return_value=[{"name": "Test Salad"}]):
            reply = tools._tool_swap_meal("Tue 10/27", "Test Miso", "test salad", confirmed=confirmed)
        return reply, swap

    def test_needs_confirmation_relays_question_verbatim(self):
        reply, _ = self._swap({
            "success": False,
            "message": "needs_confirmation",
            "detail": "Is this the right recipe? Test Salad - https://example.com/salad",
        })
        self.assertTrue(reply.startswith("needs_confirmation"))
        self.assertIn("Is this the right recipe? Test Salad - https://example.com/salad", reply)
        self.assertIn("confirmed=true", reply)

    def test_confirmed_defaults_false_and_passes_through(self):
        _, swap = self._swap({"success": True, "message": "ok"})
        self.assertFalse(swap.call_args.kwargs["confirmed"])
        _, swap = self._swap({"success": True, "message": "ok"}, confirmed=True)
        self.assertTrue(swap.call_args.kwargs["confirmed"])


class TestRecipeUrlExists(unittest.TestCase):

    def test_save_recipe_idea_exact_link_says_already_in_collection(self):
        with patch("tools._call_menubuilder_tool",
                   return_value={"status": "exists", "recipe": "Test Salad", "url": "https://example.com/s"}):
            reply = tools._tool_save_recipe_idea("https://example.com/s")
        self.assertIn("already in the collection", reply)
        self.assertNotIn("Couldn't fetch", reply)


class TestRecipeSimilarityListCuisine(unittest.TestCase):

    def test_list_valued_cuisine_does_not_crash(self):
        import json
        import tempfile
        meta = {"recipes": {"Test Three Cup Chicken": {
            "status": "active", "cuisine": ["Chinese", "Taiwanese"],
            "cooking_method": "stovetop", "ingredients": [{"name": "chicken"}],
        }}}
        with tempfile.TemporaryDirectory() as d:
            meta_file = Path(d) / "recipe_metadata.json"
            meta_file.write_text(json.dumps(meta))
            with patch("tools.METADATA_FILE", meta_file), patch("tools.IDEAS_DIR", Path(d)):
                reply = tools._tool_check_recipe_similarity("https://example.com/test-three-cup-chicken/")
        self.assertIn("Already in collection: Test Three Cup Chicken", reply)
        self.assertIn("Chinese, Taiwanese", reply)


class TestAddLunchRecipeUrl(unittest.TestCase):

    def _call(self, return_value, force_add=False):
        with patch("tools._call_menubuilder_tool", return_value=return_value) as mb:
            reply = tools._tool_add_lunch_recipe_url("https://example.com/l", force_add)
        return reply, mb

    def test_added_includes_name_and_url(self):
        reply, mb = self._call({"ok": True, "recipe": "Test Grain Bowl", "url": "https://example.com/bowl",
                                "message": "Added 'Test Grain Bowl' as a lunch recipe."})
        mb.assert_called_once_with("add_lunch_recipe_url", url="https://example.com/l", force_add=False)
        self.assertIn("Test Grain Bowl", reply)
        self.assertIn("\nhttps://example.com/bowl", reply)

    def test_exact_link_already_in_collection(self):
        reply, _ = self._call({"ok": True, "recipe": "Test Grain Bowl", "url": "https://example.com/bowl",
                               "message": "'Test Grain Bowl' is already in the collection -- marked it for lunches."})
        self.assertIn("already in the collection", reply)
        self.assertIn("https://example.com/bowl", reply)

    def test_similar_exists_asks_and_points_to_force_add(self):
        reply, _ = self._call({"ok": False, "status": "similar_exists", "recipe": "Test Grain Bowl",
                               "match_score": 0.82, "url": "https://example.com/bowl", "message": "..."})
        self.assertTrue(reply.startswith("similar_exists"))
        self.assertIn("82%", reply)
        self.assertIn("force_add=true", reply)
        self.assertIn("set_lunch_pick", reply)

    def test_fetch_failed_asks_for_paste(self):
        reply, _ = self._call({"ok": False, "recipe": "", "url": "https://example.com/l",
                               "message": "Page has no ingredients"})
        self.assertTrue(reply.startswith("fetch_failed"))
        self.assertIn("Page has no ingredients", reply)
        self.assertIn("paste", reply)

    def test_force_add_passes_through(self):
        _, mb = self._call({"ok": True, "recipe": "X", "url": "", "message": "Added"}, force_add=True)
        self.assertTrue(mb.call_args.kwargs["force_add"])

    def test_bridge_error(self):
        reply, _ = self._call({"error": "timeout"})
        self.assertIn("went wrong", reply)

    def test_offered_to_idea_submitters_only(self):
        submitter = [t["name"] for t in tools.build_tool_list(False, False, True)]
        other_adult = [t["name"] for t in tools.build_tool_list(False, False, False)]
        self.assertIn("add_lunch_recipe_url", submitter)
        self.assertNotIn("add_lunch_recipe_url", other_adult)


if __name__ == "__main__":
    unittest.main()
