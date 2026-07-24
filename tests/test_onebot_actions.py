from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _install_astrbot_stubs() -> None:
    """让本测试在未安装 AstrBot 的开发机上也能导入插件入口。"""
    if "astrbot.api" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event_module = types.ModuleType("astrbot.api.event")
    component_module = types.ModuleType("astrbot.api.message_components")
    star_module = types.ModuleType("astrbot.api.star")

    def decorator(*_args, **_kwargs):
        return lambda function: function

    class Filter:
        EventMessageType = SimpleNamespace(ALL="all")
        PlatformAdapterType = SimpleNamespace(AIOCQHTTP="aiocqhttp")
        event_message_type = staticmethod(decorator)
        platform_adapter_type = staticmethod(decorator)

    class AstrMessageEvent:
        pass

    class Plain:
        def __init__(self, text: str):
            self.text = text

    class Record:
        @staticmethod
        def fromFileSystem(path: str):
            return path

    class Star:
        def __init__(self, context):
            self.context = context

    event_module.AstrMessageEvent = AstrMessageEvent
    event_module.filter = Filter()
    component_module.Plain = Plain
    component_module.Record = Record
    star_module.Context = object
    star_module.Star = Star
    api.logger = logging.getLogger("lazy_sheep_test")
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event_module,
            "astrbot.api.message_components": component_module,
            "astrbot.api.star": star_module,
        }
    )


_install_astrbot_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_lazy_sheep.main import LazySheepPlugin  # noqa: E402
from astrbot_plugin_lazy_sheep.storage import LazySheepStore  # noqa: E402


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.roles = {"200": "admin", "300": "owner"}
        self.fail_actions: set[str] = set()
        self.fail_message_ids: set[str] = set()
        self.history_responses: list[object] = []
        self.group_info: dict = {"group_all_shut": 0}

    async def call_action(self, action: str, **payload):
        self.calls.append((action, payload))
        if action in self.fail_actions:
            return {"retcode": 100, "status": "failed", "wording": "模拟失败"}
        if action == "get_group_member_info":
            return {"role": self.roles.get(str(payload.get("user_id")), "member")}
        if action == "get_group_info":
            return {"retcode": 0, "status": "ok", "data": self.group_info}
        if action == "get_image":
            return {"retcode": 0, "status": "ok", "data": {"file": "resolved-qr.png"}}
        if action == "get_group_msg_history":
            if self.history_responses:
                return self.history_responses.pop(0)
            return {"retcode": 0, "status": "ok", "data": {"messages": []}}
        if action == "delete_msg" and str(payload.get("message_id")) in self.fail_message_ids:
            return {"retcode": 100, "status": "failed", "wording": "无法撤回"}
        return {"retcode": 0, "status": "ok", "data": {}}


class Image:
    async def convert_to_file_path(self):
        return "fixture.png"


class At:
    def __init__(self, qq: str = "12345678") -> None:
        self.qq = qq


class Reply:
    def __init__(self, message_id: str = "") -> None:
        self.id = message_id
        self.message_str = "违规词"


class Plain:
    def __init__(self, text: str):
        self.text = text


class FakeEvent:
    def __init__(self, message_str: str = "https://example.com", components: list[object] | None = None) -> None:
        self.bot = FakeBot()
        self.admin = False
        self.message_str = message_str
        self.components = components or []
        self.message_obj = SimpleNamespace(
            raw_message={
                "sender": {"role": "member"},
                "message": [{"type": "text", "data": {"text": self.message_str}}],
            },
            message_id="42",
        )
        self.stopped = False

    def get_group_id(self):
        return "100"

    def get_sender_id(self):
        return "200"

    def get_self_id(self):
        return "300"

    def get_messages(self):
        return self.components

    def get_platform_name(self):
        return "aiocqhttp"

    def is_admin(self):
        return self.admin

    def stop_event(self):
        self.stopped = True

    def should_call_llm(self, value: bool):
        self.call_llm = value

    def plain_result(self, text: str):
        return text


class OneBotActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.plugin = object.__new__(LazySheepPlugin)
        self.plugin.config = {"default_mute_minutes": 10}
        self.plugin.store = LazySheepStore(Path(self.tempdir.name) / "state.sqlite3")
        self.plugin._member_role_cache = {}
        self.event = FakeEvent()

    async def asyncTearDown(self) -> None:
        self.plugin.store.close()
        self.tempdir.cleanup()

    @staticmethod
    def _history_message(
        message_id: str,
        user_id: str,
        *,
        group_id: str = "100",
        message_seq: int | None = None,
        timestamp: int | None = None,
    ) -> dict:
        return {
            "message_id": message_id,
            "message_seq": message_seq if message_seq is not None else int(message_id),
            "time": timestamp if timestamp is not None else int(message_id),
            "group_id": group_id,
            "user_id": user_id,
            "sender": {"user_id": user_id},
            "message": [],
        }

    async def test_security_rule_calls_recall_and_mute_actions(self) -> None:
        self.plugin.store.set_security_rule("100", "link", "mute")
        self.assertTrue(await self.plugin._moderate_message(self.event))
        self.assertTrue(self.event.stopped)
        self.assertEqual([name for name, _ in self.event.bot.calls], ["delete_msg", "set_group_ban"])
        self.assertEqual(self.event.bot.calls[1][1]["duration"], 600)

    async def test_onebot_error_is_raised(self) -> None:
        async def failed_call(action: str, **payload):
            return {"retcode": 100, "status": "failed", "wording": "机器人不是群主"}

        self.event.bot.call_action = failed_call
        with self.assertRaisesRegex(RuntimeError, "机器人不是群主"):
            await self.plugin._onebot_action(self.event, "set_group_admin", group_id=100, user_id=200, enable=True)

    async def test_qrcode_component_triggers_mute_even_if_recall_fails(self) -> None:
        event = FakeEvent("", [Image()])
        event.bot.fail_actions.add("delete_msg")
        self.plugin.store.set_security_rule("100", "qrcode", "mute")

        async def qr_detected(_event):
            return True

        self.plugin._has_qrcode = qr_detected
        self.assertTrue(await self.plugin._moderate_message(event))
        self.assertIn("set_group_ban", [name for name, _ in event.bot.calls])

    async def test_qrcode_raw_image_segment_is_detected_and_recalled(self) -> None:
        event = FakeEvent("")
        event.message_obj.raw_message["message"] = [
            {"type": "image", "data": {"file": "raw-qr.png", "url": "https://example.invalid/qr.png"}},
        ]
        self.plugin.store.set_security_rule("100", "qrcode", "recall")

        detector = SimpleNamespace(
            detectAndDecode=lambda _image: ("", object(), None),
            detectAndDecodeMulti=lambda _image: (False, (), None, ()),
        )
        fake_cv2 = SimpleNamespace(
            QRCodeDetector=lambda: detector,
            IMREAD_COLOR=1,
            imdecode=lambda _data, _mode: object(),
        )
        fake_numpy = SimpleNamespace(fromfile=lambda _path, dtype: object(), uint8=object())
        with patch.dict(sys.modules, {"cv2": fake_cv2, "numpy": fake_numpy}):
            self.assertTrue(await self.plugin._moderate_message(event))

        self.assertEqual(event.bot.calls[-1], ("delete_msg", {"message_id": 42}))

    async def test_qrcode_image_file_id_is_resolved_before_detection(self) -> None:
        event = FakeEvent("")
        event.message_obj.raw_message["message"] = [
            {"type": "image", "data": {"file": "qr-file-id"}},
        ]
        self.plugin.store.set_security_rule("100", "qrcode", "recall")

        detector = SimpleNamespace(
            detectAndDecode=lambda _image: ("", object(), None),
            detectAndDecodeMulti=lambda _image: (False, (), None, ()),
        )
        fake_cv2 = SimpleNamespace(
            QRCodeDetector=lambda: detector,
            IMREAD_COLOR=1,
            imdecode=lambda _data, _mode: object(),
        )
        fake_numpy = SimpleNamespace(fromfile=lambda _path, dtype: object(), uint8=object())
        with patch.dict(sys.modules, {"cv2": fake_cv2, "numpy": fake_numpy}):
            self.assertTrue(await self.plugin._moderate_message(event))

        self.assertIn("get_image", [name for name, _ in event.bot.calls])
        self.assertEqual(event.bot.calls[-1], ("delete_msg", {"message_id": 42}))

    async def test_mentions_and_replies_do_not_trigger_text_rules(self) -> None:
        self.plugin.store.set_security_rule("100", "number", "recall")
        self.plugin.store.set_keyword_rule("100", "违规词", "recall")
        self.assertFalse(await self.plugin._moderate_message(FakeEvent("@昵称(12345678)", [At()])))
        self.assertFalse(await self.plugin._moderate_message(FakeEvent("[引用消息: 违规词]", [Reply()])))
        self.assertTrue(await self.plugin._moderate_message(FakeEvent("违规词", [Plain("违规词")])))

    async def test_link_security_moderates_share_segment_without_scanning_mentions(self) -> None:
        event = FakeEvent("")
        event.message_obj.raw_message["message"] = [
            {"type": "share", "data": {"url": "b23.tv/abcdef"}},
        ]
        self.plugin.store.set_security_rule("100", "link", "recall")

        self.assertTrue(await self.plugin._moderate_message(event))
        self.assertEqual(event.bot.calls[-1], ("delete_msg", {"message_id": 42}))

    async def test_admin_command_requires_group_owner(self) -> None:
        event = FakeEvent("上管 12345678", [Plain("上管 12345678")])
        event.bot.roles["200"] = "member"
        reply = await self.plugin._dispatch_command(event, event.message_str)
        self.assertIn("群主", reply.text)
        event.bot.roles["200"] = "owner"
        event.message_obj.raw_message["sender"]["role"] = "owner"
        self.plugin._member_role_cache.clear()
        reply = await self.plugin._dispatch_command(event, event.message_str)
        self.assertEqual(reply.text, "已设置群管理员。")

    async def test_group_admin_is_implicitly_whitelisted(self) -> None:
        event = FakeEvent("违规词", [Plain("违规词")])
        event.message_obj.raw_message["sender"]["role"] = "admin"
        self.plugin.store.set_keyword_rule("100", "违规词", "recall")
        self.assertFalse(await self.plugin._moderate_message(event))

    async def test_group_whitelist_commands(self) -> None:
        event = FakeEvent("群开机", [Plain("群开机")])
        event.message_obj.raw_message["sender"]["role"] = "admin"
        reply = await self.plugin._dispatch_command(event, event.message_str)
        self.assertIn("加入", reply.text)
        self.assertTrue(self.plugin.store.is_group_whitelisted("100"))

        event = FakeEvent("开启群白名单", [Plain("开启群白名单")])
        event.admin = True
        reply = await self.plugin._dispatch_command(event, event.message_str)
        self.assertIn("开启", reply.text)
        self.assertTrue(self.plugin.store.is_group_gate_enabled())

    async def test_unlisted_group_is_stopped_when_gate_is_on(self) -> None:
        self.plugin.store.set_group_gate_enabled(True)
        event = FakeEvent("普通聊天", [Plain("普通聊天")])
        results = [item async for item in self.plugin.on_onebot_event(event)]
        self.assertEqual(results, [])
        self.assertTrue(event.stopped)
        self.assertFalse(event.call_llm)

    async def test_group_gate_defaults_to_on_for_unregistered_groups(self) -> None:
        event = FakeEvent("菜单", [Plain("菜单")])
        results = [item async for item in self.plugin.on_onebot_event(event)]

        self.assertEqual(results, [])
        self.assertTrue(event.stopped)
        self.assertFalse(event.call_llm)

        boot_event = FakeEvent("群开机", [Plain("群开机")])
        boot_event.message_obj.raw_message["sender"]["role"] = "admin"
        results = [item async for item in self.plugin.on_onebot_event(boot_event)]

        self.assertTrue(results)
        self.assertTrue(self.plugin.store.is_group_whitelisted("100"))

    async def test_recall_reply_and_message_id_remain_single_message_actions(self) -> None:
        event = FakeEvent("撤回", [Reply("777")])
        event.message_obj.raw_message["sender"]["role"] = "admin"
        reply = await self.plugin._dispatch_command(event, event.message_str)
        self.assertEqual(reply.text, "已撤回目标消息。")
        self.assertEqual(event.bot.calls[-1], ("delete_msg", {"message_id": 777}))

        event = FakeEvent("撤回 888", [Plain("撤回 888")])
        event.message_obj.raw_message["sender"]["role"] = "admin"
        reply = await self.plugin._dispatch_command(event, event.message_str)
        self.assertEqual(reply.text, "已撤回目标消息。")
        self.assertEqual(event.bot.calls[-1], ("delete_msg", {"message_id": 888}))

    async def test_batch_recall_by_qq_uses_nested_history_and_filters_messages(self) -> None:
        event = FakeEvent("撤回 12345678 3", [Plain("撤回 12345678 3")])
        event.message_obj.raw_message["sender"]["role"] = "admin"
        event.bot.history_responses = [
            {
                "retcode": 0,
                "status": "ok",
                "data": {
                    "messages": [
                        self._history_message("42", "12345678", timestamp=9),
                        self._history_message("10", "12345678", timestamp=2),
                        self._history_message("11", "99999999", timestamp=4),
                        self._history_message("12", "12345678", group_id="101", timestamp=5),
                        self._history_message("13", "12345678", timestamp=7),
                    ]
                },
            }
        ]

        reply = await self.plugin._dispatch_command(event, event.message_str)

        self.assertIn("成功 2 条，失败 0 条；已找到 2/3 条", reply.text)
        delete_calls = [payload for name, payload in event.bot.calls if name == "delete_msg"]
        self.assertEqual(delete_calls, [{"message_id": 13}, {"message_id": 10}])

    async def test_batch_recall_by_at_supports_direct_history_response(self) -> None:
        event = FakeEvent("撤回 @成员 1", [At("12345678"), Plain("撤回 @成员 1")])
        event.message_obj.raw_message["sender"]["role"] = "admin"
        event.bot.history_responses = [{"messages": [self._history_message("15", "12345678")]}]

        reply = await self.plugin._dispatch_command(event, event.message_str)

        self.assertIn("成功 1 条，失败 0 条", reply.text)
        self.assertEqual(event.bot.calls[-1], ("delete_msg", {"message_id": 15}))

    async def test_batch_recall_paginates_and_reports_partial_delete_failures(self) -> None:
        event = FakeEvent("撤回 12345678 2", [Plain("撤回 12345678 2")])
        event.message_obj.raw_message["sender"]["role"] = "admin"
        first_page = [self._history_message(str(index), "99999999") for index in range(1, 201)]
        event.bot.history_responses = [
            {"messages": first_page},
            {"data": {"messages": [
                self._history_message("401", "12345678", timestamp=401),
                self._history_message("400", "12345678", timestamp=400),
            ]}},
        ]
        event.bot.fail_message_ids.add("400")

        reply = await self.plugin._dispatch_command(event, event.message_str)

        history_calls = [payload for name, payload in event.bot.calls if name == "get_group_msg_history"]
        self.assertEqual(len(history_calls), 2)
        self.assertIn("message_seq", history_calls[1])
        self.assertTrue(history_calls[1]["reverse_order"])
        self.assertIn("成功 1 条，失败 1 条", reply.text)

    async def test_batch_recall_rejects_invalid_count_and_unsupported_history(self) -> None:
        event = FakeEvent("撤回 12345678 101", [Plain("撤回 12345678 101")])
        event.message_obj.raw_message["sender"]["role"] = "admin"
        reply = await self.plugin._dispatch_command(event, event.message_str)
        self.assertEqual(reply.text, "批量撤回数量需为 1–100。")
        self.assertNotIn("get_group_msg_history", [name for name, _ in event.bot.calls])

        event = FakeEvent("撤回 12345678 1", [Plain("撤回 12345678 1")])
        event.message_obj.raw_message["sender"]["role"] = "admin"
        event.bot.fail_actions.add("get_group_msg_history")
        reply = await self.plugin._dispatch_command(event, event.message_str)
        self.assertIn("批量撤回不可用", reply.text)
        self.assertNotIn("delete_msg", [name for name, _ in event.bot.calls])

    async def test_whole_ban_requires_fresh_group_role(self) -> None:
        event = FakeEvent("说话", [Plain("说话")])
        event.message_obj.raw_message["sender"]["role"] = "admin"
        event.bot.roles["200"] = "member"

        reply = await self.plugin._dispatch_command(event, event.message_str)

        self.assertIn("只有群主、群管理员或 AstrBot 管理员", reply.text)
        self.assertIn("get_group_member_info", [name for name, _ in event.bot.calls])
        self.assertNotIn("set_group_whole_ban", [name for name, _ in event.bot.calls])

    async def test_whole_ban_denies_when_role_lookup_fails(self) -> None:
        event = FakeEvent("安静", [Plain("安静")])
        event.message_obj.raw_message["sender"]["role"] = "owner"
        event.bot.fail_actions.add("get_group_member_info")

        reply = await self.plugin._dispatch_command(event, event.message_str)

        self.assertIn("只有群主、群管理员或 AstrBot 管理员", reply.text)
        self.assertNotIn("set_group_whole_ban", [name for name, _ in event.bot.calls])

    async def test_whole_ban_allows_owner_admin_and_astrbot_admin(self) -> None:
        for role, astrbot_admin in (("owner", False), ("admin", False), ("member", True)):
            event = FakeEvent("安静", [Plain("安静")])
            event.message_obj.raw_message["sender"]["role"] = "member"
            event.bot.roles["200"] = role
            event.admin = astrbot_admin

            reply = await self.plugin._dispatch_command(event, event.message_str)

            self.assertEqual(reply.text, "已开启全员禁言。")
            self.assertIn("set_group_whole_ban", [name for name, _ in event.bot.calls])

    async def test_speak_does_not_call_onebot_when_whole_ban_is_off(self) -> None:
        event = FakeEvent("说话", [Plain("说话")])
        event.bot.roles["200"] = "admin"
        event.bot.group_info = {"group_all_shut": 0}

        reply = await self.plugin._dispatch_command(event, event.message_str)

        self.assertEqual(reply.text, "当前未开启全员禁言，无需解除。")
        self.assertIn("get_group_info", [name for name, _ in event.bot.calls])
        self.assertNotIn("set_group_whole_ban", [name for name, _ in event.bot.calls])

    async def test_speak_only_disables_an_active_whole_ban(self) -> None:
        event = FakeEvent("说话", [Plain("说话")])
        event.bot.roles["200"] = "admin"
        event.bot.group_info = {"group_all_shut": -1}

        reply = await self.plugin._dispatch_command(event, event.message_str)

        self.assertEqual(reply.text, "已解除全员禁言。")
        self.assertEqual(event.bot.calls[-1], ("set_group_whole_ban", {"group_id": 100, "enable": False}))


if __name__ == "__main__":
    unittest.main()
