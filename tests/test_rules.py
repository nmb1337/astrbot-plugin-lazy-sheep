from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rules import find_keyword_action, find_number_or_link, find_segment_kinds, parse_target_from_text, stronger_action


class RulesTests(unittest.TestCase):
    def test_text_detection(self) -> None:
        self.assertEqual(find_number_or_link("加我 13800138000 或 https://example.com"), {"number", "link"})
        self.assertEqual(parse_target_from_text("禁言 12345678 10"), "12345678")

    def test_segments_and_precedence(self) -> None:
        kinds = find_segment_kinds([{"type": "json", "data": {}}, {"type": "image", "data": {}}])
        self.assertEqual(kinds, {"card", "image"})
        self.assertEqual(stronger_action(["recall", "mute"]), "mute")
        self.assertEqual(find_keyword_action("请不要剧透", [("剧透", "recall")]), ("剧透", "recall"))


if __name__ == "__main__":
    unittest.main()
