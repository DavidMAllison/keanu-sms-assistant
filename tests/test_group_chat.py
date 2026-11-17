"""
Group chats: messages from a group are kept apart from the sender's 1:1 turns,
answered only in the group (never privately), only when the chat includes a
family member, and only for games. Story Chain state is chat-scoped and lives
on disk so a restart doesn't end a game.
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import group_games
import server

DAVID = "+15550000001"
KID = "+15550000003"
FRIEND = "+15550000099"
STRANGER = "+15550000098"
GROUP = "any;+;c1b2c554247c49c5a9651ea61766ccfb"
T0 = 1_800_000_000.0

CONFIG = {
    "security": {
        "allowed_numbers": [DAVID, KID],
        "rate_limit_per_hour": 50,
        "handle_to_person": {DAVID: "Dad", KID: "Kiddo"},
    },
    "agents": {"menu": {"enabled": True}},
}


def _msg(rowid, text, handle=DAVID, chat_guid=GROUP, at=0.0):
    return {"rowid": rowid, "text": text, "handle": handle, "chat_guid": chat_guid,
            "is_group": chat_guid == GROUP, "date": T0 + at, "attachments": []}


class TestCoalesceKeepsChatsApart(unittest.TestCase):
    def test_group_and_dm_from_same_sender_not_merged(self):
        msgs = [_msg(1, "keanu story time"), _msg(2, "what's for dinner", chat_guid="any;-;" + DAVID, at=1)]
        out = server.coalesce_messages(msgs, T0 + 60)
        self.assertEqual([m["rowid"] for m in out], [1, 2])

    def test_same_chat_still_merged(self):
        out = server.coalesce_messages([_msg(1, "hello"), _msg(2, "there", at=1)], T0 + 60)
        self.assertEqual(len(out), 1)


class _GameDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patches = [
            mock.patch.object(group_games, "GAME_STATE_DIR", Path(self.tmp.name)),
            mock.patch.object(group_games, "_opening_line", return_value="A koala found a key."),
            mock.patch.object(group_games, "_ending_line", return_value="The end came."),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def say(self, handle, text):
        return group_games.handle_group_game_message(
            GROUP, handle, text, is_family=handle in CONFIG["security"]["allowed_numbers"],
            config=CONFIG)


class TestGroupRouting(_GameDirCase):
    def setUp(self):
        super().setUp()
        server._rate_limit_store.clear()
        self.sent = []
        for target, kw in (("send_imessage_chat", {"side_effect": lambda g, t: self.sent.append((g, t))}),
                           ("send_imessage", {}), ("get_reply", {})):
            p = mock.patch.object(server, target, **kw)
            setattr(self, target, p.start())
            self.addCleanup(p.stop)

    def route(self, msg, participants):
        with mock.patch.object(server, "get_chat_participants", return_value=participants):
            server.handle_group_message(msg, CONFIG)

    def test_family_start_replies_in_group_only(self):
        self.route(_msg(1, "@Keanu let's do a story"), [DAVID, KID])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], GROUP)
        self.send_imessage.assert_not_called()

    def test_family_chat_mention_goes_to_agent_in_group(self):
        self.get_reply.return_value = "Tacos tonight!"
        self.route(_msg(1, "keanu what's for dinner"), [DAVID, KID])
        self.get_reply.assert_called_once_with(
            DAVID, "keanu what's for dinner", CONFIG,
            history_key="chat:" + GROUP, group_names=["Dad", "Kiddo"])
        self.assertEqual(self.sent, [(GROUP, "Tacos tonight!")])
        self.send_imessage.assert_not_called()

    def test_friend_in_chat_gets_games_only_reply(self):
        self.route(_msg(1, "keanu what's for dinner"), [DAVID, FRIEND])
        self.get_reply.assert_not_called()
        self.assertEqual(self.sent, [(GROUP, server.GROUP_GAMES_ONLY_REPLY)])

    def test_friend_asking_gets_games_only_reply(self):
        self.route(_msg(1, "keanu where do you live", handle=FRIEND), [DAVID, FRIEND])
        self.get_reply.assert_not_called()
        self.assertEqual(self.sent, [(GROUP, server.GROUP_FRIEND_REPLY)])

    def test_chatter_without_mention_ignored(self):
        self.route(_msg(1, "what's for dinner"), [DAVID, KID])
        self.assertEqual(self.sent, [])
        self.get_reply.assert_not_called()

    def test_running_game_takes_priority_over_agent(self):
        self.route(_msg(1, "keanu story"), [DAVID, KID])
        self.route(_msg(2, "keanu what's for dinner"), [DAVID, KID])
        self.get_reply.assert_not_called()

    def test_chat_without_family_ignored(self):
        self.route(_msg(1, "keanu story", handle=FRIEND), [FRIEND, STRANGER])
        self.assertEqual(self.sent, [])

    def test_friend_cannot_start_game(self):
        self.route(_msg(1, "keanu story", handle=FRIEND), [FRIEND, KID])
        self.assertEqual(self.sent, [(GROUP, server.GROUP_FRIEND_REPLY)])
        self.assertIsNone(group_games.load_game(GROUP))

    def test_friend_can_join_game_in_family_chat(self):
        self.route(_msg(1, "keanu story"), [DAVID, FRIEND])
        self.route(_msg(2, "in sam", handle=FRIEND), [DAVID, FRIEND])
        self.assertEqual(self.sent[-1][1], "Sam's in! (2 players)")

    def test_empty_text_ignored(self):
        self.route(_msg(1, ""), [DAVID])
        self.assertEqual(self.sent, [])


class TestAgentGroupReply(unittest.TestCase):
    """agent.get_reply in a group: own history, sender-labeled turns, no menu start."""

    def setUp(self):
        import agent
        self.agent = agent
        agent._conversation_history.clear()
        agent._last_seen.clear()
        response = mock.MagicMock(stop_reason="end_turn")
        response.content = [mock.MagicMock(text="Tacos!")]
        self.create = mock.MagicMock(return_value=response)
        for target, value in (("_client", mock.MagicMock(messages=mock.MagicMock(create=self.create))),
                              ("load_context", mock.MagicMock(return_value="Tonight: tacos"))):
            p = mock.patch.object(agent, target, value)
            p.start()
            self.addCleanup(p.stop)
        self.config = dict(CONFIG, security=dict(CONFIG["security"], menu_admin=DAVID))

    def test_group_history_kept_apart_and_labeled(self):
        self.agent.get_reply(DAVID, "keanu dinner?", self.config,
                             history_key="chat:" + GROUP, group_names=["Dad", "Kiddo"])
        self.assertNotIn(DAVID, self.agent._conversation_history)
        turn = self.agent._conversation_history["chat:" + GROUP][0]["content"]
        self.assertTrue(turn.endswith("] Dad: keanu dinner?"))
        self.assertIn("family group text with Dad, Kiddo", self.create.call_args[1]["system"])

    def test_group_drops_start_menu_workflow(self):
        self.agent.get_reply(DAVID, "keanu plan the menu", self.config,
                             history_key="chat:" + GROUP, group_names=["Dad", "Kiddo"])
        names = [t["name"] for t in self.create.call_args[1]["tools"]]
        self.assertNotIn("start_menu_workflow", names)

    def test_one_to_one_unchanged(self):
        self.agent.get_reply(DAVID, "dinner?", self.config)
        names = [t["name"] for t in self.create.call_args[1]["tools"]]
        self.assertIn("start_menu_workflow", names)
        self.assertNotIn("Group chat", self.create.call_args[1]["system"])
        self.assertTrue(self.agent._conversation_history[DAVID][0]["content"].endswith("] dinner?"))


class TestParsing(unittest.TestCase):
    def test_start_needs_mention_and_story(self):
        self.assertTrue(group_games.is_story_start("@Keanu story chain!"))
        self.assertTrue(group_games.is_story_start("keanu, story time"))
        self.assertFalse(group_games.is_story_start("tell me a story"))
        self.assertFalse(group_games.is_story_start("keanu hi"))

    def test_commands_must_be_whole_message(self):
        self.assertEqual(group_games.parse_command("@keanu go"), "go")
        self.assertEqual(group_games.parse_command("Keanu, stop!"), "stop")
        self.assertEqual(group_games.parse_command("keanu skip"), "skip")
        self.assertIsNone(group_games.parse_command("and then keanu said stop the car"))

    def test_join(self):
        self.assertEqual(group_games.parse_join("in"), "")
        self.assertEqual(group_games.parse_join("I'm in!"), "")
        self.assertEqual(group_games.parse_join("in Sam"), "Sam")
        self.assertIsNone(group_games.parse_join("inside the cave"))
        self.assertIsNone(group_games.parse_join("the dragon was in the cave"))


class TestStoryChain(_GameDirCase):
    def test_full_game(self):
        self.assertIn("Story Chain", self.say(DAVID, "keanu story")[0])
        self.assertEqual(self.say(KID, "in"), ["Kiddo's in! (2 players)"])
        self.assertEqual(self.say(KID, "in"), [])  # already joined
        start = self.say(DAVID, "keanu go")[0]
        self.assertIn("Dad → Kiddo", start)
        self.assertIn("A koala found a key.", start)
        self.assertTrue(start.endswith("Dad, you're up!"))

        self.assertEqual(self.say(KID, "out of turn"), [])
        lines = []
        for i in range(6):  # 2 players x 3 rounds
            handle = DAVID if i % 2 == 0 else KID
            lines.append(f"Line {i}.")
            out = self.say(handle, f"Line {i}.")
        self.assertIn("The end!", out[0])
        self.assertIn("A koala found a key. " + " ".join(lines) + " The end came.", out[0])
        self.assertIsNone(group_games.load_game(GROUP))

    def test_go_needs_two_players(self):
        self.say(DAVID, "keanu story")
        self.assertIn("at least 2", self.say(DAVID, "keanu go")[0])

    def test_friend_cannot_go_or_cancel(self):
        self.say(DAVID, "keanu story")
        self.say(FRIEND, "in Sam")
        self.assertEqual(self.say(FRIEND, "keanu go"), [])
        self.assertEqual(self.say(FRIEND, "keanu stop"), [])
        self.assertEqual(group_games.load_game(GROUP)["phase"], "joining")

    def test_friend_without_name_gets_numbered(self):
        self.say(DAVID, "keanu story")
        self.assertEqual(self.say(FRIEND, "in"), ["Player 2's in! (2 players)"])

    def test_skip_and_stop_early(self):
        self.say(DAVID, "keanu story")
        self.say(KID, "in")
        self.say(DAVID, "keanu go")
        self.assertEqual(self.say(KID, "keanu skip"), ["Skipping Dad. Kiddo, you're up!"])
        self.say(KID, "A dragon appeared.")
        out = self.say(DAVID, "keanu stop")[0]
        self.assertIn("so far", out)
        self.assertIn("A koala found a key. A dragon appeared. The end came.", out)
        self.assertIsNone(group_games.load_game(GROUP))

    def test_state_survives_reload(self):
        self.say(DAVID, "keanu story")
        self.say(KID, "in")
        game = group_games.load_game(GROUP)
        self.assertEqual([p["name"] for p in game["players"]], ["Dad", "Kiddo"])

    def _later(self, seconds):
        return mock.patch.object(group_games.time, "time", return_value=time.time() + seconds)

    def _playing(self):
        self.say(DAVID, "keanu story")
        self.say(KID, "in")
        self.say(DAVID, "keanu go")
        self.say(DAVID, "A tree grew skittles.")  # Kiddo's turn now (2/6)

    def test_unstarted_game_expires_silently(self):
        self.say(DAVID, "keanu story")
        with self._later(group_games.JOIN_TIMEOUT + 1):
            self.assertIsNone(self.say(KID, "in"))
        self.assertIsNone(group_games.load_game(GROUP))

    def test_game_survives_an_afternoon(self):
        self._playing()
        with self._later(4 * 60 * 60):
            self.assertEqual(self.say(KID, "It rained sour ones."), ["Dad, you're up! (3/6)"])

    def test_expired_game_announced_once_with_story(self):
        self._playing()
        with self._later(group_games.PLAY_TIMEOUT + 1):
            out = self.say(KID, "It rained sour ones.")
            self.assertEqual(len(out), 1)
            self.assertIn("timed out", out[0])
            self.assertIn("A koala found a key. A tree grew skittles.", out[0])
            self.assertIsNone(self.say(KID, "hello?"))

    def test_expired_notice_then_fresh_start(self):
        self._playing()
        with self._later(group_games.PLAY_TIMEOUT + 1):
            out = self.say(DAVID, "keanu story")
        self.assertEqual(len(out), 2)
        self.assertIn("timed out", out[0])
        self.assertIn("Story Chain!", out[1])

    def test_mention_out_of_turn_gets_status(self):
        self._playing()
        expected = ["It's Kiddo's turn (2/6). Commands: keanu skip, keanu stop."]
        self.assertEqual(self.say(DAVID, "Keanu who is next?"), expected)
        self.assertEqual(self.say(DAVID, "keanu"), expected)

    def test_out_of_turn_chat_stays_quiet(self):
        self._playing()
        self.assertEqual(self.say(DAVID, "lol good one"), [])

    def test_current_player_bare_mention_gets_status_not_a_line(self):
        self._playing()
        self.assertIn("Kiddo's turn", self.say(KID, "Keanu ")[0])
        self.assertIn("Kiddo's turn", self.say(KID, "keanu whose turn?")[0])
        self.assertEqual(len(group_games.load_game(GROUP)["lines"]), 1)

    def test_current_player_line_naming_keanu_still_counts(self):
        self._playing()
        self.assertEqual(self.say(KID, "Then Keanu the koala ate them all."),
                         ["Dad, you're up! (3/6)"])

    def test_turn_question_parsing(self):
        self.assertTrue(group_games.is_turn_question("@Keanu"))
        self.assertTrue(group_games.is_turn_question("keanu who's next?"))
        self.assertFalse(group_games.is_turn_question("Keanu died"))
        self.assertFalse(group_games.is_turn_question("no mention here, whose turn"))

if __name__ == "__main__":
    unittest.main()
