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
    daily_pullups   INTEGER DEFAULT 0,
    daily_running   INTEGER DEFAULT 0,
    daily_chess     INTEGER DEFAULT 0,
    daily_reading   INTEGER DEFAULT 0,
    reg_state       TEXT DEFAULT 'done', -- FSM-состояние регистрации
    last_reg_message_id INTEGER,         -- id последнего служебного/шагового сообщения (для удаления)
    profile_chat_id     INTEGER,         -- чат, где закреплена сводка профиля
    profile_message_id  INTEGER,         -- id закреплённого сообщения-сводки
    failure_chat_id      INTEGER,        -- чат последнего сообщения-отчёта о провале испытания
    failure_message_id   INTEGER         -- id последнего сообщения-отчёта о провале (удаляется при новом квесте)
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
    bonus_claimed   INTEGER DEFAULT 0,   -- личный x2 по этой дисциплине достигнут - только метка для 🔥, наград не даёт
    FOREIGN KEY(challenge_id) REFERENCES challenges(id)
);

-- Отдельная таблица под историю прогрессии для выгрузки в Excel (/get_stats_excel).
-- Хранится отдельно от users/challenges, чтобы не раздувать основные таблицы:
-- здесь по одной строке на пользователя на календарный день (локальная дата
-- по TZ из настроек), с итоговыми числами по каждой дисциплине за этот день.
-- Строка создаётся/обновляется в момент закрытия испытания дня (завершено,
-- сдался или просрочено) - см. Database.record_daily_stats.
CREATE TABLE IF NOT EXISTS daily_stats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER,
    date            TEXT,               -- YYYY-MM-DD, локальная дата по TZ
    pushups         INTEGER DEFAULT 0,
    squats          INTEGER DEFAULT 0,
    abs             INTEGER DEFAULT 0,
    pullups         INTEGER DEFAULT 0,
    running         INTEGER DEFAULT 0,
    chess           INTEGER DEFAULT 0,
    reading         INTEGER DEFAULT 0,
    challenge_id    INTEGER,
    UNIQUE(user_id, date)
);

-- Список друзей (/add_friend, /friendlist). Заявка не одностороняя: пока
-- addressee не подтвердил, status='pending' и addressee не получает доступа
-- к данным requester (и наоборот). Одна строка на пару (requester, addressee)
-- в направлении, в котором была отправлена заявка изначально - дружба, будучи
-- принятой, считается двусторонней (см. Database.get_friend_ids), повторная
-- заявка в обратную сторону не создаёт вторую строку (см. find_friendship).
CREATE TABLE IF NOT EXISTS friendships (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    requester_id    INTEGER,
    addressee_id    INTEGER,
    status          TEXT DEFAULT 'pending',  -- pending / accepted
    created_at      TEXT,
    responded_at    TEXT,
    UNIQUE(requester_id, addressee_id)
);

