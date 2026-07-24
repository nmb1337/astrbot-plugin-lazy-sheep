from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rules import (
    extract_plain_text,
    find_keyword_action,
    find_number_or_link,
    find_segment_kinds,
    parse_target_from_text,
    stronger_action,
)


class Plain:
    def __init__(self, text: str):
        self.text = text


class At:
    qq = "12345678"


class Reply:
    message_str = "违规词"


class RulesTests(unittest.TestCase):
    def test_text_detection(self) -> None:
        self.assertEqual(find_number_or_link("加我 13800138000 或 https://example.com"), {"number", "link"})
        self.assertEqual(find_number_or_link("短链 b23.tv/abcd 和 192.168.0.1:8080"), {"link"})
        self.assertEqual(find_number_or_link("版本 1.2 不是链接"), set())
        self.assertEqual(parse_target_from_text("禁言 12345678 10"), "12345678")

    def test_segments_and_precedence(self) -> None:
        kinds = find_segment_kinds([{"type": "json", "data": {}}, {"type": "image", "data": {}}])
        self.assertEqual(kinds, {"card", "image"})
        self.assertEqual(stronger_action(["recall", "mute"]), "mute")
        self.assertEqual(find_keyword_action("请不要剧透", [("剧透", "recall")]), ("剧透", "recall"))

    def test_link_bearing_current_segments_are_detected(self) -> None:
        kinds = find_segment_kinds(
            [
                {"type": "share", "data": {"url": "https://example.com/share"}},
                {"type": "markdown", "data": {"content": "[链接](https://example.com/md)"}},
                {"type": "json", "data": {"data": '{"url":"example.cn/card"}'}},
                {"type": "xml", "data": {"data": '<msg url="https://example.org/xml" />'}},
                {"type": "image", "data": {"url": "https://cdn.example.com/image.jpg"}},
            ]
        )
        self.assertEqual(kinds, {"card", "image", "link"})

    def test_only_new_plain_text_is_scanned(self) -> None:
        self.assertEqual(extract_plain_text([At(), Reply(), Plain("正常内容")]), "正常内容")
        self.assertEqual(find_number_or_link(extract_plain_text([At(), Reply()])), set())


if __name__ == "__main__":
    unittest.main()
