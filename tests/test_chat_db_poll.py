"""
server.poll_new_messages / _decode_attributed_body: messages whose text lives
only in attributedBody (message.text NULL, common since Ventura) are decoded
rather than dropped, and tapbacks/reactions are excluded via
associated_message_type rather than relying on English text prefixes.
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import server

DAVID = "+15550000001"
NS = 1_000_000_000


def _blob(text: str) -> bytes:
    """Minimal typedstream shaped like a real attributedBody."""
    body = text.encode("utf-8")
    n = len(body)
    if n < 0x80:
        length = bytes([n])
    elif n < 0x10000:
        length = b"\x81" + n.to_bytes(2, "little")
    else:
        length = b"\x82" + n.to_bytes(4, "little")
    return (b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString"
            b"\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+"
            + length + body + b"\x86\x84\x02iI\x01")


class TestDecodeAttributedBody(unittest.TestCase):
    def test_short_string(self):
        self.assertEqual(server._decode_attributed_body(_blob("what's for dinner")), "what's for dinner")

    def test_long_string_two_byte_length(self):
        text = "x" * 300
        self.assertEqual(server._decode_attributed_body(_blob(text)), text)

    def test_multibyte(self):
        self.assertEqual(server._decode_attributed_body(_blob("tacos 🌮 tonight?")), "tacos 🌮 tonight?")

    def test_attachment_placeholder_only(self):
        self.assertEqual(server._decode_attributed_body(_blob("￼")), "")

    def test_garbage_and_none(self):
        self.assertEqual(server._decode_attributed_body(None), "")
        self.assertEqual(server._decode_attributed_body(b""), "")
        self.assertEqual(server._decode_attributed_body(b"no marker here"), "")
        self.assertEqual(server._decode_attributed_body(b"NSString"), "")


class TestPollNewMessages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "chat.db"
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, attributedBody BLOB,
                date INTEGER, handle_id INTEGER, is_from_me INTEGER, associated_message_type INTEGER);
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT);
            CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
            CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, filename TEXT, mime_type TEXT);
            CREATE TABLE message_attachment_join (ROWID INTEGER PRIMARY KEY, message_id INTEGER, attachment_id INTEGER);
        """)
        conn.execute("INSERT INTO handle VALUES (1, ?)", (DAVID,))
        conn.execute("INSERT INTO chat VALUES (1, ?)", (DAVID,))
        rows = [
            (1, "plain text", None, 0),
            (2, None, _blob("only in attributedBody"), 0),
            (3, "Loved “plain text”", None, 2000),
            (4, "Reacted 🌮 to “plain text”", None, 2006),
            (5, "￼", _blob("￼"), 0),
        ]
        for rowid, text, body, assoc in rows:
            conn.execute("INSERT INTO message VALUES (?, ?, ?, ?, 1, 0, ?)",
                         (rowid, text, body, rowid * NS, assoc))
            conn.execute("INSERT INTO chat_message_join VALUES (1, ?)", (rowid,))
        conn.execute("INSERT INTO attachment VALUES (1, '~/img.heic', 'image/heic')")
        conn.execute("INSERT INTO message_attachment_join VALUES (1, 5, 1)")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _poll(self):
        with mock.patch.object(server, "CHAT_DB", self.db):
            return {m["rowid"]: m for m in server.poll_new_messages(0)}

    def test_plain_text_unchanged(self):
        msg = self._poll()[1]
        self.assertEqual(msg["text"], "plain text")
        self.assertNotIn("decoded", msg)

    def test_null_text_decoded_from_attributed_body(self):
        msg = self._poll()[2]
        self.assertEqual(msg["text"], "only in attributedBody")
        self.assertTrue(msg["decoded"])

    def test_reactions_excluded(self):
        msgs = self._poll()
        self.assertNotIn(3, msgs)
        self.assertNotIn(4, msgs)

    def test_image_message_still_returned(self):
        msg = self._poll()[5]
        self.assertEqual(len(msg["attachments"]), 1)


class TestIsTapbackEmojiReaction(unittest.TestCase):
    def test_reacted_prefix(self):
        self.assertTrue(server.is_tapback("Reacted 😂 to “lol”"))


if __name__ == "__main__":
    unittest.main()
