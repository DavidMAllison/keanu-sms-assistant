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


class TestAgentConfirmationEndsTurn(unittest.TestCase):
    """A needs_confirmation tool result is sent as the reply and ends the turn."""

    def test_question_sent_and_model_not_called_again(self):
        import agent
        agent._conversation_history.clear()
        agent._last_seen.clear()
        block = mock.MagicMock(type="tool_use", input={"day": "Mon 11/23"}, id="tu_1")
        block.name = "swap_meal"
        response = mock.MagicMock(stop_reason="tool_use", content=[block])
        create = mock.MagicMock(return_value=response)
        result = ("needs_confirmation: Which one did you mean?\n1. A - u\n2. B - v\n"
                  "Send that to the user word for word. After they pick, call swap_meal again.")
        with mock.patch.object(agent, "_client", mock.MagicMock(messages=mock.MagicMock(create=create))), \
             mock.patch.object(agent, "load_context", mock.MagicMock(return_value="")), \
             mock.patch.object(agent, "execute_tool", return_value=result):
            reply = agent.get_reply(KID, "swap monday", CONFIG)
        self.assertEqual(reply, "Which one did you mean?\n1. A - u\n2. B - v")
        self.assertEqual(create.call_count, 1)
        self.assertEqual(agent._conversation_history[KID][-1]["content"], reply)


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
        self.assertEqual(self.say(DAVID, "keanu go"),
                         ["Need at least 2 players. Dad's in so far. Reply \"in\" to join."])

    def test_story_start_while_waiting_joins(self):
        self.say(KID, "@keanu story")
        self.assertEqual(self.say(DAVID, "Keanu story "), ["Dad's in! (2 players)"])

    def test_mention_while_waiting_gets_status(self):
        self.say(KID, "@keanu story")
        self.say(FRIEND, "in Sam")
        self.assertEqual(self.say(KID, "keanu"),
                         ["Waiting to start: Kiddo and Sam are in. Reply \"in\" to join, or \"keanu go\" to begin."])
        self.assertIn("Waiting to start", self.say(DAVID, "keanu what do I do")[0])

    def test_chatter_while_waiting_stays_quiet(self):
        self.say(KID, "@keanu story")
        self.assertEqual(self.say(KID, "SAY IN"), [])
        self.assertEqual(self.say(KID, "go"), [])

    def test_friend_cannot_go_or_cancel(self):
        self.say(DAVID, "keanu story")
        self.say(FRIEND, "in Sam")
        self.assertEqual(self.say(FRIEND, "keanu go"), [])
        self.assertEqual(self.say(FRIEND, "keanu stop"), [])
        self.assertEqual(group_games.load_game(GROUP)["phase"], "joining")

    def test_family_join_name_overrides_config_name(self):
        self.say(DAVID, "keanu story")
        self.assertEqual(self.say(KID, "in WeWishYouADinoChristmas"),
                         ["WeWishYouADinoChristmas's in! (2 players)"])
        self.assertEqual(self.say(FRIEND, "in sam"), ["Sam's in! (3 players)"])
        self.say(DAVID, "keanu go")
        self.say(DAVID, "Line one.")
        self.assertIn("WeWishYouADinoChristmas's turn", self.say(DAVID, "keanu")[0])

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

