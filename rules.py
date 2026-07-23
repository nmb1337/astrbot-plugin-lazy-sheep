"""不依赖 AstrBot 的文本与消息段规则。"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping


SECURITY_KINDS = ("card", "number", "link", "image", "qrcode")
ACTIONS = ("recall", "mute")

# 中国手机号码与常见 QQ 号长度。边界避免把更长的数字的一部分误判为号码。
NUMBER_RE = re.compile(r"(?<!\d)(?:1[3-9]\d{9}|[1-9]\d{4,11})(?!\d)")
LINK_RE = re.compile(r"(?i)(?:https?://|www\.)[^\s<>]+")
QQ_RE = re.compile(r"(?<!\d)([1-9]\d{4,11})(?!\d)")


def normalized_text(text: str) -> str:
    """归一化用于关键词比较；保留中文和所有可见字符。"""
    return " ".join(text.casefold().split())


def find_number_or_link(text: str) -> set[str]:
    """返回纯文本中命中的“号码”和“链接”类别。"""
    kinds: set[str] = set()
    if NUMBER_RE.search(text):
        kinds.add("number")
    if LINK_RE.search(text):
        kinds.add("link")
    return kinds


def find_segment_kinds(segments: Iterable[object]) -> set[str]:
    """按 OneBot v11 原始消息段识别卡片和图片。"""
    kinds: set[str] = set()
    for segment in segments:
        if isinstance(segment, Mapping):
            segment_type = str(segment.get("type", "")).lower()
        else:
            segment_type = type(segment).__name__.lower()
            if segment_type in {"plain", "text"}:
                continue
        if segment_type in {"json", "xml"}:
            kinds.add("card")
        if segment_type == "image":
            kinds.add("image")
    return kinds


def extract_plain_text(components: Iterable[object], raw_segments: Iterable[object] = ()) -> str:
    """只提取当前消息的纯文本，忽略 @、回复、图片和其他消息段。"""
    parts: list[str] = []
    component_list = list(components)
    source = component_list if component_list else list(raw_segments)
    for component in source:
        if isinstance(component, Mapping):
            segment_type = str(component.get("type", "")).lower()
            if segment_type in {"text", "plain"}:
                data = component.get("data", {})
                if isinstance(data, Mapping):
                    parts.append(str(data.get("text", "")))
            continue
        component_type = type(component).__name__.lower()
        if component_type in {"plain", "text"}:
            parts.append(str(getattr(component, "text", "")))
    return "".join(parts)


def find_keyword_action(text: str, rules: Iterable[tuple[str, str]]) -> tuple[str, str] | None:
    """返回第一个命中的 (关键词, 动作)，动作优先级由调用方保证。"""
    source = normalized_text(text)
    for keyword, action in rules:
        normalized = normalized_text(keyword)
        if normalized and normalized in source:
            return keyword, action
    return None


def parse_target_from_text(text: str) -> str | None:
    """从命令文本解析独立 QQ 号；@ 目标由消息组件解析。"""
    match = QQ_RE.search(text)
    return match.group(1) if match else None


def stronger_action(actions: Iterable[str]) -> str | None:
    """撤回后禁言比单纯撤回更严格。"""
    actions = set(actions)
    if "mute" in actions:
        return "mute"
    if "recall" in actions:
        return "recall"
    return None
