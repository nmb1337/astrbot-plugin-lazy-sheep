from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage import LazySheepStore


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = LazySheepStore(Path(self.tempdir.name) / "state.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def test_lists_rules_and_statistics(self) -> None:
        self.store.set_list_member("g", "u", "white", True)
        self.assertTrue(self.store.is_list_member("g", "u", "white"))
        self.store.set_security_rule("g", "link", "mute")
        self.assertEqual(self.store.get_security_rules("g"), {"link": "mute"})
        self.store.set_keyword_rule("g", "剧透", "kick")
        self.assertEqual(self.store.get_keyword_rules("g"), [("剧透", "kick")])

        today = self.store.today()
        self.store.record_message("g", "u", today)
        self.store.record_message("g", "u", today)
        self.store.record_message("g", "v", today - timedelta(days=1))
        self.assertEqual(self.store.message_total("g", "u"), 2)
        self.assertEqual(self.store.message_rank("g", today - timedelta(days=2), today)[0], ("u", 2))
        self.assertEqual(len(self.store.message_trend("g", None)), 30)

    def test_checkins_and_invites_are_deduplicated(self) -> None:
        self.assertEqual(self.store.check_in("g", "u"), (True, 1))
        self.assertEqual(self.store.check_in("g", "u"), (False, 1))
        self.assertTrue(self.store.record_invite("g", "new", "u"))
        self.assertFalse(self.store.record_invite("g", "new", "u"))
        self.assertEqual(self.store.invite_total("g", "u"), 1)
        self.assertEqual(self.store.invite_rank("g"), [("u", 1)])

    def test_group_whitelist_gate(self) -> None:
        self.assertTrue(self.store.is_group_gate_enabled())
        self.store.set_group_gate_enabled(False)
        self.assertFalse(self.store.is_group_gate_enabled())
        self.store.set_group_gate_enabled(True)
        self.assertTrue(self.store.is_group_gate_enabled())
        self.assertFalse(self.store.is_group_whitelisted("100"))
        self.store.set_group_whitelisted("100", True, "admin")
        self.assertTrue(self.store.is_group_whitelisted("100"))
        self.store.set_group_whitelisted("100", False, "admin")
        self.assertFalse(self.store.is_group_whitelisted("100"))


if __name__ == "__main__":
    unittest.main()