class TestImposter(_GameDirCase):
    """Three players; Kiddo is always dealt the imposter, the word is BEACH."""

    def setUp(self):
        super().setUp()
        self.private = []
        self.fail_for = set()
        for target, kw in (("_pick_secret", {"return_value": ("Places", "beach")}),):
            p = mock.patch.object(group_games, target, **kw)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(group_games.random, "choice", side_effect=lambda seq: seq[1])
        p.start()
        self.addCleanup(p.stop)

    def send_private(self, handle, text):
        self.private.append((handle, text))
        return handle not in self.fail_for

    def say(self, handle, text):
        return group_games.handle_group_game_message(
            GROUP, handle, text, is_family=handle in CONFIG["security"]["allowed_numbers"],
            config=CONFIG, send_private=self.send_private)

    def _deal(self):
        self.say(DAVID, "@keanu imposter")
        self.say(KID, "in")
        self.say(FRIEND, "in Sam")
        return self.say(DAVID, "keanu go")

    def _clues(self):
        self._deal()
        for i in range(6):
            out = self.say((DAVID, KID, FRIEND)[i % 3], f"clue{i}")
        return out

    def test_needs_three_players(self):
        self.say(DAVID, "@keanu imposter")
        self.say(KID, "in")
        self.assertEqual(self.say(DAVID, "keanu go"),
                         ["Need at least 3 players. Dad and Kiddo are in so far. Reply \"in\" to join."])
        self.assertEqual(self.private, [])

    def test_private_texts_only_to_players_with_right_content(self):
        out = self._deal()
        self.assertIn("check your texts", out[0])
        self.assertTrue(out[0].endswith("Dad, you're up!"))
        sent = dict(self.private)
        self.assertEqual(set(sent), {DAVID, KID, FRIEND})
        self.assertIn("BEACH", sent[DAVID])
        self.assertIn("BEACH", sent[FRIEND])
        self.assertIn("IMPOSTER", sent[KID])
        self.assertIn("Places", sent[KID])
        self.assertNotIn("BEACH", sent[KID])
        self.assertNotIn("BEACH", out[0])

    def test_failed_private_text_cancels(self):
        self.fail_for = {FRIEND}
        out = self._deal()
        self.assertIn("couldn't text Sam privately", out[0])
        self.assertIsNone(group_games.load_game(GROUP))

    def test_secret_never_logged(self):
        with self.assertLogs(group_games.log, level="INFO") as logs:
            self._deal()
        self.assertFalse(any("beach" in line.lower() for line in logs.output))

    def test_clue_round_then_vote_prompt_with_recap(self):
        self._deal()
        self.assertEqual(self.say(KID, "out of turn"), [])
        self.assertEqual(self.say(DAVID, "sand"), ["Kiddo, you're up! (clue 2/6)"])
        for i in range(1, 6):
            out = self.say((DAVID, KID, FRIEND)[i % 3], f"clue{i}")
        out = out[0]
        self.assertIn("Dad: sand, clue3", out)
        self.assertIn("Kiddo: clue1, clue4", out)
        self.assertIn("Vote!", out)

    def test_caught_then_correct_guess(self):
        self._clues()
        self.assertEqual(self.say(DAVID, "kiddo"), ["Got it, Dad! (1/3 voted)"])
        self.say(FRIEND, "I think Kiddo")
        out = self.say(KID, "Dad")[0]
        self.assertIn("Busted! Kiddo was the imposter!", out)
        self.assertIn("Votes: Kiddo 2, Dad 1", out)
        self.assertIn("steals the win", self.say(KID, "the beaches")[0])
        self.assertIsNone(group_games.load_game(GROUP))

    def test_caught_then_wrong_guess(self):
        self._clues()
        self.say(DAVID, "Kiddo")
        self.say(FRIEND, "Kiddo")
        self.say(KID, "Sam")
        self.assertEqual(self.say(DAVID, "beach"), [])  # only the imposter guesses
        self.assertEqual(self.say(KID, "pool"), ["Nope! The word was BEACH. Everyone else wins!"])

    def test_wrong_person_imposter_wins(self):
        self._clues()
        self.say(DAVID, "Sam")
        self.say(KID, "Sam")
        out = self.say(FRIEND, "Dad")[0]
        self.assertIn("Wrong person!", out)
        self.assertIn("The imposter was Kiddo and the word was BEACH. Kiddo wins!", out)

    def test_tie_imposter_escapes(self):
        self._clues()
        self.say(DAVID, "Sam")
        self.say(KID, "Dad")
        out = self.say(FRIEND, "Kiddo")[0]
        self.assertIn("tie", out)
        self.assertIn("Kiddo wins!", out)

    def test_self_vote_rejected_and_double_vote_ignored(self):
        self._clues()
        self.assertIn("can't vote for yourself", self.say(DAVID, "Dad")[0])
        self.say(DAVID, "Kiddo")
        self.assertEqual(self.say(DAVID, "Sam"), [])
        self.assertEqual(group_games.load_game(GROUP)["votes"], {DAVID: KID})

    def test_voting_status_and_reveal(self):
        self._clues()
        self.say(DAVID, "Kiddo")
        self.assertEqual(self.say(FRIEND, "keanu"),
                         ["Waiting on votes from Kiddo, Sam. Reply with who you think the imposter is."])
        self.assertEqual(self.say(FRIEND, "keanu reveal"), [])  # friends can't force it
        self.assertIn("Busted!", self.say(DAVID, "keanu reveal")[0])

    def test_stop_reveals(self):
        self._deal()
        self.assertEqual(self.say(KID, "keanu stop"),
                         ["Game stopped. The imposter was Kiddo and the word was BEACH."])

    def test_expiry_notice_reveals(self):
        self._deal()
        with mock.patch.object(group_games.time, "time",
                               return_value=time.time() + group_games.PLAY_TIMEOUT + 1):
            out = self.say(DAVID, "hello")
        self.assertEqual(out, ["Our Imposter game timed out. The imposter was Kiddo and the word was BEACH."])

    def test_game_start_parsing(self):
        self.assertEqual(group_games.game_start("@Keanu imposter!"), "imposter")
        self.assertEqual(group_games.game_start("keanu impostor"), "imposter")
        self.assertEqual(group_games.game_start("keanu story"), "story_chain")
        self.assertIsNone(group_games.game_start("imposter"))

    def test_normalize_word(self):
        self.assertEqual(group_games._normalize_word("The Beaches!"), "beach")
        self.assertEqual(group_games._normalize_word("is it a pizza"), "pizza")


