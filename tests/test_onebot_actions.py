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

    async def call_action(self, action: str, **payload):
        self.calls.append((action, payload))
        return {"role": "admin"}


class FakeEvent:
    def __init__(self) -> None:
        self.bot = FakeBot()
        self.message_str = "https://example.com"
        self.message_obj = SimpleNamespace(
            raw_message={"message": [{"type": "text", "data": {"text": self.message_str}}]},
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
        return []

    def stop_event(self):
        self.stopped = True


class OneBotActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.plugin = object.__new__(LazySheepPlugin)
        self.plugin.config = {"default_mute_minutes": 10}
        self.plugin.store = LazySheepStore(Path(self.tempdir.name) / "state.sqlite3")
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


if __name__ == "__main__":
    unittest.main()
