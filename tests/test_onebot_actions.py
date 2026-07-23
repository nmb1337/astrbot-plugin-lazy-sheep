from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


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

    async def call_action(self, action: str, **payload):
        self.calls.append((action, payload))
        if action == "get_group_member_info":
            return {"role": self.roles.get(str(payload.get("user_id")), "member")}
        if action in self.fail_actions:
            return {"retcode": 100, "status": "failed", "wording": "模拟失败"}
        return {"retcode": 0, "status": "ok", "data": {}}


class Image:
    async def convert_to_file_path(self):
        return "fixture.png"


class At:
    qq = "12345678"


class Reply:
    message_str = "违规词"


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

    async def test_mentions_and_replies_do_not_trigger_text_rules(self) -> None:
        self.plugin.store.set_security_rule("100", "number", "recall")
        self.plugin.store.set_keyword_rule("100", "违规词", "recall")
        self.assertFalse(await self.plugin._moderate_message(FakeEvent("@昵称(12345678)", [At()])))
        self.assertFalse(await self.plugin._moderate_message(FakeEvent("[引用消息: 违规词]", [Reply()])))
        self.assertTrue(await self.plugin._moderate_message(FakeEvent("违规词", [Plain("违规词")])))

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


if __name__ == "__main__":
    unittest.main()
