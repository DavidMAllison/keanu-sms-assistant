"""
Unit tests for server._RedactHandles — keanu.log must never contain raw handles.

Family handles (from handle_to_person) are logged as names; any other phone
number or email is masked. All handles here are synthetic.
"""
import io
import logging
import unittest

import server

_CONFIG = {"security": {"handle_to_person": {"+10000000001": "Admin", "kid@example.com": "Kid"}}}


class TestLogRedaction(unittest.TestCase):
    def setUp(self):
        self.buf = io.StringIO()
        self.handler = logging.StreamHandler(self.buf)
        self.handler.addFilter(server._RedactHandles(_CONFIG["security"]["handle_to_person"]))
        self.log = logging.getLogger("test_log_redaction")
        self.log.addHandler(self.handler)
        self.log.setLevel(logging.INFO)
        self.log.propagate = False

    def tearDown(self):
        self.log.removeHandler(self.handler)

    def _logged(self, msg, *args):
        self.log.info(msg, *args)
        return self.buf.getvalue().strip().splitlines()[-1]

    def test_family_phone_becomes_name(self):
        self.assertEqual(self._logged("Message from %s: hi", "+10000000001"), "Message from Admin: hi")

    def test_family_email_becomes_name(self):
        self.assertEqual(self._logged("Sent reply to kid@example.com"), "Sent reply to Kid")

    def test_unknown_phone_is_masked(self):
        line = self._logged("Rejected from unauthorized handle: +15559876543")
        self.assertEqual(line, "Rejected from unauthorized handle: +1555•••••••")

    def test_unknown_email_is_masked(self):
        self.assertEqual(self._logged("to stranger@example.org"), "to •••@example.org")

    def test_handle_list_redacted(self):
        line = self._logged("group to %s", ["+10000000001", "+15559876543"])
        self.assertEqual(line, "group to ['Admin', '+1555•••••••']")


if __name__ == "__main__":
    unittest.main()
