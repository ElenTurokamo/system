"""
Слой доступа к данным. Файл базы — обычный SQLite (db.sql), как и просил автор идеи.
Все операции асинхронные (aiosqlite), чтобы не блокировать event loop бота.
"""
import json
from datetime import datetime, timedelta
from typing import Any, Optional

import aiosqlite

from bot.config import FOCUS_OPTIONS, settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id         INTEGER PRIMARY KEY,
    username        TEXT,
    registered_at   TEXT,
    time_of_day     TEXT,               -- morning / day / evening / night
    focuses         TEXT DEFAULT '[]',  -- JSON-список ключей из FOCUS_OPTIONS
    group_id        INTEGER,
    xp              INTEGER DEFAULT 0,
    level           INTEGER DEFAULT 1,
    streak          INTEGER DEFAULT 0,
    penalty_until   TEXT,
    daily_pushups   INTEGER DEFAULT 0,
    daily_squats    INTEGER DEFAULT 0,
    daily_abs       INTEGER DEFAULT 0,
    daily_chess     INTEGER DEFAULT 0,
    daily_reading   INTEGER DEFAULT 0,
    reg_state       TEXT DEFAULT 'done', -- FSM-состояние регистрации
    last_reg_message_id INTEGER,         -- id последнего экрана регистрации (для удаления)
    profile_chat_id     INTEGER,         -- чат, где закреплена сводка профиля
    profile_message_id  INTEGER          -- id закреплённого сообщения-сводки
);