class TestEmojiGuess(_GameDirCase):
    PUZZLES = ["Frozen", "Up", "The Lion King", "Coco", "Shrek"]

    def setUp(self):
        super().setUp()
        p = mock.patch.object(group_games, "_pick_puzzles", return_value=list(self.PUZZLES))
        p.start()
        self.addCleanup(p.stop)

    def test_starts_without_lobby(self):
        out = self.say(DAVID, "keanu emoji")
        self.assertIn("Emoji Guess!", out[0])
        self.assertTrue(out[0].endswith("Round 1/5: ❄️👸⛄"))

    def test_full_game_scores_and_winner(self):
        self.say(DAVID, "keanu emoji")
        self.assertIn("Yes! Kiddo got it: Frozen.", self.say(KID, "frozen!")[0])
        self.say(FRIEND, "up")
        self.say(KID, "the lion king")
        self.say(DAVID, "coco")
        out = self.say(KID, "is it shrek")
        self.assertIn("Scores: Kiddo 3, Dad 1, Player 3 1. Kiddo wins!", out[0])
        self.assertIsNone(group_games.load_game(GROUP))

    def test_wrong_guess_and_chatter_stay_quiet(self):
        self.say(DAVID, "keanu emoji")
        self.assertEqual(self.say(KID, "moana?"), [])
        self.assertEqual(self.say(KID, "lol"), [])
        self.assertIn("Round 1/5", self.say(KID, "keanu?")[0])

    def test_hints_then_skip(self):
        self.say(DAVID, "keanu emoji")
        self.say(KID, "keanu skip")  # friend-or-family control: Kiddo is family
        self.assertEqual(self.say(FRIEND, "keanu hint"), ["Hint: 1 word, starting with U."])
        self.assertEqual(self.say(FRIEND, "keanu hint"), ["Hint: U_"])
        self.assertIn("No more hints", self.say(FRIEND, "keanu hint")[0])
        self.assertEqual(self.say(FRIEND, "keanu skip"), [])
        out = self.say(DAVID, "keanu skip")
        self.assertIn("Skipped! It was Up.", out[0])
        self.assertIn("Round 3/5", out[0])

    def test_friend_names_themselves_mid_game(self):
        self.say(DAVID, "keanu emoji")
        self.assertEqual(self.say(FRIEND, "in Sam"), ["Sam's in!"])
        self.assertIn("Sam got it", self.say(FRIEND, "frozen")[0])

    def test_stop_shows_answer(self):
        self.say(DAVID, "keanu emoji")
        self.assertIn("The answer was Frozen.", self.say(DAVID, "keanu stop")[0])
        self.assertIsNone(group_games.load_game(GROUP))

    def test_title_matching(self):
        match = group_games._is_title_guess
        self.assertTrue(match("omg its frozen!!", ("Frozen",)))
        self.assertTrue(match("lion king", ("The Lion King",)))
        self.assertTrue(match("ratatoullie", ("Ratatouille",)))
        self.assertTrue(match("Monsters Inc", ("Monsters, Inc.",)))
        self.assertTrue(match("up", ("Up",)))
        self.assertFalse(match("what's up", ("Up",)))
        self.assertFalse(match("lion", ("The Lion King",)))

    def test_every_puzzle_title_matches_itself(self):
        for emojis, title, aliases in group_games._EMOJI_PUZZLES:
            for answer in (title,) + aliases:
                self.assertTrue(group_games._is_title_guess(answer, (title,) + aliases), answer)


