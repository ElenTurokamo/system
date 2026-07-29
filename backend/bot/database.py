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
    first_name      TEXT,
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
    last_reg_message_id INTEGER,         -- id последнего служебного/шагового сообщения (для удаления)
    profile_chat_id     INTEGER,         -- чат, где закреплена сводка профиля
    profile_message_id  INTEGER          -- id закреплённого сообщения-сводки
);

CREATE TABLE IF NOT EXISTS challenges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER,
    status          TEXT,   -- awaiting_action / completed / completed_with_photo / gave_up / expired
    quest_text      TEXT,   -- исходный текст испытания (не меняется, к нему дописывается футер)
    active_focus    TEXT,   -- какой фокус сейчас выбран (принимает числа)
    bonus_claimed   INTEGER DEFAULT 0,  -- секретный бонус х2 (на ВСЕ дисциплины сразу) получен
    physical_photo_done   INTEGER DEFAULT 0,  -- фото по физической части отправлено/пропущено
    physical_photo_posted INTEGER DEFAULT 0,  -- фото реально ушло в группу
    started_at      TEXT,
    completed_at    TEXT,
    message_id      INTEGER,
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS challenge_progress (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    challenge_id    INTEGER,
    focus           TEXT,
    target          INTEGER,
    amount          INTEGER DEFAULT 0,
    completed       INTEGER DEFAULT 0,   -- цель достигнута (amount >= target)
    bonus_claimed   INTEGER DEFAULT 0,   -- секретный бонус х2 уже получен, дисциплина запечатана
    FOREIGN KEY(challenge_id) REFERENCES challenges(id)
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
        existing_users = {row[1] for row in await cur.fetchall()}

        new_user_columns = {
            "first_name": "TEXT",
            "last_reg_message_id": "INTEGER",
            "profile_chat_id": "INTEGER",
            "profile_message_id": "INTEGER",
        }
        changed = False
        for name, col_type in new_user_columns.items():
            if name not in existing_users:
                await self._conn.execute(f"ALTER TABLE users ADD COLUMN {name} {col_type}")
                changed = True

        cur = await self._conn.execute("PRAGMA table_info(challenges)")
        existing_challenges = {row[1] for row in await cur.fetchall()}

        new_challenge_columns = {
            "quest_text": "TEXT",
            "active_focus": "TEXT",
            "bonus_claimed": "INTEGER DEFAULT 0",
            "physical_photo_done": "INTEGER DEFAULT 0",
            "physical_photo_posted": "INTEGER DEFAULT 0",
        }
        for name, col_type in new_challenge_columns.items():
            if name not in existing_challenges:
                await self._conn.execute(f"ALTER TABLE challenges ADD COLUMN {name} {col_type}")
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

    async def create_user_if_missing(self, user_id: int, username: str, first_name: str = ""):
        existing = await self.get_user(user_id)
        if existing:
            return existing
        await self._conn.execute(
            """INSERT INTO users (user_id, username, first_name, registered_at, reg_state)
               VALUES (?, ?, ?, ?, 'time_of_day')""",
            (user_id, username, first_name, datetime.utcnow().isoformat()),
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
        from bot.config import level_from_total_xp

        new_level, _, _ = level_from_total_xp(new_xp)
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

    async def create_challenge(self, user_id: int, quest_text: str, focuses: list[str]) -> int:
        """Создаёт испытание дня и по одной строке прогресса на каждый выбранный фокус.

        Цель по каждому фокусу зависит от текущего уровня игрока - чем выше уровень,
        тем ближе цель к потолку сложности (см. target_for_level в config.py)."""
        from bot.config import level_from_total_xp, target_for_level

        user = await self.get_user(user_id)
        # Уровень пересчитывается из XP напрямую (а не берётся из закэшированного
        # поля level), чтобы сложность сразу была верной даже для игроков, у которых
        # level в БД ещё не пересчитан по новой формуле (обновится при первом add_xp).
        level, _, _ = level_from_total_xp(user["xp"]) if user else (1, 0, 0)

        now = datetime.utcnow().isoformat()
        cur = await self._conn.execute(
            """INSERT INTO challenges (user_id, status, quest_text, started_at)
               VALUES (?, 'awaiting_action', ?, ?)""",
            (user_id, quest_text, now),
        )
        challenge_id = cur.lastrowid

        for focus_key in focuses:
            target = target_for_level(focus_key, level)
            await self._conn.execute(
                "INSERT INTO challenge_progress (challenge_id, focus, target) VALUES (?, ?, ?)",
                (challenge_id, focus_key, target),
            )

        await self._conn.commit()
        return challenge_id

    async def get_active_challenge(self, user_id: int) -> Optional[dict]:
        cur = await self._conn.execute(
            """SELECT * FROM challenges WHERE user_id = ?
               AND status = 'awaiting_action'
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

    async def set_active_focus(self, challenge_id: int, focus_key: Optional[str]):
        await self._conn.execute(
            "UPDATE challenges SET active_focus = ? WHERE id = ?", (focus_key, challenge_id)
        )
        await self._conn.commit()

    async def set_status(self, challenge_id: int, status: str):
        await self._conn.execute(
            "UPDATE challenges SET status = ? WHERE id = ?", (status, challenge_id)
        )
        await self._conn.commit()

    async def mark_challenge_bonus_claimed(self, challenge_id: int):
        await self._conn.execute(
            "UPDATE challenges SET bonus_claimed = 1 WHERE id = ?", (challenge_id,)
        )
        await self._conn.commit()

    async def mark_physical_photo(self, challenge_id: int, posted: bool):
        await self._conn.execute(
            "UPDATE challenges SET physical_photo_done = 1, physical_photo_posted = ? WHERE id = ?",
            (int(posted), challenge_id),
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
            "UPDATE challenges SET status = 'gave_up', active_focus = NULL, completed_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), challenge_id),
        )
        await self._conn.commit()

    async def get_expirable_challenges(self, timeout_hours: int) -> list[dict]:
        cutoff = (datetime.utcnow() - timedelta(hours=timeout_hours)).isoformat()
        cur = await self._conn.execute(
            """SELECT * FROM challenges
               WHERE status = 'awaiting_action'
               AND started_at < ?""",
            (cutoff,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def expire_challenge(self, challenge_id: int):
        await self._conn.execute(
            "UPDATE challenges SET status = 'expired', active_focus = NULL WHERE id = ?",
            (challenge_id,),
        )
        await self._conn.commit()

    # ---------- challenge progress (per-focus targets) ----------

    async def get_progress_rows(self, challenge_id: int) -> list[dict]:
        cur = await self._conn.execute(
            "SELECT * FROM challenge_progress WHERE challenge_id = ? ORDER BY id ASC",
            (challenge_id,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_progress(self, challenge_id: int, focus_key: str) -> Optional[dict]:
        cur = await self._conn.execute(
            "SELECT * FROM challenge_progress WHERE challenge_id = ? AND focus = ?",
            (challenge_id, focus_key),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def add_progress_amount(self, challenge_id: int, focus_key: str, amount: int) -> dict:
        """Добавляет подход к прогрессу по фокусу и возвращает обновлённую строку."""
        await self._conn.execute(
            """UPDATE challenge_progress SET amount = amount + ?
               WHERE challenge_id = ? AND focus = ?""",
            (amount, challenge_id, focus_key),
        )
        await self._conn.commit()
        return await self.get_progress(challenge_id, focus_key)

    async def mark_progress_completed(self, challenge_id: int, focus_key: str):
        await self._conn.execute(
            "UPDATE challenge_progress SET completed = 1 WHERE challenge_id = ? AND focus = ?",
            (challenge_id, focus_key),
        )
        await self._conn.commit()

    async def all_progress_completed(self, challenge_id: int) -> bool:
        cur = await self._conn.execute(
            "SELECT COUNT(*) as cnt FROM challenge_progress WHERE challenge_id = ? AND completed = 0",
            (challenge_id,),
        )
        row = await cur.fetchone()
        return row["cnt"] == 0


db = Database()
