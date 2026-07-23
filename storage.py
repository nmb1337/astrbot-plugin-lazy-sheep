"""插件的 SQLite 持久化层。所有数据按群隔离。"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class LazySheepStore:
    def __init__(self, database_path: Path, timezone_name: str = "Asia/Shanghai") -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database_path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        try:
            self.timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            self.timezone = ZoneInfo("Asia/Shanghai")
        self._migrate()

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    def _migrate(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS group_settings (
                group_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                PRIMARY KEY (group_id, key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS member_lists (
                group_id TEXT NOT NULL, user_id TEXT NOT NULL, list_type TEXT NOT NULL,
                PRIMARY KEY (group_id, user_id, list_type)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS group_whitelist (
                group_id TEXT PRIMARY KEY, added_by TEXT NOT NULL, added_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS security_rules (
                group_id TEXT NOT NULL, kind TEXT NOT NULL, action TEXT NOT NULL,
                PRIMARY KEY (group_id, kind)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS keyword_rules (
                group_id TEXT NOT NULL, keyword TEXT NOT NULL, action TEXT NOT NULL,
                PRIMARY KEY (group_id, keyword)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS message_daily (
                group_id TEXT NOT NULL, user_id TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL,
                PRIMARY KEY (group_id, user_id, day)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS checkins (
                group_id TEXT NOT NULL, user_id TEXT NOT NULL, day TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY (group_id, user_id, day)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS invites (
                group_id TEXT NOT NULL, invitee_id TEXT NOT NULL, inviter_id TEXT NOT NULL,
                day TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY (group_id, invitee_id)
            )
            """,
        )
        with self.lock:
            for statement in statements:
                self.connection.execute(statement)
            self.connection.commit()

    def today(self) -> date:
        return datetime.now(self.timezone).date()

    @staticmethod
    def _day_string(day: date) -> str:
        return day.isoformat()

    def set_group_setting(self, group_id: str, key: str, value: str) -> None:
        with self.lock:
            self.connection.execute(
                "INSERT INTO group_settings(group_id,key,value) VALUES(?,?,?) "
                "ON CONFLICT(group_id,key) DO UPDATE SET value=excluded.value",
                (group_id, key, value),
            )
            self.connection.commit()

    def get_group_setting(self, group_id: str, key: str, default: str = "") -> str:
        with self.lock:
            row = self.connection.execute(
                "SELECT value FROM group_settings WHERE group_id=? AND key=?", (group_id, key)
            ).fetchone()
        return str(row["value"]) if row else default

    def set_group_gate_enabled(self, enabled: bool) -> None:
        self.set_group_setting("__global__", "group_whitelist_gate", "1" if enabled else "0")

    def is_group_gate_enabled(self) -> bool:
        return self.get_group_setting("__global__", "group_whitelist_gate", "0") == "1"

    def set_group_whitelisted(self, group_id: str, present: bool, actor_id: str = "system") -> None:
        now = datetime.now(self.timezone).isoformat(timespec="seconds")
        with self.lock:
            if present:
                self.connection.execute(
                    "INSERT INTO group_whitelist(group_id,added_by,added_at) VALUES(?,?,?) "
                    "ON CONFLICT(group_id) DO UPDATE SET added_by=excluded.added_by,added_at=excluded.added_at",
                    (group_id, actor_id, now),
                )
            else:
                self.connection.execute("DELETE FROM group_whitelist WHERE group_id=?", (group_id,))
            self.connection.commit()

    def is_group_whitelisted(self, group_id: str) -> bool:
        with self.lock:
            row = self.connection.execute(
                "SELECT 1 FROM group_whitelist WHERE group_id=?", (group_id,)
            ).fetchone()
        return row is not None

    def list_group_whitelist(self) -> list[tuple[str, str, str]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT group_id,added_by,added_at FROM group_whitelist ORDER BY group_id"
            ).fetchall()
        return [(str(row["group_id"]), str(row["added_by"]), str(row["added_at"])) for row in rows]

    def set_list_member(self, group_id: str, user_id: str, list_type: str, present: bool) -> None:
        with self.lock:
            if present:
                self.connection.execute(
                    "INSERT OR IGNORE INTO member_lists(group_id,user_id,list_type) VALUES(?,?,?)",
                    (group_id, user_id, list_type),
                )
            else:
                self.connection.execute(
                    "DELETE FROM member_lists WHERE group_id=? AND user_id=? AND list_type=?",
                    (group_id, user_id, list_type),
                )
            self.connection.commit()

    def is_list_member(self, group_id: str, user_id: str, list_type: str) -> bool:
        with self.lock:
            row = self.connection.execute(
                "SELECT 1 FROM member_lists WHERE group_id=? AND user_id=? AND list_type=?",
                (group_id, user_id, list_type),
            ).fetchone()
        return row is not None

    def set_security_rule(self, group_id: str, kind: str, action: str | None) -> None:
        with self.lock:
            if action:
                self.connection.execute(
                    "INSERT INTO security_rules(group_id,kind,action) VALUES(?,?,?) "
                    "ON CONFLICT(group_id,kind) DO UPDATE SET action=excluded.action",
                    (group_id, kind, action),
                )
            else:
                self.connection.execute(
                    "DELETE FROM security_rules WHERE group_id=? AND kind=?", (group_id, kind)
                )
            self.connection.commit()

    def get_security_rules(self, group_id: str) -> dict[str, str]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT kind,action FROM security_rules WHERE group_id=?", (group_id,)
            ).fetchall()
        return {str(row["kind"]): str(row["action"]) for row in rows}

    def set_keyword_rule(self, group_id: str, keyword: str, action: str) -> None:
        with self.lock:
            self.connection.execute(
                "INSERT INTO keyword_rules(group_id,keyword,action) VALUES(?,?,?) "
                "ON CONFLICT(group_id,keyword) DO UPDATE SET action=excluded.action",
                (group_id, keyword, action),
            )
            self.connection.commit()

    def delete_keyword_rule(self, group_id: str, keyword: str, action: str) -> bool:
        with self.lock:
            cursor = self.connection.execute(
                "DELETE FROM keyword_rules WHERE group_id=? AND keyword=? AND action=?",
                (group_id, keyword, action),
            )
            self.connection.commit()
        return cursor.rowcount > 0

    def get_keyword_rules(self, group_id: str) -> list[tuple[str, str]]:
        # 踢人 > 禁言 > 撤回，使同一消息命中多个词时行为可预测。
        priority = {"kick": 0, "mute": 1, "recall": 2}
        with self.lock:
            rows = self.connection.execute(
                "SELECT keyword,action FROM keyword_rules WHERE group_id=?", (group_id,)
            ).fetchall()
        return sorted(
            ((str(row["keyword"]), str(row["action"])) for row in rows),
            key=lambda item: priority.get(item[1], 99),
        )

    def record_message(self, group_id: str, user_id: str, day: date | None = None) -> None:
        day = day or self.today()
        with self.lock:
            self.connection.execute(
                "INSERT INTO message_daily(group_id,user_id,day,count) VALUES(?,?,?,1) "
                "ON CONFLICT(group_id,user_id,day) DO UPDATE SET count=count+1",
                (group_id, user_id, self._day_string(day)),
            )
            self.connection.commit()

    def message_total(self, group_id: str, user_id: str) -> int:
        with self.lock:
            row = self.connection.execute(
                "SELECT COALESCE(SUM(count),0) AS total FROM message_daily WHERE group_id=? AND user_id=?",
                (group_id, user_id),
            ).fetchone()
        return int(row["total"])

    def message_rank(self, group_id: str, start: date, end: date, limit: int = 10) -> list[tuple[str, int]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT user_id,SUM(count) AS total FROM message_daily "
                "WHERE group_id=? AND day BETWEEN ? AND ? GROUP BY user_id "
                "ORDER BY total DESC,user_id LIMIT ?",
                (group_id, self._day_string(start), self._day_string(end), limit),
            ).fetchall()
        return [(str(row["user_id"]), int(row["total"])) for row in rows]

    def message_trend(self, group_id: str, user_id: str | None, days: int = 30) -> list[tuple[date, int]]:
        end = self.today()
        start = end - timedelta(days=days - 1)
        parameters: list[object] = [group_id, self._day_string(start), self._day_string(end)]
        user_filter = ""
        if user_id:
            user_filter = " AND user_id=?"
            parameters.append(user_id)
        with self.lock:
            rows = self.connection.execute(
                "SELECT day,SUM(count) AS total FROM message_daily WHERE group_id=? "
                "AND day BETWEEN ? AND ?" + user_filter + " GROUP BY day",
                parameters,
            ).fetchall()
        counts = {date.fromisoformat(str(row["day"])): int(row["total"]) for row in rows}
        return [(start + timedelta(days=offset), counts.get(start + timedelta(days=offset), 0)) for offset in range(days)]

    def check_in(self, group_id: str, user_id: str) -> tuple[bool, int]:
        today = self.today()
        timestamp = datetime.now(self.timezone).isoformat(timespec="seconds")
        with self.lock:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO checkins(group_id,user_id,day,created_at) VALUES(?,?,?,?)",
                (group_id, user_id, self._day_string(today), timestamp),
            )
            row = self.connection.execute(
                "SELECT COUNT(*) AS total FROM checkins WHERE group_id=? AND user_id=?",
                (group_id, user_id),
            ).fetchone()
            self.connection.commit()
        return cursor.rowcount > 0, int(row["total"])

    def checkin_rank(self, group_id: str, limit: int = 10) -> list[tuple[str, int, str]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT user_id,COUNT(*) AS total,MAX(created_at) AS recent FROM checkins "
                "WHERE group_id=? GROUP BY user_id ORDER BY total DESC,recent ASC,user_id LIMIT ?",
                (group_id, limit),
            ).fetchall()
        return [(str(row["user_id"]), int(row["total"]), str(row["recent"])) for row in rows]

    def record_invite(self, group_id: str, invitee_id: str, inviter_id: str) -> bool:
        if not inviter_id or invitee_id == inviter_id:
            return False
        now = datetime.now(self.timezone)
        with self.lock:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO invites(group_id,invitee_id,inviter_id,day,created_at) VALUES(?,?,?,?,?)",
                (group_id, invitee_id, inviter_id, now.date().isoformat(), now.isoformat(timespec="seconds")),
            )
            self.connection.commit()
        return cursor.rowcount > 0

    def invite_total(self, group_id: str, inviter_id: str) -> int:
        with self.lock:
            row = self.connection.execute(
                "SELECT COUNT(*) AS total FROM invites WHERE group_id=? AND inviter_id=?",
                (group_id, inviter_id),
            ).fetchone()
        return int(row["total"])

    def invite_rank(self, group_id: str, limit: int = 10) -> list[tuple[str, int]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT inviter_id,COUNT(*) AS total FROM invites WHERE group_id=? GROUP BY inviter_id "
                "ORDER BY total DESC,inviter_id LIMIT ?",
                (group_id, limit),
            ).fetchall()
        return [(str(row["inviter_id"]), int(row["total"])) for row in rows]

    def invite_trend(self, group_id: str, inviter_id: str | None, days: int = 30) -> list[tuple[date, int]]:
        end = self.today()
        start = end - timedelta(days=days - 1)
        parameters: list[object] = [group_id, self._day_string(start), self._day_string(end)]
        inviter_filter = ""
        if inviter_id:
            inviter_filter = " AND inviter_id=?"
            parameters.append(inviter_id)
        with self.lock:
            rows = self.connection.execute(
                "SELECT day,COUNT(*) AS total FROM invites WHERE group_id=? AND day BETWEEN ? AND ?"
                + inviter_filter
                + " GROUP BY day",
                parameters,
            ).fetchall()
        counts = {date.fromisoformat(str(row["day"])): int(row["total"]) for row in rows}
        return [(start + timedelta(days=offset), counts.get(start + timedelta(days=offset), 0)) for offset in range(days)]