CREATE TABLE IF NOT EXISTS challenges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER,
    focus           TEXT,
    status          TEXT,   -- awaiting_focus / in_progress / awaiting_photo / completed / gave_up / expired
    started_at      TEXT,
    completed_at    TEXT,
    amount          INTEGER DEFAULT 0,
    message_id      INTEGER,
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);
"""


class Database:
    def __init__(self, path: str = settings.db_path):
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        await self._migrate()

    async def _migrate(self):
        """Добавляет новые колонки в уже существующие БД, не трогая данные."""
        cur = await self._conn.execute("PRAGMA table_info(users)")
        existing = {row[1] for row in await cur.fetchall()}

        new_columns = {
            "last_reg_message_id": "INTEGER",
            "profile_chat_id": "INTEGER",
            "profile_message_id": "INTEGER",
        }
        changed = False
        for name, col_type in new_columns.items():
            if name not in existing:
                await self._conn.execute(f"ALTER TABLE users ADD COLUMN {name} {col_type}")
                changed = True
        if changed:
            await self._conn.commit()

    async def close(self):
        if self._conn:
            await self._conn.close()

    # ---------- users ----------

    async def get_user(self, user_id: int) -> Optional[dict]:
        cur = await self._conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def create_user_if_missing(self, user_id: int, username: str):
        existing = await self.get_user(user_id)
        if existing:
            return existing
        await self._conn.execute(
            "INSERT INTO users (user_id, username, registered_at, reg_state) VALUES (?, ?, ?, 'time_of_day')",
            (user_id, username, datetime.utcnow().isoformat()),
        )
        await self._conn.commit()
        return await self.get_user(user_id)

    async def set_reg_state(self, user_id: int, state: str):
        await self._conn.execute("UPDATE users SET reg_state = ? WHERE user_id = ?", (state, user_id))
        await self._conn.commit()

    async def set_time_of_day(self, user_id: int, time_of_day: str):
        await self._conn.execute(
            "UPDATE users SET time_of_day = ? WHERE user_id = ?", (time_of_day, user_id)
        )
        await self._conn.commit()

    async def get_focuses(self, user_id: int) -> list[str]:
        user = await self.get_user(user_id)
        return json.loads(user["focuses"]) if user and user["focuses"] else []

    async def toggle_focus(self, user_id: int, focus_key: str):
        focuses = await self.get_focuses(user_id)
        if focus_key in focuses:
            focuses.remove(focus_key)
        else:
            focuses.append(focus_key)
        await self._conn.execute(
            "UPDATE users SET focuses = ? WHERE user_id = ?", (json.dumps(focuses), user_id)
        )
        await self._conn.commit()
        return focuses

    async def set_group(self, user_id: int, group_id: int):
        await self._conn.execute(
            "UPDATE users SET group_id = ? WHERE user_id = ?", (group_id, user_id)
        )
        await self._conn.commit()

    async def finish_registration(self, user_id: int):
        await self.set_reg_state(user_id, "done")

    async def set_last_reg_message(self, user_id: int, message_id: Optional[int]):
        await self._conn.execute(
            "UPDATE users SET last_reg_message_id = ? WHERE user_id = ?", (message_id, user_id)
        )
        await self._conn.commit()

    async def set_profile_message(self, user_id: int, chat_id: int, message_id: int):
        await self._conn.execute(
            "UPDATE users SET profile_chat_id = ?, profile_message_id = ? WHERE user_id = ?",
            (chat_id, message_id, user_id),
        )
        await self._conn.commit()

    async def get_users_by_time(self, time_of_day: str) -> list[dict]:
        cur = await self._conn.execute(
            "SELECT * FROM users WHERE time_of_day = ? AND reg_state = 'done'", (time_of_day,)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def add_xp(self, user_id: int, amount: int):
        user = await self.get_user(user_id)
        new_xp = user["xp"] + amount
        from bot.config import XP_PER_LEVEL

        new_level = new_xp // XP_PER_LEVEL + 1
        leveled_up = new_level > user["level"]
        await self._conn.execute(
            "UPDATE users SET xp = ?, level = ? WHERE user_id = ?", (new_xp, new_level, user_id)
        )
        await self._conn.commit()
        return new_xp, new_level, leveled_up

    async def increment_streak(self, user_id: int):
        await self._conn.execute(
            "UPDATE users SET streak = streak + 1 WHERE user_id = ?", (user_id,)
        )
        await self._conn.commit()
        user = await self.get_user(user_id)
        return user["streak"]

    async def reset_streak(self, user_id: int):
        await self._conn.execute("UPDATE users SET streak = 0 WHERE user_id = ?", (user_id,))
        await self._conn.commit()

    async def add_focus_amount(self, user_id: int, focus_key: str, amount: int):
        field = FOCUS_OPTIONS[focus_key]["counter_field"]
        await self._conn.execute(
            f"UPDATE users SET {field} = {field} + ? WHERE user_id = ?", (amount, user_id)
        )
        await self._conn.commit()

    async def set_penalty(self, user_id: int, hours: int):
        until = (datetime.utcnow() + timedelta(hours=hours)).isoformat()
        await self._conn.execute(
            "UPDATE users SET penalty_until = ? WHERE user_id = ?", (until, user_id)
        )
        await self._conn.commit()
        return until

    # ---------- challenges ----------

    async def create_challenge(self, user_id: int) -> int:
        now = datetime.utcnow().isoformat()
        cur = await self._conn.execute(
            "INSERT INTO challenges (user_id, status, started_at) VALUES (?, 'awaiting_focus', ?)",
            (user_id, now),
        )
        await self._conn.commit()
        return cur.lastrowid

    async def get_active_challenge(self, user_id: int) -> Optional[dict]:
        cur = await self._conn.execute(
            """SELECT * FROM challenges WHERE user_id = ?
               AND status IN ('awaiting_focus', 'in_progress', 'awaiting_photo')
               ORDER BY id DESC LIMIT 1""",
            (user_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_challenge(self, challenge_id: int) -> Optional[dict]:
        cur = await self._conn.execute("SELECT * FROM challenges WHERE id = ?", (challenge_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def set_message_id(self, challenge_id: int, message_id: int):
        await self._conn.execute(
            "UPDATE challenges SET message_id = ? WHERE id = ?", (message_id, challenge_id)
        )
        await self._conn.commit()

    async def set_challenge_focus(self, challenge_id: int, focus_key: str):
        await self._conn.execute(
            "UPDATE challenges SET focus = ?, status = 'in_progress' WHERE id = ?",
            (focus_key, challenge_id),
        )
        await self._conn.commit()

    async def set_challenge_amount(self, challenge_id: int, amount: int):
        await self._conn.execute(
            "UPDATE challenges SET amount = ?, status = 'awaiting_photo' WHERE id = ?",
            (amount, challenge_id),
        )
        await self._conn.commit()

    async def complete_challenge(self, challenge_id: int, with_photo: bool):
        status = "completed_with_photo" if with_photo else "completed"
        await self._conn.execute(
            "UPDATE challenges SET status = ?, completed_at = ? WHERE id = ?",
            (status, datetime.utcnow().isoformat(), challenge_id),
        )
        await self._conn.commit()

    async def give_up_challenge(self, challenge_id: int):
        await self._conn.execute(
            "UPDATE challenges SET status = 'gave_up', completed_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), challenge_id),
        )
        await self._conn.commit()

    async def get_expirable_challenges(self, timeout_hours: int) -> list[dict]:
        cutoff = (datetime.utcnow() - timedelta(hours=timeout_hours)).isoformat()
        cur = await self._conn.execute(
            """SELECT * FROM challenges
               WHERE status IN ('awaiting_focus', 'in_progress', 'awaiting_photo')
               AND started_at < ?""",
            (cutoff,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def expire_challenge(self, challenge_id: int):
        await self._conn.execute(
            "UPDATE challenges SET status = 'expired' WHERE id = ?", (challenge_id,)
        )
        await self._conn.commit()


db = Database()