class TestEmojiPuzzlePicks(_GameDirCase):
    def test_no_repeats_until_list_runs_low(self):
        seen = []
        rounds = len(group_games._EMOJI_PUZZLES) // group_games.EMOJI_ROUNDS
        for _ in range(rounds):
            seen += group_games._pick_puzzles(GROUP)
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(group_games._pick_puzzles(GROUP)), group_games.EMOJI_ROUNDS)


class TestHangman(_GameDirCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(group_games, "_HANGMAN_WORDS", ("puppy",))
        p.start()
        self.addCleanup(p.stop)

    def test_start_shows_board(self):
        out = self.say(DAVID, "keanu hangman")
        self.assertIn("_ _ _ _ _\nLives: 6/6", out[0])

    def test_letters_then_win(self):
        self.say(DAVID, "keanu hangman")
        self.assertIn("Yes, there's P!\n\nP _ P P _", self.say(KID, "p")[0])
        out = self.say(KID, "z")
        self.assertIn("No Z!", out[0])
        self.assertIn("Lives: 5/6\nWrong: Z", out[0])
        self.assertIn("already guessed", self.say(DAVID, "P")[0])
        self.say(FRIEND, "u")
        out = self.say(FRIEND, "y")
        self.assertEqual(out, ["Player 2 got it! The word was PUPPY, with 5 lives to spare."])
        self.assertIsNone(group_games.load_game(GROUP))

    def test_word_guesses(self):
        self.say(DAVID, "keanu hangman")
        self.assertIn("Not KITTY!", self.say(KID, "guess kitty")[0])
        self.assertIn("already guessed", self.say(KID, "guess kitty")[0])
        self.assertIn("got it", self.say(KID, "puppy")[0])

    def test_chatter_ignored_and_mention_shows_board(self):
        self.say(DAVID, "keanu hangman")
        self.assertEqual(self.say(KID, "this is hard"), [])
        self.assertIn("Lives: 6/6", self.say(KID, "keanu")[0])

    def test_out_of_lives(self):
        self.say(DAVID, "keanu hangman")
        for letter in "abcde":
            self.say(KID, letter)
        self.assertEqual(self.say(KID, "f"), ["Out of lives! The word was PUPPY. I win this one!"])

    def test_word_never_logged(self):
        with self.assertLogs(group_games.log, level="INFO") as logs:
            self.say(DAVID, "keanu hangman")
            self.say(DAVID, "keanu stop")
        self.assertFalse(any("puppy" in line.lower() for line in logs.output))

    def test_expiry_notice_reveals(self):
        self.say(DAVID, "keanu hangman")
        with mock.patch.object(group_games.time, "time", return_value=time.time() + 2 * 86400):
            out = self.say(KID, "a")
        self.assertEqual(out, ["Our Hangman game timed out. The word was PUPPY."])


class TestBalderdash(_GameDirCase):
    """Dad, Kiddo and Sam; one word, ZARF; no shuffle, so fakes come first in
    submission order and the real definition last."""

    def setUp(self):
        super().setUp()
        self.private = []
        self.fail_for = set()
        for target, value in (("_BALDERDASH_WORDS", (("zarf", "A cup holder"),)),):
            p = mock.patch.object(group_games, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(group_games.random, "shuffle", side_effect=lambda seq: None)
        p.start()
        self.addCleanup(p.stop)

    def send_private(self, handle, text):
        self.private.append((handle, text))
        return handle not in self.fail_for

    def say(self, handle, text):
        return group_games.handle_group_game_message(
            GROUP, handle, text, is_family=handle in CONFIG["security"]["allowed_numbers"],
            config=CONFIG, send_private=self.send_private)

    def dm(self, handle, text):
        return group_games.handle_private_game_message(handle, text)

    def _deal(self):
        self.say(DAVID, "keanu balderdash")
        self.say(KID, "in")
        self.say(FRIEND, "in Sam")
        return self.say(DAVID, "keanu go")

    def _write(self):
        self._deal()
        self.dm(DAVID, "a kind of hat")
        self.dm(KID, "a tiny dragon.")
        return self.dm(FRIEND, "a dance move")

    def test_needs_three_players(self):
        self.say(DAVID, "fake definitions keanu")
        self.say(KID, "in")
        self.assertIn("Need at least 3 players", self.say(DAVID, "keanu go")[0])

    def test_deal_texts_every_player_privately(self):
        out = self._deal()
        self.assertIn("Round 1/3: what does ZARF mean?", out[0])
        self.assertEqual({h for h, _ in self.private}, {DAVID, KID, FRIEND})
        self.assertTrue(all("ZARF" in t and "not in the group" in t for _, t in self.private))

    def test_failed_private_text_stops(self):
        self.fail_for = {FRIEND}
        self.assertIn("couldn't text Sam privately", self._deal()[0])
        self.assertIsNone(group_games.load_game(GROUP))

    def test_private_capture_only_for_players_who_owe_one(self):
        self._deal()
        self.assertIsNone(self.dm(STRANGER, "hello"))
        self.assertEqual(self.dm(FRIEND, "a dance move"), (GROUP, "Got it! Head back to the group chat.", []))
        self.assertIsNone(self.dm(FRIEND, "another one"))
        self.assertIsNone(self.dm(KID, "   "))

    def test_no_capture_outside_writing_or_without_game(self):
        self.assertIsNone(self.dm(KID, "what's for dinner"))
        self._write()
        self.assertIsNone(self.dm(KID, "what's for dinner"))

    def test_last_definition_posts_vote(self):
        chat, reply, group = self._write()
        self.assertEqual(chat, GROUP)
        self.assertIn("Voting's starting", reply)
        self.assertIn("1. A kind of hat\n2. A tiny dragon\n3. A dance move\n4. A cup holder", group[0])

    def test_status_while_writing_and_reveal(self):
        self._deal()
        self.dm(KID, "a tiny dragon")
        self.assertIn("Waiting on fake definitions from Dad, Sam", self.say(KID, "keanu?")[0])
        self.assertEqual(self.say(FRIEND, "keanu reveal"), [])
        out = self.say(DAVID, "keanu reveal")
        self.assertIn("1. A tiny dragon\n2. A cup holder", out[0])

    def test_votes_score_and_next_round(self):
        self._write()
        self.assertEqual(self.say(DAVID, "1"), ["That's your own! Pick another."])
        self.assertEqual(self.say(DAVID, "9"), ["Pick a number from 1 to 4."])
        self.say(DAVID, "4")
        self.say(KID, "1")
        out = self.say(FRIEND, "#4")
        self.assertIn("ZARF really means: A cup holder (number 4).", out[0])
        self.assertIn("Found it (+2): Dad, Sam.", out[0])
        self.assertIn("Dad's fake fooled Kiddo (+1).", out[0])
        self.assertIn("Scores: Dad 3, Sam 2, Kiddo 0", out[0])
        self.assertIn("Round 2/3", out[1])

    def test_three_rounds_then_winner(self):
        for _ in range(3):
            if group_games.load_game(GROUP) is None:
                self._deal()
            for h in (DAVID, KID, FRIEND):
                self.dm(h, "a guess")
            self.say(DAVID, "4")
            self.say(KID, "4")
            out = self.say(FRIEND, "4")
        self.assertIn("That's the game! It's a tie between Dad and Kiddo and Sam!", out[0])
        self.assertIsNone(group_games.load_game(GROUP))

    def test_non_player_and_chatter_cannot_vote(self):
        self._write()
        self.assertEqual(self.say(STRANGER, "4"), [])
        self.assertEqual(self.say(KID, "hmm tough one"), [])
        self.say(KID, "4")
        self.assertEqual(self.say(KID, "1"), [])

    def test_game_start_parsing(self):
        self.assertEqual(group_games.game_start("keanu balderdash"), "balderdash")
        self.assertEqual(group_games.game_start("@Keanu fake definitions"), "balderdash")
        self.assertEqual(group_games.game_start("keanu hang man"), "hangman")
        self.assertEqual(group_games.game_start("keanu emojis!"), "emoji")


if __name__ == "__main__":
    unittest.main()
