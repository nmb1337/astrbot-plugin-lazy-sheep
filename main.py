"""懒羊羊大王：AstrBot + QQ OneBot v11 群管插件入口。"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Context, Star

from .charts import render_trend_chart
from .games import GameManager
from .rules import (
    SECURITY_KINDS,
    find_keyword_action,
    find_number_or_link,
    find_segment_kinds,
    parse_target_from_text,
    stronger_action,
)
from .storage import LazySheepStore


MENU_IMAGES = {
    "菜单": "menu.jpg",
    "群管": "group_admin.jpg",
    "保安": "security.jpg",
    "统计": "statistics.jpg",
    "村规": "rules.jpg",
    "乐园": "playground.jpg",
}
SECURITY_LABELS = {"卡片": "card", "号码": "number", "链接": "link", "图片": "image", "二维码": "qrcode"}
KEYWORD_LABELS = {"撤回词": "recall", "禁言词": "mute", "踢人词": "kick"}
ACTION_LABELS = {"撤回": "recall", "禁言": "mute"}


@dataclass
class CommandReply:
    text: str | None = None
    image_path: str | None = None


class LazySheepPlugin(Star):
    """面向 QQ OneBot v11 的群管理、统计和互动小游戏插件。"""

    def __init__(self, context: Context, config: Any | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self.assets = Path(__file__).resolve().parent / "assets"
        self.store = LazySheepStore(self._database_path(), self._config_text("timezone", "Asia/Shanghai"))
        self.games = GameManager()
        self.race_tasks: dict[str, asyncio.Task] = {}
        self.vote_tasks: dict[str, asyncio.Task] = {}
        self.voice_semaphore = asyncio.Semaphore(max(1, self._config_int("max_concurrent_voice", 2)))

    def _database_path(self) -> Path:
        """优先把数据放入 AstrBot data/ 目录，开发环境使用同级数据目录。"""
        plugin_root = Path(__file__).resolve().parent
        for parent in (plugin_root, *plugin_root.parents):
            if parent.name == "data":
                return parent / "lazy_sheep" / "lazy_sheep.sqlite3"
        return plugin_root.parent / "lazy_sheep_data" / "lazy_sheep.sqlite3"

    def _config_text(self, key: str, default: str) -> str:
        value = self.config.get(key, default) if hasattr(self.config, "get") else default
        return str(value or default)

    def _config_int(self, key: str, default: int) -> int:
        try:
            return max(1, int(self.config.get(key, default)))
        except (TypeError, ValueError, AttributeError):
            return default

    @staticmethod
    def _raw(event: AstrMessageEvent) -> Mapping[str, Any]:
        raw = getattr(event.message_obj, "raw_message", {})
        return raw if isinstance(raw, Mapping) else {}

    @staticmethod
    def _group_id(event: AstrMessageEvent) -> str:
        return str(event.get_group_id() or "")

    @staticmethod
    def _is_group(event: AstrMessageEvent) -> bool:
        return bool(event.get_group_id())

    @staticmethod
    def _component_name(component: object) -> str:
        return type(component).__name__.lower()

    @staticmethod
    def _number(value: str) -> str | int:
        return int(value) if value.isdigit() else value

    async def _onebot_action(self, event: AstrMessageEvent, action: str, **payload: Any) -> Any:
        """兼容当前 CQHttp 与旧版 api.call_action 两种调用方式。"""
        bot = getattr(event, "bot", None)
        if not bot:
            raise RuntimeError("当前事件不是 QQ OneBot v11 事件")
        caller = getattr(bot, "call_action", None)
        if not caller:
            caller = getattr(getattr(bot, "api", None), "call_action", None)
        if not caller:
            raise RuntimeError("OneBot 客户端不支持 call_action")
        try:
            return await caller(action=action, **payload)
        except TypeError:
            return await caller(action, **payload)

    async def _send_group(self, event: AstrMessageEvent, text: str) -> None:
        await self._onebot_action(
            event,
            "send_group_msg",
            group_id=self._number(self._group_id(event)),
            message=text,
        )

    async def _send_private(self, event: AstrMessageEvent, user_id: str, text: str) -> None:
        await self._onebot_action(event, "send_private_msg", user_id=self._number(user_id), message=text)

    async def _is_manager(self, event: AstrMessageEvent) -> bool:
        """AstrBot 管理员或当前 QQ 群主/管理员才可进行管理操作。"""
        if event.is_admin():
            return True
        group_id = self._group_id(event)
        if not group_id:
            return False
        try:
            info = await self._onebot_action(
                event,
                "get_group_member_info",
                group_id=self._number(group_id),
                user_id=self._number(event.get_sender_id()),
                no_cache=False,
            )
        except Exception as exc:
            logger.warning("无法读取群成员权限: %s", exc)
            return False
        return str((info or {}).get("role", "member")) in {"owner", "admin"}

    async def _require_manager(self, event: AstrMessageEvent) -> str | None:
        if not self._is_group(event):
            return "此功能仅可在 QQ 群聊中使用。"
        if event.get_platform_name() != "aiocqhttp":
            return "此功能仅支持 QQ OneBot v11。"
        if not await self._is_manager(event):
            return "只有群主、群管理员或 AstrBot 管理员可以执行此操作。"
        return None

    def _extract_target(self, event: AstrMessageEvent, text: str) -> str | None:
        for component in event.get_messages():
            if self._component_name(component) == "at":
                target = str(getattr(component, "qq", ""))
                if target.isdigit():
                    return target
        return parse_target_from_text(text)

    def _extract_reply_message_id(self, event: AstrMessageEvent) -> str | None:
        for component in event.get_messages():
            if self._component_name(component) == "reply":
                value = str(getattr(component, "id", ""))
                if value:
                    return value
        return None

    def _extract_duration(self, event: AstrMessageEvent, text: str, target: str) -> int:
        # @ 目标时可取结尾短数字；纯 QQ 号目标时只读取 QQ 号后的独立数字。
        after_command = text.replace("禁言", "", 1).strip()
        for component in event.get_messages():
            if self._component_name(component) == "at":
                match = re.search(r"(?:\s|^)(\d{1,4})\s*$", after_command)
                if match:
                    return max(1, min(43200, int(match.group(1))))
                return self._config_int("default_mute_minutes", 10)
        parts = after_command.split()
        if len(parts) >= 2 and parts[-1].isdigit() and parts[-1] != target:
            return max(1, min(43200, int(parts[-1])))
        return self._config_int("default_mute_minutes", 10)

    @staticmethod
    def _format_rank(rows: list[tuple[str, int]], title: str, unit: str) -> str:
        if not rows:
            return f"{title}\n暂无数据。"
        lines = [title]
        lines.extend(f"{index}. {user_id} — {value} {unit}" for index, (user_id, value) in enumerate(rows, 1))
        return "\n".join(lines)

    async def _handle_notice(self, event: AstrMessageEvent) -> None:
        raw = self._raw(event)
        if raw.get("post_type") != "notice" or raw.get("notice_type") != "group_increase":
            return
        group_id = str(raw.get("group_id", ""))
        user_id = str(raw.get("user_id", ""))
        if not group_id or not user_id:
            return
        if self.store.is_list_member(group_id, user_id, "black"):
            try:
                await self._onebot_action(
                    event,
                    "set_group_kick",
                    group_id=self._number(group_id),
                    user_id=self._number(user_id),
                    reject_add_request=True,
                )
            except Exception as exc:
                logger.warning("自动踢出黑名单成员失败: %s", exc)
        if raw.get("sub_type") == "invite":
            inviter_id = str(raw.get("operator_id", ""))
            self.store.record_invite(group_id, user_id, inviter_id)

    async def _has_qrcode(self, event: AstrMessageEvent) -> bool:
        """仅在二维码规则开启时调用，避免普通图片消息带来额外耗时。"""
        try:
            import cv2  # 延迟导入，插件无法安装依赖时仍可加载其他功能。
        except ImportError:
            logger.warning("未安装 OpenCV，二维码保安无法工作")
            return False
        detector = cv2.QRCodeDetector()
        for component in event.get_messages():
            if self._component_name(component) != "image":
                continue
            try:
                local_path = await component.convert_to_file_path()
                decoded, _, _ = detector.detectAndDecode(local_path)
                if decoded:
                    return True
            except Exception as exc:
                logger.debug("二维码检测失败: %s", exc)
        return False

    async def _moderate_message(self, event: AstrMessageEvent) -> bool:
        """执行关键词/保安规则；返回 True 表示消息已经被拦截。"""
        group_id = self._group_id(event)
        user_id = event.get_sender_id()
        if self.store.is_list_member(group_id, user_id, "white"):
            return False

        text = event.message_str or ""
        keyword = find_keyword_action(text, self.store.get_keyword_rules(group_id))
        if keyword and keyword[1] == "kick":
            action = "kick"
            reason = f"踢人词：{keyword[0]}"
        else:
            raw_segments = self._raw(event).get("message", [])
            kinds = find_number_or_link(text) | find_segment_kinds(raw_segments if isinstance(raw_segments, list) else [])
            enabled = self.store.get_security_rules(group_id)
            if "qrcode" in enabled and "image" in kinds and await self._has_qrcode(event):
                kinds.add("qrcode")
            actions: list[str] = [enabled[kind] for kind in kinds if kind in enabled]
            if keyword:
                actions.append(keyword[1])
            action = stronger_action(actions)
            reason = "、".join(sorted(kinds)) if action else ""
        if not action:
            return False

        message_id = str(getattr(event.message_obj, "message_id", ""))
        try:
            if message_id:
                await self._onebot_action(event, "delete_msg", message_id=self._number(message_id))
            if action == "mute":
                await self._onebot_action(
                    event,
                    "set_group_ban",
                    group_id=self._number(group_id),
                    user_id=self._number(user_id),
                    duration=self._config_int("default_mute_minutes", 10) * 60,
                )
            elif action == "kick":
                await self._onebot_action(
                    event,
                    "set_group_kick",
                    group_id=self._number(group_id),
                    user_id=self._number(user_id),
                    reject_add_request=False,
                )
            logger.info("群 %s 拦截成员 %s 的消息，原因：%s", group_id, user_id, reason)
        except Exception as exc:
            logger.warning("执行保安动作失败: %s", exc)
        event.stop_event()
        return True

    async def _finish_race_after(self, event: AstrMessageEvent, group_id: str) -> None:
        try:
            await asyncio.sleep(self._config_int("race_seconds", 60))
            result = self.games.finish_race(group_id)
            if not result:
                return
            winner, winners = result
            text = f"🏁 比赛结束！{winner} 号赛车夺冠。"
            text += "\n猜中的成员：" + ("、".join(winners) if winners else "本轮无人猜中。")
            await self._send_group(event, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("赛车结算失败: %s", exc)
        finally:
            if self.race_tasks.get(group_id) is asyncio.current_task():
                self.race_tasks.pop(group_id, None)

    async def _finish_vote_after(self, event: AstrMessageEvent, group_id: str) -> None:
        try:
            await asyncio.sleep(self._config_int("vote_seconds", 90))
            text = self.games.finish_vote(group_id)
            if text:
                await self._send_group(event, "⏰ 投票时间到。\n" + text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("卧底投票结算失败: %s", exc)
        finally:
            if self.vote_tasks.get(group_id) is asyncio.current_task():
                self.vote_tasks.pop(group_id, None)

    async def _start_undercover(self, event: AstrMessageEvent) -> CommandReply:
        group_id = self._group_id(event)
        game, error = self.games.prepare_undercover_start(group_id, event.get_sender_id())
        if error or not game:
            return CommandReply(text=error)
        try:
            for user_id in game.players:
                word = game.undercover_word if user_id == game.undercover_id else game.civilian_word
                await self._send_private(event, user_id, f"【谁是卧底】你的词语是：{word}\n请勿在群内直接说出词语。")
        except Exception as exc:
            self.games.cancel_undercover(group_id)
            return CommandReply(text=f"无法向所有玩家私聊发词，本局已取消。请先让玩家添加机器人好友。({exc})")
        self.games.activate_undercover(group_id)
        return CommandReply(text="词语已私聊发出！请所有存活玩家依次描述词语；发起人发送“开始投票”进入投票。")

    async def _handle_voice(self, event: AstrMessageEvent) -> CommandReply:
        group_id = self._group_id(event)
        if self.store.get_group_setting(group_id, "voice_mode", "0") != "1":
            return CommandReply()
        record = next((item for item in event.get_messages() if self._component_name(item) == "record"), None)
        if not record:
            return CommandReply()
        if self.voice_semaphore.locked():
            return CommandReply(text="语音互动正在忙，请稍后再试。")
        event.stop_event()
        async with self.voice_semaphore:
            try:
                audio_path = await record.convert_to_file_path()
                if os.path.getsize(audio_path) > self._config_int("max_voice_bytes", 8 * 1024 * 1024):
                    return CommandReply(text="这条语音文件过大，请缩短后再试。")
                stt = self.context.get_using_stt_provider(umo=event.unified_msg_origin)
                tts = self.context.get_using_tts_provider(umo=event.unified_msg_origin)
                llm = self.context.get_using_provider(umo=event.unified_msg_origin)
                if not stt or not tts or not llm:
                    return CommandReply(text="语音互动需要同时配置 STT、聊天模型和 TTS 服务。")
                transcript = (await stt.get_text(audio_path)).strip()
                if not transcript:
                    return CommandReply(text="没有识别到有效语音内容。")
                response = await llm.text_chat(
                    prompt=transcript,
                    system_prompt=self._config_text("voice_system_prompt", "你是友好的群聊语音助手。"),
                )
                answer = (response.completion_text or "").strip()
                if not answer:
                    return CommandReply(text="语音助手暂时没有生成回复。")
                output_audio = await tts.get_audio(answer)
                return CommandReply(text=f"🎙️ 你说：{transcript}\n🤖 回复：{answer}", image_path=f"record:{output_audio}")
            except Exception as exc:
                logger.warning("语音互动失败: %s", exc)
                return CommandReply(text="语音互动处理失败，请检查 STT、LLM 和 TTS 配置。")

    async def _dispatch_command(self, event: AstrMessageEvent, text: str) -> CommandReply | None:
        """返回 None 表示普通聊天，不应截断 AstrBot 的默认消息流程。"""
        if text in MENU_IMAGES:
            return CommandReply(image_path=str(self.assets / MENU_IMAGES[text]))
        if not self._is_group(event):
            return None
        group_id = self._group_id(event)
        user_id = event.get_sender_id()

        # 统计与签到（所有群成员可使用）。
        if text == "我的发言":
            return CommandReply(text=f"你在本群累计发言 {self.store.message_total(group_id, user_id)} 条。")
        if text in {"发言日榜", "发言周榜", "发言月榜"}:
            today = self.store.today()
            if text == "发言日榜":
                start = end = today
            elif text == "发言周榜":
                start, end = today.fromordinal(today.toordinal() - today.weekday()), today
            else:
                start, end = today.replace(day=1), today
            return CommandReply(text=self._format_rank(self.store.message_rank(group_id, start, end), text, "条"))
        if text.startswith("查发言图"):
            target = self._extract_target(event, text)
            title = "本群近 30 天发言趋势" if not target else f"{target} 近 30 天发言趋势"
            return CommandReply(image_path=render_trend_chart(title, self.store.message_trend(group_id, target)))
        if text == "签到":
            inserted, total = self.store.check_in(group_id, user_id)
            return CommandReply(text=(f"签到成功！累计签到 {total} 天。" if inserted else f"今天已经签到过了，累计签到 {total} 天。"))
        if text == "签到排行":
            rows = [(user, total) for user, total, _ in self.store.checkin_rank(group_id)]
            return CommandReply(text=self._format_rank(rows, "签到排行", "天"))
        if text == "我的邀请":
            return CommandReply(text=f"你在本群累计有效邀请 {self.store.invite_total(group_id, user_id)} 人。")
        if text == "邀请排行":
            return CommandReply(text=self._format_rank(self.store.invite_rank(group_id), "邀请排行", "人"))
        if text.startswith("查邀请图"):
            target = self._extract_target(event, text)
            title = "本群近 30 天邀请趋势" if not target else f"{target} 近 30 天邀请趋势"
            return CommandReply(image_path=render_trend_chart(title, self.store.invite_trend(group_id, target)))

        # 乐园：赛车。
        if text == "赛车游戏":
            if not self.games.start_race(group_id):
                return CommandReply(text="本群已有一场赛车竞猜，请发送“选车 1”至“选车 6”参与。")
            self.race_tasks[group_id] = asyncio.create_task(self._finish_race_after(event, group_id))
            return CommandReply(text=f"🏎️ 赛车竞猜开始！{self._config_int('race_seconds', 60)} 秒内发送“选车 1”到“选车 6”。")
        car_match = re.fullmatch(r"选车\s*([1-6])", text)
        if car_match:
            car = int(car_match.group(1))
            if not self.games.choose_race_car(group_id, user_id, car):
                return CommandReply(text="当前没有进行中的赛车竞猜。")
            return CommandReply(text=f"已选择 {car} 号赛车。")

        # 乐园：谁是卧底。
        if text == "谁是卧底":
            game = self.games.create_undercover_lobby(group_id, user_id, event.get_sender_name() or user_id)
            return CommandReply(text="卧底大厅已创建，发送“加入卧底”报名；发起人发送“开始卧底”开局。" if game else "本群已有一局卧底游戏。")
        if text == "加入卧底":
            _, message = self.games.join_undercover(group_id, user_id, event.get_sender_name() or user_id)
            return CommandReply(text=message)
        if text == "开始卧底":
            return await self._start_undercover(event)
        if text == "开始投票":
            success, message = self.games.start_vote(group_id, user_id)
            if success:
                old_task = self.vote_tasks.pop(group_id, None)
                if old_task:
                    old_task.cancel()
                self.vote_tasks[group_id] = asyncio.create_task(self._finish_vote_after(event, group_id))
            return CommandReply(text=message)
        if text.startswith("投票"):
            target = self._extract_target(event, text)
            if not target:
                return CommandReply(text="请使用“投票 @成员”或“投票 QQ号”。")
            success, message, all_voted = self.games.cast_vote(group_id, user_id, target)
            if success and all_voted:
                task = self.vote_tasks.pop(group_id, None)
                if task:
                    task.cancel()
                outcome = self.games.finish_vote(group_id)
                message += "\n" + (outcome or "")
            return CommandReply(text=message)

        # 乐园：语音聊天模式管理。
        voice_match = re.fullmatch(r"语音互动\s*(开|关)?", text)
        if voice_match:
            state = voice_match.group(1)
            if not state:
                enabled = self.store.get_group_setting(group_id, "voice_mode", "0") == "1"
                return CommandReply(text=f"语音互动当前{'已开启' if enabled else '未开启'}。管理员可发送“语音互动 开/关”。")
            denied = await self._require_manager(event)
            if denied:
                return CommandReply(text=denied)
            self.store.set_group_setting(group_id, "voice_mode", "1" if state == "开" else "0")
            if state == "开":
                return CommandReply(text="语音互动已开启。群内语音会发送给已配置的 STT、聊天模型和 TTS 服务以生成回复，可能产生模型费用。")
            return CommandReply(text="语音互动已关闭。")

        # 保安设置。
        security_match = re.fullmatch(r"(开|关|关闭)(卡片|号码|链接|图片|二维码)\s*(撤回|禁言)?", text)
        if security_match:
            denied = await self._require_manager(event)
            if denied:
                return CommandReply(text=denied)
            verb, label, action_label = security_match.groups()
            if verb == "开" and not action_label:
                return CommandReply(text="请指定动作，例如“开链接撤回”或“开链接禁言”。")
            kind = SECURITY_LABELS[label]
            self.store.set_security_rule(group_id, kind, ACTION_LABELS[action_label] if verb == "开" else None)
            return CommandReply(text=(f"已开启{label}{action_label}。" if verb == "开" else f"已关闭{label}保安。"))

        # 村规关键词。
        keyword_match = re.fullmatch(r"(加|删)(撤回词|禁言词|踢人词)\s+(.+)", text)
        if keyword_match:
            denied = await self._require_manager(event)
            if denied:
                return CommandReply(text=denied)
            verb, label, keyword = keyword_match.groups()
            keyword = keyword.strip()
            if not keyword or len(keyword) > 100:
                return CommandReply(text="关键词长度需为 1–100 个字符。")
            action = KEYWORD_LABELS[label]
            if verb == "加":
                self.store.set_keyword_rule(group_id, keyword, action)
                return CommandReply(text=f"已添加{label}：{keyword}")
            deleted = self.store.delete_keyword_rule(group_id, keyword, action)
            return CommandReply(text=(f"已删除{label}：{keyword}" if deleted else "未找到该关键词。"))

        # 群管理命令。
        if text in {"安静", "说话"}:
            denied = await self._require_manager(event)
            if denied:
                return CommandReply(text=denied)
            try:
                await self._onebot_action(
                    event,
                    "set_group_whole_ban",
                    group_id=self._number(group_id),
                    enable=text == "安静",
                )
                return CommandReply(text="已开启全员禁言。" if text == "安静" else "已解除全员禁言。")
            except Exception as exc:
                return CommandReply(text=f"操作失败：{exc}")
        if text == "撤回" or re.fullmatch(r"撤回\s+\d+", text):
            denied = await self._require_manager(event)
            if denied:
                return CommandReply(text=denied)
            message_id = self._extract_reply_message_id(event)
            if not message_id:
                parts = text.split()
                message_id = parts[1] if len(parts) == 2 else None
            if not message_id:
                return CommandReply(text="请回复目标消息后发送“撤回”，或使用“撤回 消息ID”。")
            try:
                await self._onebot_action(event, "delete_msg", message_id=self._number(message_id))
                return CommandReply(text="已撤回目标消息。")
            except Exception as exc:
                return CommandReply(text=f"撤回失败：{exc}")
        manage_match = re.match(r"^(上管|下管|禁言|解禁|踢回|拉白|删白|拉黑|删黑)(?:\s|$)", text)
        if manage_match:
            denied = await self._require_manager(event)
            if denied:
                return CommandReply(text=denied)
            command = manage_match.group(1)
            target = self._extract_target(event, text)
            if not target:
                return CommandReply(text=f"请使用“{command} @成员”或“{command} QQ号”。")
            try:
                if command in {"上管", "下管"}:
                    await self._onebot_action(
                        event,
                        "set_group_admin",
                        group_id=self._number(group_id),
                        user_id=self._number(target),
                        enable=command == "上管",
                    )
                    return CommandReply(text=("已设置群管理员。" if command == "上管" else "已取消群管理员。"))
                if command in {"禁言", "解禁"}:
                    minutes = self._extract_duration(event, text, target) if command == "禁言" else 0
                    await self._onebot_action(
                        event,
                        "set_group_ban",
                        group_id=self._number(group_id),
                        user_id=self._number(target),
                        duration=minutes * 60,
                    )
                    return CommandReply(text=(f"已禁言 {minutes} 分钟。" if minutes else "已解除禁言。"))
                if command == "踢回":
                    await self._onebot_action(
                        event,
                        "set_group_kick",
                        group_id=self._number(group_id),
                        user_id=self._number(target),
                        reject_add_request=True,
                    )
                    return CommandReply(text="已踢出成员并拒绝其加群申请。")
                if command in {"拉白", "删白", "拉黑", "删黑"}:
                    list_type = "white" if "白" in command else "black"
                    present = command.startswith("拉")
                    self.store.set_list_member(group_id, target, list_type, present)
                    if list_type == "black" and present:
                        try:
                            await self._onebot_action(
                                event,
                                "set_group_kick",
                                group_id=self._number(group_id),
                                user_id=self._number(target),
                                reject_add_request=True,
                            )
                        except Exception as exc:
                            logger.warning("黑名单成员即时踢出失败: %s", exc)
                    return CommandReply(text=f"已{'加入' if present else '移出'}{'白' if list_type == 'white' else '黑'}名单。")
            except Exception as exc:
                return CommandReply(text=f"操作失败：{exc}")
        return None

    @filter.event_message_type(filter.EventMessageType.ALL)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_onebot_event(self, event: AstrMessageEvent):
        """处理普通消息、群成员通知和无前缀中文命令。"""
        raw = self._raw(event)
        if raw.get("post_type") == "notice":
            await self._handle_notice(event)
            event.stop_event()
            return
        text = (event.message_str or "").strip()
        reply = await self._dispatch_command(event, text)
        if reply is not None:
            event.stop_event()
            if reply.image_path:
                if reply.image_path.startswith("record:"):
                    audio_path = reply.image_path.removeprefix("record:")
                    yield event.chain_result([Plain(reply.text or ""), Record.fromFileSystem(audio_path)])
                else:
                    if reply.image_path.endswith(".png"):
                        event.track_temporary_local_file(reply.image_path)
                    yield event.image_result(reply.image_path)
            if reply.text and (not reply.image_path or not reply.image_path.startswith("record:")):
                yield event.plain_result(reply.text)
            return
        if not self._is_group(event):
            return
        # 语音模式处理位于统计之前，避免普通消息分支重复调用模型。
        if any(self._component_name(item) == "record" for item in event.get_messages()):
            voice_reply = await self._handle_voice(event)
            if voice_reply.text or voice_reply.image_path:
                event.stop_event()
                if voice_reply.image_path and voice_reply.image_path.startswith("record:"):
                    audio_path = voice_reply.image_path.removeprefix("record:")
                    yield event.chain_result([Plain(voice_reply.text or ""), Record.fromFileSystem(audio_path)])
                elif voice_reply.text:
                    yield event.plain_result(voice_reply.text)
                return
        if event.get_sender_id() == event.get_self_id():
            return
        if await self._moderate_message(event):
            return
        self.store.record_message(self._group_id(event), event.get_sender_id())

    async def terminate(self) -> None:
        """插件卸载/重载时取消内存游戏，关闭数据库。"""
        for task in [*self.race_tasks.values(), *self.vote_tasks.values()]:
            task.cancel()
        self.race_tasks.clear()
        self.vote_tasks.clear()
        self.store.close()