-- "Поддержать" в брифе друга (см. bot/handlers/friends.py). Метка на пару
-- (конкретное испытание, конкретный поддержавший) - двусторонний sync (тоггл),
-- а не одностороннее добавление строки, чтобы один и тот же друг не мог
-- нажать кнопку многократно за одно испытание (та же логика, что и у 🔥
-- личного бонуса в challenge_progress).
CREATE TABLE IF NOT EXISTS challenge_cheers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    challenge_id    INTEGER,
    supporter_id    INTEGER,
    created_at      TEXT,
    UNIQUE(challenge_id, supporter_id)
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
            "failure_chat_id": "INTEGER",
            "failure_message_id": "INTEGER",
            "awaiting_friend_code": "INTEGER DEFAULT 0",
            "friend_prompt_message_id": "INTEGER",
            "daily_pullups": "INTEGER DEFAULT 0",
            "daily_running": "INTEGER DEFAULT 0",
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

        cur = await self._conn.execute("PRAGMA table_info(challenge_progress)")
        existing_progress = {row[1] for row in await cur.fetchall()}
        if "bonus_claimed" not in existing_progress:
            await self._conn.execute(
                "ALTER TABLE challenge_progress ADD COLUMN bonus_claimed INTEGER DEFAULT 0"
            )
            changed = True

        cur = await self._conn.execute("PRAGMA table_info(daily_stats)")
        existing_daily_stats = {row[1] for row in await cur.fetchall()}
        new_daily_stats_columns = {
            "pullups": "INTEGER DEFAULT 0",
            "running": "INTEGER DEFAULT 0",
        }
        for name, col_type in new_daily_stats_columns.items():
            if name not in existing_daily_stats:
                await self._conn.execute(f"ALTER TABLE daily_stats ADD COLUMN {name} {col_type}")
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

    async def set_failure_message(self, user_id: int, chat_id: int, message_id: int):
        await self._conn.execute(
            "UPDATE users SET failure_chat_id = ?, failure_message_id = ? WHERE user_id = ?",
            (chat_id, message_id, user_id),
        )
        await self._conn.commit()

    async def clear_failure_message(self, user_id: int):
        await self._conn.execute(
            "UPDATE users SET failure_chat_id = NULL, failure_message_id = NULL WHERE user_id = ?",
            (user_id,),
        )
        await self._conn.commit()

    async def get_focus_keys_with_history(self, user_id: int) -> set[str]:
        """
        Ключи дисциплин (из FOCUS_OPTIONS), по которым у игрока КОГДА-ЛИБО была
        зафиксирована активность - либо в истории daily_stats (закрытые дни),
        либо прямо сейчас в текущих daily_* счётчиках (день ещё не закрыт, но
        игрок уже что-то отметил). Используется в профиле/брифе друга, чтобы
        не отслеживаемая сейчас дисциплина не пропадала бесследно, если по ней
        когда-то был прогресс - см. bot/profile.py и bot/friends.py.
        """
        cur = await self._conn.execute(
            """SELECT
                   COALESCE(SUM(pushups), 0) AS pushups,
                   COALESCE(SUM(squats), 0) AS squats,
                   COALESCE(SUM(abs), 0) AS abs,
                   COALESCE(SUM(pullups), 0) AS pullups,
                   COALESCE(SUM(running), 0) AS running,
                   COALESCE(SUM(chess), 0) AS chess,
                   COALESCE(SUM(reading), 0) AS reading
               FROM daily_stats WHERE user_id = ?""",
            (user_id,),
        )
        row = await cur.fetchone()
        history_totals = dict(row) if row else {}

        user = await self.get_user(user_id)
        active: set[str] = set()
        for focus_key, opt in FOCUS_OPTIONS.items():
            history_total = history_totals.get(focus_key) or 0
            today_amount = (user or {}).get(opt["counter_field"]) or 0
            if history_total > 0 or today_amount > 0:
                active.add(focus_key)
        return active

    async def get_users_by_time(self, time_of_day: str) -> list[dict]:
        cur = await self._conn.execute(
            "SELECT * FROM users WHERE time_of_day = ? AND reg_state = 'done'", (time_of_day,)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def add_xp(self, user_id: int, amount: int):
        """
        Изменяет суммарный XP игрока (amount может быть отрицательным - например,
        штраф за пропущенный день) и пересчитывает уровень. XP не уходит ниже 0 -
        иначе штрафы могли бы образовать "долг", который потом пришлось бы
        отрабатывать несколько дней подряд просто чтобы вернуться к нулю.
        """
        user = await self.get_user(user_id)
        new_xp = max(0, user["xp"] + amount)
        from bot.config import level_from_total_xp

        new_level, _, _ = level_from_total_xp(new_xp)
        leveled_up = new_level > user["level"]
        leveled_down = new_level < user["level"]
        await self._conn.execute(
            "UPDATE users SET xp = ?, level = ? WHERE user_id = ?", (new_xp, new_level, user_id)
        )
        await self._conn.commit()
        return new_xp, new_level, leveled_up, leveled_down

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
            f"UPDATE users SET {field} = MAX(0, {field} + ?) WHERE user_id = ?", (amount, user_id)
        )
        await self._conn.commit()

    async def get_users_with_active_penalty(self, grace_minutes: int = 2) -> list[dict]:
        """
        Пользователи, у которых сейчас действует ограничение, плюс небольшой
        запас в прошлое (grace_minutes) - чтобы поймать момент, когда штраф
        только что истёк, и один последний раз перерисовать профиль без
        блока ограничения (иначе он "залипнет" в тексте до следующего
        произвольного действия пользователя).
        """
        cutoff = (datetime.utcnow() - timedelta(minutes=grace_minutes)).isoformat()
        cur = await self._conn.execute(
            "SELECT * FROM users WHERE penalty_until IS NOT NULL AND penalty_until > ?",
            (cutoff,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

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

    async def get_latest_challenge(self, user_id: int) -> Optional[dict]:
        """
        Последнее (по времени создания) испытание пользователя, вне зависимости
        от статуса. Используется профилем, чтобы понять, выполнен ли квест на
        сегодня - строка "Вы выполнили сегодняшний квест" в профиле держится,
        пока status последнего испытания completed/completed_with_photo, и
        пропадает сама, как только диспетчер создаёт новое испытание на завтра
        (тогда последним снова становится свежее awaiting_action).
        """
        cur = await self._conn.execute(
            "SELECT * FROM challenges WHERE user_id = ? ORDER BY id DESC LIMIT 1",
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

    async def get_awaiting_challenges(self) -> list[dict]:
        """Все испытания, которые сейчас активны (карточка ждёт действий игрока)."""
        cur = await self._conn.execute(
            "SELECT * FROM challenges WHERE status = 'awaiting_action'"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

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

    async def physical_bonus_achieved(self, challenge_id: int) -> bool:
        """
        Личный x2 достигнут по КАЖДОЙ выбранной ФИЗИЧЕСКОЙ дисциплине (без учёта
        интеллектуальных - шахмат/чтения). Используется отдельно от общего
        секретного бонуса (challenges.bonus_claimed, который требует x2 сразу по
        ВСЕМ выбранным дисциплинам и даёт награду x2 XP) - специально для отметки
        в ежедневном фото-отчёте в группу: физический прогресс оценивается по
        мышцам сразу после тренировки, пока не прошёл памп, поэтому не должен
        зависеть от того, успел ли игрок ещё и закрыть интеллектуальную часть.
        Опирается на per-focus флаг bonus_claimed в challenge_progress, который
        синкается на каждый залогированный подход (см. Database.sync_progress_bonus).
        """
        rows = await self.get_progress_rows(challenge_id)
        physical_rows = [r for r in rows if FOCUS_OPTIONS[r["focus"]]["kind"] == "physical"]
        return bool(physical_rows) and all(r["bonus_claimed"] for r in physical_rows)

    async def add_progress_amount(self, challenge_id: int, focus_key: str, amount: int) -> dict:
        """Добавляет подход к прогрессу по фокусу (может быть отрицательным - исправление
        ошибочно введённого числа) и возвращает обновлённую строку. Не даёт уйти ниже 0."""
        await self._conn.execute(
            """UPDATE challenge_progress SET amount = MAX(0, amount + ?)
               WHERE challenge_id = ? AND focus = ?""",
            (amount, challenge_id, focus_key),
        )
        await self._conn.commit()
        return await self.get_progress(challenge_id, focus_key)

    async def sync_progress_bonus(self, challenge_id: int, focus_key: str, bonus_multiplier: int):
        """
        Приводит флаг bonus_claimed (личный x2 по этой дисциплине, метка 🔥 на
        кнопке) в соответствие текущему amount/target - в обе стороны, точно
        так же, как sync_progress_completed делает это для completed. amount
        может не только расти, но и уменьшаться (исправление ошибочно
        введённого числа), а значит метка должна и сниматься, если amount
        снова опустился ниже target * bonus_multiplier.
        """
        row = await self.get_progress(challenge_id, focus_key)
        if not row:
            return
        should_be_claimed = 1 if row["amount"] >= row["target"] * bonus_multiplier else 0
        if row["bonus_claimed"] != should_be_claimed:
            await self._conn.execute(
                "UPDATE challenge_progress SET bonus_claimed = ? WHERE challenge_id = ? AND focus = ?",
                (should_be_claimed, challenge_id, focus_key),
            )
            await self._conn.commit()

    async def mark_progress_completed(self, challenge_id: int, focus_key: str):
        await self._conn.execute(
            "UPDATE challenge_progress SET completed = 1 WHERE challenge_id = ? AND focus = ?",
            (challenge_id, focus_key),
        )
        await self._conn.commit()

    async def sync_progress_completed(self, challenge_id: int, focus_key: str):
        """
        Приводит флаг completed в соответствие текущему amount/target - в обе стороны.
        Нужно, потому что amount теперь может не только расти, но и уменьшаться
        (исправление ошибочно введённого числа), а значит дисциплина может как
        стать выполненной, так и перестать ею быть.
        """
        row = await self.get_progress(challenge_id, focus_key)
        if not row:
            return
        should_be_completed = 1 if row["amount"] >= row["target"] else 0
        if row["completed"] != should_be_completed:
            await self._conn.execute(
                "UPDATE challenge_progress SET completed = ? WHERE challenge_id = ? AND focus = ?",
                (should_be_completed, challenge_id, focus_key),
            )
            await self._conn.commit()

    async def all_progress_completed(self, challenge_id: int) -> bool:
        cur = await self._conn.execute(
            "SELECT COUNT(*) as cnt FROM challenge_progress WHERE challenge_id = ? AND completed = 0",
            (challenge_id,),
        )
        row = await cur.fetchone()
        return row["cnt"] == 0

    # ---------- daily stats (для /get_stats_excel) ----------

    async def record_daily_stats(self, challenge_id: int):
        """
        Фиксирует итоговые числа по дисциплинам этого испытания в daily_stats -
        по одной строке на пользователя на календарный день (локальная дата по
        TZ из настроек, а не UTC, чтобы день не "съезжал" из-за разницы поясов).

        Вызывается в момент закрытия испытания дня - неважно, завершено оно,
        провалено (сдался) или сгорело по таймауту: игрок мог успеть вписать
        часть повторений даже в проваленный день, и это тоже часть его прогрессии.

        Если строка на эту дату уже есть (тот же день, например повторный
        вызов), значения перезаписываются актуальными - на пользователя и день
        всегда ровно одна строка.
        """
        from zoneinfo import ZoneInfo

        challenge = await self.get_challenge(challenge_id)
        if not challenge:
            return

        try:
            started = datetime.fromisoformat(challenge["started_at"])
            local_date = started.replace(tzinfo=ZoneInfo("UTC")).astimezone(
                ZoneInfo(settings.tz)
            ).date().isoformat()
        except (TypeError, ValueError):
            local_date = datetime.utcnow().date().isoformat()

        amounts = {"pushups": 0, "squats": 0, "abs": 0, "pullups": 0, "running": 0, "chess": 0, "reading": 0}
        rows = await self.get_progress_rows(challenge_id)
        for r in rows:
            if r["focus"] in amounts:
                amounts[r["focus"]] = r["amount"]

        await self._conn.execute(
            """INSERT INTO daily_stats (user_id, date, pushups, squats, abs, pullups, running, chess, reading, challenge_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, date) DO UPDATE SET
                   pushups = excluded.pushups,
                   squats = excluded.squats,
                   abs = excluded.abs,
                   pullups = excluded.pullups,
                   running = excluded.running,
                   chess = excluded.chess,
                   reading = excluded.reading,
                   challenge_id = excluded.challenge_id""",
            (
                challenge["user_id"],
                local_date,
                amounts["pushups"],
                amounts["squats"],
                amounts["abs"],
                amounts["pullups"],
                amounts["running"],
                amounts["chess"],
                amounts["reading"],
                challenge_id,
            ),
        )
        await self._conn.commit()

    async def get_stats_rows(self, user_id: int) -> list[dict]:
        """Вся история дневной статистики пользователя, от старых дат к новым."""
        cur = await self._conn.execute(
            "SELECT * FROM daily_stats WHERE user_id = ? ORDER BY date ASC",
            (user_id,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ---------- friend-add flow (/add_friend) ----------

    async def set_awaiting_friend_code(self, user_id: int, prompt_message_id: Optional[int]):
        """
        Включает/выключает режим ожидания player_code от игрока (prompt_message_id
        задан -> ждём, None -> сброс). Хранится прямо на users, а не в отдельном
        FSM-сторадже - тем же паттерном, что last_reg_message_id у регистрации.
        """
        await self._conn.execute(
            "UPDATE users SET awaiting_friend_code = ?, friend_prompt_message_id = ? WHERE user_id = ?",
            (1 if prompt_message_id else 0, prompt_message_id, user_id),
        )
        await self._conn.commit()

    async def find_user_by_player_code(self, code: str) -> Optional[dict]:
        """
        player_code (см. profile.player_code) не хранится отдельной колонкой -
        он целиком выводится из registered_at + user_id, поэтому ищем перебором
        по всем зарегистрированным пользователям. При масштабе "закрытый круг
        друзей" (а не публичный сервис на тысячи пользователей) это дёшево и
        не требует денормализации схемы ради одной редкой операции поиска.
        """
        from bot.profile import player_code as _player_code

        cur = await self._conn.execute("SELECT * FROM users WHERE reg_state = 'done'")
        rows = await cur.fetchall()
        for row in rows:
            user = dict(row)
            if _player_code(user) == code:
                return user
        return None

    # ---------- friendships (/friendlist) ----------

    async def find_friendship(self, user_a: int, user_b: int) -> Optional[dict]:
        """Заявка/дружба между этими двумя людьми в ЛЮБОМ направлении, если есть."""
        cur = await self._conn.execute(
            """SELECT * FROM friendships
               WHERE (requester_id = ? AND addressee_id = ?)
                  OR (requester_id = ? AND addressee_id = ?)""",
            (user_a, user_b, user_b, user_a),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def create_friend_request(self, requester_id: int, addressee_id: int) -> int:
        cur = await self._conn.execute(
            """INSERT INTO friendships (requester_id, addressee_id, status, created_at)
               VALUES (?, ?, 'pending', ?)""",
            (requester_id, addressee_id, datetime.utcnow().isoformat()),
        )
        await self._conn.commit()
        return cur.lastrowid

    async def get_friendship(self, friendship_id: int) -> Optional[dict]:
        cur = await self._conn.execute("SELECT * FROM friendships WHERE id = ?", (friendship_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def accept_friendship(self, friendship_id: int):
        await self._conn.execute(
            "UPDATE friendships SET status = 'accepted', responded_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), friendship_id),
        )
        await self._conn.commit()

    async def delete_friendship(self, friendship_id: int):
        await self._conn.execute("DELETE FROM friendships WHERE id = ?", (friendship_id,))
        await self._conn.commit()

    async def get_friend_ids(self, user_id: int) -> list[int]:
        """ID всех ПРИНЯТЫХ друзей - дружба двусторонняя, неважно, кто был инициатором."""
        cur = await self._conn.execute(
            """SELECT requester_id, addressee_id FROM friendships
               WHERE status = 'accepted' AND (requester_id = ? OR addressee_id = ?)""",
            (user_id, user_id),
        )
        rows = await cur.fetchall()
        friend_ids = []
        for row in rows:
            other = row["addressee_id"] if row["requester_id"] == user_id else row["requester_id"]
            friend_ids.append(other)
        return friend_ids

    # ---------- поддержка друга (challenge_cheers) ----------

    async def toggle_cheer(self, challenge_id: int, supporter_id: int) -> bool:
        """
        Тоггл "поддержки" (та же idея, что и у sync_progress_bonus - отметить/снять,
        а не одностороннее добавление). Возвращает True, если поддержка теперь
        ВКЛЮЧЕНА (запись создана), False - если только что снята.
        """
        existing = await self._conn.execute(
            "SELECT id FROM challenge_cheers WHERE challenge_id = ? AND supporter_id = ?",
            (challenge_id, supporter_id),
        )
        row = await existing.fetchone()
        if row:
            await self._conn.execute("DELETE FROM challenge_cheers WHERE id = ?", (row["id"],))
            await self._conn.commit()
            return False

        await self._conn.execute(
            "INSERT INTO challenge_cheers (challenge_id, supporter_id, created_at) VALUES (?, ?, ?)",
            (challenge_id, supporter_id, datetime.utcnow().isoformat()),
        )
        await self._conn.commit()
        return True

    async def get_cheer_supporter_ids(self, challenge_id: int) -> list[int]:
        cur = await self._conn.execute(
            "SELECT supporter_id FROM challenge_cheers WHERE challenge_id = ? ORDER BY id ASC",
            (challenge_id,),
        )
        rows = await cur.fetchall()
        return [r["supporter_id"] for r in rows]


db = Database()
