"""
Unit tests for the outbox spool (tools.queue_outbox / tools.drain_outbox).

Phase 2.3: create-exclusive spool files replaced the .outbox.json
read-modify-write, which could drop messages under concurrent writers
(cross-process, cross-user — _outbox_lock never covered MenuBuilder).

Mocks:
  - tools.OUTBOX_DIR    → temp dir
  - send_imessage / send_imessage_group → recorded, never sent
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import tools


class TestOutboxSpool(unittest.TestCase):

    def setUp(self):
        self.outbox_dir = Path(tempfile.mkdtemp())

        self._p_dir = patch("tools.OUTBOX_DIR", self.outbox_dir)
        self._p_dir.start()

        self.sent = []  # (kind, target, text) in send order
        self._p_send = patch(
            "tools.send_imessage",
            side_effect=lambda h, t: self.sent.append(("single", h, t)))
        self._p_send_group = patch(
            "tools.send_imessage_group",
            side_effect=lambda hs, t: self.sent.append(("group", tuple(hs), t)))
        self._p_send.start()
        self._p_send_group.start()

    def tearDown(self):
        for p in (self._p_dir, self._p_send, self._p_send_group):
            p.stop()
        shutil.rmtree(self.outbox_dir, ignore_errors=True)

    def test_queue_outbox_writes_one_file_per_entry(self):
        tools.queue_outbox({"handle": "+10000000001", "text": "one"})
        tools.queue_outbox({"handle": "+10000000001", "text": "two"})
        files = sorted(self.outbox_dir.glob("*.json"))
        self.assertEqual(len(files), 2)
        self.assertEqual(json.loads(files[0].read_text())["text"], "one")
        self.assertEqual(json.loads(files[1].read_text())["text"], "two")

    def test_drain_sends_spool_in_name_order_and_empties_dir(self):
        tools.queue_outbox({"handle": "+10000000001", "text": "first"})
        tools.queue_outbox({"handle": "+10000000002", "text": "second"})
        tools.drain_outbox()
        self.assertEqual([s[2] for s in self.sent], ["first", "second"])
        self.assertEqual(list(self.outbox_dir.glob("*.json")), [])

    def test_group_entry_routed_to_group_send(self):
        tools.queue_outbox({"handles": ["+10000000001", "+10000000002"], "text": "hi both"})
        tools.drain_outbox()
        self.assertEqual(self.sent, [("group", ("+10000000001", "+10000000002"), "hi both")])

    def test_bad_file_quarantined_and_drain_continues(self):
        (self.outbox_dir / "0000_garbage.json").write_text("{not json")
        (self.outbox_dir / "0001_noschema.json").write_text(json.dumps({"text": "no handle"}))
        tools.queue_outbox({"handle": "+10000000001", "text": "good"})
        tools.drain_outbox()
        # Good entry still sent, both bad files quarantined, spool clean
        self.assertEqual([s[2] for s in self.sent], ["good"])
        self.assertEqual(list(self.outbox_dir.glob("*.json")), [])
        bad = sorted(p.name for p in (self.outbox_dir / ".bad").glob("*.json"))
        self.assertEqual(bad, ["0000_garbage.json", "0001_noschema.json"])

    def test_drain_with_no_spool_dir_is_a_noop(self):
        shutil.rmtree(self.outbox_dir)
        tools.drain_outbox()  # must not raise or recreate the dir
        self.assertEqual(self.sent, [])
        self.assertFalse(self.outbox_dir.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
