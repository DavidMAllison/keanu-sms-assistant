"""
server.coalesce_messages: back-to-back messages from one sender are merged into
one turn, and a sender's latest message is held until they've gone quiet (4s,
or 15s after a lead-in like "Let's add this recipe:") so a link that iMessage
delivers as its own message isn't answered separately.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import server

DAVID = "+15550000001"
PARTNER = "+15550000002"
T0 = 1_800_000_000.0


def _msg(rowid, text, handle=DAVID, at=0.0, images=0):
    return {
        "rowid": rowid,
        "text": text,
        "handle": handle,
        "date": T0 + at,
        "attachments": [{"filename": f"img{rowid}.jpg", "mime_type": "image/jpeg"}] * images,
    }


class TestIsLeadIn(unittest.TestCase):

    def test_lead_ins(self):
        for text in ("Let’s add this recipe:", "This is it:", "here's a recipe", "Check this out"):
            self.assertTrue(server.is_lead_in(text), text)

    def test_not_lead_ins(self):
        for text in ("what's for dinner?", "https://example.com/r", "add this recipe: https://example.com/r", ""):
            self.assertFalse(server.is_lead_in(text), text)


class TestCoalesceMessages(unittest.TestCase):

    def test_lead_in_and_link_ten_seconds_apart_merge(self):
        msgs = [_msg(1, "Let’s add this recipe:"), _msg(2, "https://example.com/r", at=10)]
        ready = server.coalesce_messages(msgs, now=T0 + 30)
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["text"], "Let’s add this recipe:\nhttps://example.com/r")
        self.assertEqual(ready[0]["rowid"], 2)
        self.assertEqual(ready[0]["merged"], 2)

    def test_quick_followup_merges(self):
        msgs = [_msg(1, "what's for dinner"), _msg(2, "tonight I mean", at=2)]
        ready = server.coalesce_messages(msgs, now=T0 + 30)
        self.assertEqual([m["text"] for m in ready], ["what's for dinner\ntonight I mean"])

    def test_gap_over_quiet_window_stays_separate(self):
        msgs = [_msg(1, "what's for dinner"), _msg(2, "thanks", at=6)]
        ready = server.coalesce_messages(msgs, now=T0 + 30)
        self.assertEqual([m["rowid"] for m in ready], [1, 2])

    def test_different_senders_stay_separate(self):
        msgs = [_msg(1, "hi"), _msg(2, "hi", handle=PARTNER, at=1)]
        ready = server.coalesce_messages(msgs, now=T0 + 30)
        self.assertEqual(len(ready), 2)

    def test_photo_after_plain_text_stays_separate(self):
        msgs = [_msg(1, "what's for dinner"), _msg(2, "", at=1, images=1)]
        ready = server.coalesce_messages(msgs, now=T0 + 30)
        self.assertEqual(len(ready), 2)

    def test_photo_after_lead_in_merges(self):
        msgs = [_msg(1, "Here's a recipe:"), _msg(2, "", at=8, images=1)]
        ready = server.coalesce_messages(msgs, now=T0 + 30)
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["text"], "Here's a recipe:")
        self.assertEqual(len(ready[0]["attachments"]), 1)

    def test_tapbacks_never_merge(self):
        msgs = [_msg(1, "Loved “ok”"), _msg(2, "thanks", at=1)]
        ready = server.coalesce_messages(msgs, now=T0 + 30)
        self.assertEqual(len(ready), 2)

    def test_recent_message_is_held(self):
        ready = server.coalesce_messages([_msg(1, "what's for dinner")], now=T0 + 2)
        self.assertEqual(ready, [])

    def test_lead_in_held_up_to_lead_in_window(self):
        msgs = [_msg(1, "This is it:")]
        self.assertEqual(server.coalesce_messages(msgs, now=T0 + 10), [])
        ready = server.coalesce_messages(msgs, now=T0 + 16)
        self.assertEqual([m["text"] for m in ready], ["This is it:"])

    def test_held_message_holds_everything_after_it(self):
        # last_rowid is a single watermark: nothing past a held turn may be handled
        msgs = [_msg(1, "old", at=0), _msg(2, "This is it:", at=20), _msg(3, "hi", handle=PARTNER, at=21)]
        ready = server.coalesce_messages(msgs, now=T0 + 30)
        self.assertEqual([m["rowid"] for m in ready], [1])


class TestAppleDate(unittest.TestCase):

    def test_nanoseconds_and_seconds(self):
        self.assertAlmostEqual(server._apple_date_to_unix(800_000_000 * 10**9), 800_000_000 + 978307200)
        self.assertAlmostEqual(server._apple_date_to_unix(800_000_000), 800_000_000 + 978307200)
        self.assertEqual(server._apple_date_to_unix(None), 0.0)


if __name__ == "__main__":
    unittest.main()
