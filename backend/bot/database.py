"""
Слой доступа к данным. Файл базы — обычный SQLite (db.sql), как и просил автор идеи.
Все операции асинхронные (aiosqlite), чтобы не блокировать event loop бота.
"""
import json
from datetime import datetime, timedelta
from typing import Any, Optional

import aiosqlite

from bot.config import FOCUS_OPTIONS, settings

# Статусы испытания, считающиеся провалом (штрафные).
FAILED_STATUSES = ("expired", "gave_up")

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
    consecutive_failures INTEGER DEFAULT 0, -- проваленных испытаний подряд (см. bot/failure_policy.py)
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
    failure_message_id   INTEGER,        -- id последнего сообщения-отчёта о провале (удаляется при новом квесте)
    guide_sent           INTEGER DEFAULT 0  -- PDF-гайд уже отправлен при регистрации (см. bot/guide.py)
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
    warned_minutes  INTEGER,  -- наименьший порог (мин до провала), о котором игрока уже предупредили (60/30/15)
    warning_message_id INTEGER, -- id текущего сообщения-предупреждения о скором провале (удаляется при закрытии)
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

-- Журнал ежедневной выдачи квестов (см. bot/dispatch.py): по одной строке на
-- игрока, локальный день (по TZ) и слот. Именно по ней бот понимает, было ли
-- сообщение с квестом реально доставлено, и когда делать следующую попытку.
--   pending     - доставка не удалась, ждёт повторной попытки (next_attempt_at)
--   delivered   - карточка квеста доставлена
--   skipped     - выдача не требовалась (Система остановлена, штраф, нет дисциплин...)
--   failed      - все попытки исчерпаны, игрок пропущен до следующего дня
--   unreachable - бот заблокирован игроком / чат недоступен, повторять бессмысленно
CREATE TABLE IF NOT EXISTS dispatch_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    day             TEXT NOT NULL,       -- YYYY-MM-DD, локальная дата слота по TZ
    slot            TEXT NOT NULL,       -- morning / day / evening / night
    status          TEXT NOT NULL,
    reason          TEXT,
    attempts        INTEGER DEFAULT 0,   -- сколько раз реально пытались отправить карточку
    last_attempt_at TEXT,                -- UTC
    next_attempt_at TEXT,                -- UTC, только для pending
    last_error      TEXT,
    created_at      TEXT,
    updated_at      TEXT,
    UNIQUE(user_id, day, slot)
);
CREATE INDEX IF NOT EXISTS idx_dispatch_log_pending ON dispatch_log(status, next_attempt_at);

-- Какие слоты (день + слот) диспетчер уже ОБОШЁЛ целиком. Если запись отсутствует
-- спустя пару минут после слота - значит cron не сработал (бот был выключен,
-- перезапускался, процесс убили посреди рассылки), и reconcile досылает квесты
-- (см. dispatch.catch_up_missed_dispatches).
CREATE TABLE IF NOT EXISTS dispatch_runs (
    day             TEXT NOT NULL,
    slot            TEXT NOT NULL,
    finished_at     TEXT,
    PRIMARY KEY (day, slot)
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
        await self.prune_dispatch_log(settings.dispatch_log_retention_days)

    async def _migrate(self):
        """Добавляет новые колонки в уже существующие БД, не трогая данные."""
        cur = await self._conn.execute("PRAGMA table_info(users)")
        existing_users = {row[1] for row in await cur.fetchall()}

        new_user_columns = {
            "first_name": "TEXT",
            "last_reg_message_id": "INTEGER",
            "profile_chat_id": "INTEGER",
            "profile_message_id": "INTEGER",
            "profile_created_at": "TEXT",  # когда текущее сообщение профиля было отправлено (см. refresh_stale_profiles)
            "failure_chat_id": "INTEGER",
            "failure_message_id": "INTEGER",
            "failure_sent_at": "TEXT",  # когда ушёл текущий пуш о провале (см. cleanup_failure_messages)
            "awaiting_friend_code": "INTEGER DEFAULT 0",
            "friend_prompt_message_id": "INTEGER",
            "daily_pullups": "INTEGER DEFAULT 0",
            "daily_running": "INTEGER DEFAULT 0",
            "is_stopped": "INTEGER DEFAULT 0",  # /stop - Система приостановлена, дневные квесты не выдаются
            "consecutive_failures": "INTEGER DEFAULT 0",  # проваленных испытаний подряд (автостоп)
            "guide_sent": "INTEGER DEFAULT 0",  # PDF-гайд уже отправлен при регистрации (см. bot/guide.py)
        }
        changed = False
        backfill_failures = "consecutive_failures" not in existing_users
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
            "warned_minutes": "INTEGER",
            "warning_message_id": "INTEGER",
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

        # Колонка consecutive_failures только что добавлена в существующую БД -
        # восстанавливаем счётчик по истории, чтобы уже накопленный провал не
        # "забывался" после обновления.
        if backfill_failures:
            await self._backfill_consecutive_failures()

    async def _backfill_consecutive_failures(self):
        cur = await self._conn.execute("SELECT user_id FROM users")
        user_ids = [row[0] for row in await cur.fetchall()]
        for user_id in user_ids:
            cur = await self._conn.execute(
                "SELECT status FROM challenges WHERE user_id = ? ORDER BY id DESC", (user_id,)
            )
            count = 0
            for (status,) in await cur.fetchall():
                if status == "awaiting_action":
                    continue  # ещё не закрыто - ни провал, ни успех
                if status in FAILED_STATUSES:
                    count += 1
                else:
                    break
            if count:
                await self._conn.execute(
                    "UPDATE users SET consecutive_failures = ? WHERE user_id = ?", (count, user_id)
                )
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
        # OR IGNORE: два почти одновременных первых /start (двойное нажатие) оба не находят
        # игрока и оба вставляют строку - второй INSERT иначе падал бы на PRIMARY KEY.
        await self._conn.execute(
            """INSERT OR IGNORE INTO users (user_id, username, first_name, registered_at, reg_state)
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

    async def claim_guide(self, user_id: int) -> bool:
        """
        Атомарно "занимает" право отправить PDF-гайд при регистрации: True, если
        гайд этому игроку ещё не отправлялся. Условный UPDATE защищает от дубля при
        двух быстрых /start подряд (апдейты обрабатываются параллельно).
        """
        cur = await self._conn.execute(
            "UPDATE users SET guide_sent = 1 WHERE user_id = ? AND COALESCE(guide_sent, 0) = 0",
            (user_id,),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def release_guide(self, user_id: int):
        """Возвращает право на отправку гайда (если отправка не удалась - попробуем при следующем /start)."""
        await self._conn.execute("UPDATE users SET guide_sent = 0 WHERE user_id = ?", (user_id,))
        await self._conn.commit()

    async def set_profile_message(self, user_id: int, chat_id: int, message_id: int):
        await self._conn.execute(
            "UPDATE users SET profile_chat_id = ?, profile_message_id = ?, profile_created_at = ? "
            "WHERE user_id = ?",
            (chat_id, message_id, datetime.utcnow().isoformat(), user_id),
        )
        await self._conn.commit()

    async def set_failure_message(self, user_id: int, chat_id: int, message_id: int):
        await self._conn.execute(
            "UPDATE users SET failure_chat_id = ?, failure_message_id = ?, failure_sent_at = ? "
            "WHERE user_id = ?",
            (chat_id, message_id, datetime.utcnow().isoformat(), user_id),
        )
        await self._conn.commit()

    async def clear_failure_message(self, user_id: int):
        await self._conn.execute(
            "UPDATE users SET failure_chat_id = NULL, failure_message_id = NULL, failure_sent_at = NULL "
            "WHERE user_id = ?",
            (user_id,),
        )
        await self._conn.commit()

    async def get_users_with_expired_failure_message(self, ttl_minutes: int) -> list[dict]:
        """
        Пользователи, у которых пуш-уведомление о провале испытания висит уже
        дольше settings.failure_message_ttl_minutes - его пора удалить, чтобы
        чат оставался чистым (см. scheduler.cleanup_failure_messages). Отдельно
        от profile.clear_failure_message, которая удаляет его же, но раньше -
        как только выдан новый ежедневный квест.
        """
        cutoff = (datetime.utcnow() - timedelta(minutes=ttl_minutes)).isoformat()
        cur = await self._conn.execute(
            "SELECT * FROM users WHERE failure_message_id IS NOT NULL "
            "AND failure_sent_at IS NOT NULL AND failure_sent_at <= ?",
            (cutoff,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

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
        # is_stopped=1 - игрок остановил Систему через /stop: пока он не введёт
        # /start заново, ежедневные испытания ему не создаются и не рассылаются
        # (см. handlers/start.py::cmd_stop / cmd_start).
        cur = await self._conn.execute(
            """SELECT * FROM users WHERE time_of_day = ? AND reg_state = 'done'
               AND (is_stopped IS NULL OR is_stopped = 0)""",
            (time_of_day,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def set_stopped(self, user_id: int, stopped: bool):
        """Ставит/снимает флаг остановки Системы (см. get_users_by_time)."""
        await self._conn.execute(
            "UPDATE users SET is_stopped = ? WHERE user_id = ?", (int(stopped), user_id)
        )
        await self._conn.commit()

    async def register_failure(self, user_id: int) -> int:
        """Атомарно +1 к счётчику проваленных испытаний подряд; возвращает новое значение."""
        await self._conn.execute(
            "UPDATE users SET consecutive_failures = COALESCE(consecutive_failures, 0) + 1 "
            "WHERE user_id = ?",
            (user_id,),
        )
        await self._conn.commit()
        user = await self.get_user(user_id)
        return int((user or {}).get("consecutive_failures") or 0)

    async def reset_failures(self, user_id: int):
        """Обнуляет счётчик провалов подряд (испытание выполнено / Система возобновлена после автостопа)."""
        await self._conn.execute(
            "UPDATE users SET consecutive_failures = 0 WHERE user_id = ?", (user_id,)
        )
        await self._conn.commit()

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

    @staticmethod
    def is_penalty_active(user: Optional[dict]) -> bool:
        """
        True, пока у игрока действует ограничение за провал (penalty_until в
        будущем). Пока это так, ежедневные испытания ему не выдаются (см.
        scheduler.dispatch_daily_challenges). Некорректное/пустое значение
        трактуется как "ограничения нет" - лучше выдать квест, чем навсегда
        заблокировать игрока из-за испорченной строки в БД.
        """
        raw = (user or {}).get("penalty_until")
        if not raw:
            return False
        try:
            return datetime.fromisoformat(raw) > datetime.utcnow()
        except (TypeError, ValueError):
            return False

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

    async def complete_challenge_if_ready(self, challenge_id: int) -> bool:
        """
        Атомарно закрывает испытание как выполненное, но ТОЛЬКО если оно всё ещё
        awaiting_action И все цели дня закрыты (есть хотя бы одна строка прогресса и
        ни одной с completed = 0). Проверка условий и смена статуса - один UPDATE,
        поэтому гонки невозможны: ручное "Завершить", авто-завершение по таймауту,
        "Сдаться" и просрочка не могут закрыть одно и то же испытание дважды, а
        прогресс, откаченный отрицательным числом за миг до этого, не будет
        засчитан. Статус - completed_with_photo, если фото уже ушло в группу.
        True - именно этот вызов закрыл испытание (и только он должен начислять награду).
        """
        cur = await self._conn.execute(
            """UPDATE challenges
               SET status = CASE WHEN physical_photo_posted = 1
                                 THEN 'completed_with_photo' ELSE 'completed' END,
                   active_focus = NULL,
                   completed_at = ?
               WHERE id = ? AND status = 'awaiting_action'
                 AND EXISTS (SELECT 1 FROM challenge_progress WHERE challenge_id = ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM challenge_progress WHERE challenge_id = ? AND completed = 0
                 )""",
            (datetime.utcnow().isoformat(), challenge_id, challenge_id, challenge_id),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def give_up_challenge(self, challenge_id: int) -> bool:
        """
        Закрывает испытание как 'gave_up', но ТОЛЬКО если оно всё ещё
        awaiting_action (защита от двойного нажатия "Да" и от гонки с
        планировщиком, который в ту же секунду мог его просрочить - иначе
        штраф и счётчик провалов засчитались бы дважды). True - статус сменился.
        """
        cur = await self._conn.execute(
            "UPDATE challenges SET status = 'gave_up', active_focus = NULL, completed_at = ? "
            "WHERE id = ? AND status = 'awaiting_action'",
            (datetime.utcnow().isoformat(), challenge_id),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def stop_challenge(self, challenge_id: int):
        """
        Закрывает испытание дня из-за остановки Системы командой /stop.

        В отличие от give_up_challenge/expire_challenge, это НЕ штрафной статус -
        игрок сам поставил Систему на паузу, а не провалил требование, поэтому
        никакой отдельный статус-репорт по нему не шлётся (карточка просто
        удаляется из чата, см. handlers/start.py::on_stop_system_confirmed) и
        XP/стрик этим статусом не трогаются.
        """
        await self._conn.execute(
            "UPDATE challenges SET status = 'stopped', active_focus = NULL, completed_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), challenge_id),
        )
        await self._conn.commit()

    async def get_awaiting_challenges(self) -> list[dict]:
        """Все испытания, которые сейчас активны (карточка ждёт действий игрока)."""
        # message_id IS NOT NULL: карточка, которая так и не дошла до игрока
        # (см. bot/dispatch.py), не должна ни обновляться таймером, ни слать
        # предупреждения о провале.
        cur = await self._conn.execute(
            "SELECT * FROM challenges WHERE status = 'awaiting_action' AND message_id IS NOT NULL"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_expirable_challenges(self, timeout_hours: int) -> list[dict]:
        """
        Активные испытания, у которых вышел таймаут. Только ДОСТАВЛЕННЫЕ
        (message_id задан): если карточка не дошла до игрока из-за сбоя
        отправки, то игрок квеста не видел и штрафовать его нельзя - такими
        занимается bot/dispatch.py (повторные попытки или удаление).
        """
        cutoff = (datetime.utcnow() - timedelta(hours=timeout_hours)).isoformat()
        cur = await self._conn.execute(
            """SELECT * FROM challenges
               WHERE status = 'awaiting_action'
               AND message_id IS NOT NULL
               AND started_at < ?""",
            (cutoff,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def expire_challenge(self, challenge_id: int) -> bool:
        """
        Помечает испытание просроченным, но ТОЛЬКО если оно всё ещё
        awaiting_action. Возвращает True, если статус действительно сменился.

        Условие нужно из-за гонки: пока планировщик разбирает список просроченных,
        игрок мог в эту же секунду нажать "Завершить"/"Сдаться" - без проверки
        мы перезаписали бы уже закрытое испытание статусом expired, сняли XP и
        обнулили стрик у игрока, который успел вовремя.
        """
        cur = await self._conn.execute(
            "UPDATE challenges SET status = 'expired', active_focus = NULL "
            "WHERE id = ? AND status = 'awaiting_action'",
            (challenge_id,),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def restart_challenge_timer(self, challenge_id: int) -> bool:
        """
        Перезапускает отсчёт таймаута (started_at = сейчас) для ещё НЕ
        доставленного испытания (message_id пуст). Нужно, чтобы игрок получал
        полные CHALLENGE_TIMEOUT_HOURS с момента, когда карточка реально до него
        дошла, а не с момента создания строки в БД (при повторных попытках
        это может быть на несколько часов раньше).
        """
        cur = await self._conn.execute(
            "UPDATE challenges SET started_at = ? "
            "WHERE id = ? AND status = 'awaiting_action' AND message_id IS NULL",
            (datetime.utcnow().isoformat(), challenge_id),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def delete_undelivered_challenge(self, challenge_id: int) -> bool:
        """
        Полностью удаляет испытание, которое так и не было доставлено игроку
        (awaiting_action и message_id пуст), вместе со строками прогресса.
        Условие в UPDATE/DELETE защищает от гонки: доставленную карточку или
        уже закрытое испытание эта операция не тронет.
        """
        cur = await self._conn.execute(
            "DELETE FROM challenges "
            "WHERE id = ? AND status = 'awaiting_action' AND message_id IS NULL",
            (challenge_id,),
        )
        deleted = cur.rowcount > 0
        if deleted:
            await self._conn.execute(
                "DELETE FROM challenge_progress WHERE challenge_id = ?", (challenge_id,)
            )
            await self._conn.execute(
                "DELETE FROM challenge_cheers WHERE challenge_id = ?", (challenge_id,)
            )
        await self._conn.commit()
        return deleted

    async def has_challenge_since(self, user_id: int, since_utc_iso: str) -> bool:
        """Было ли у игрока испытание, созданное не раньше указанного момента (UTC)."""
        cur = await self._conn.execute(
            "SELECT 1 FROM challenges WHERE user_id = ? AND started_at >= ? LIMIT 1",
            (user_id, since_utc_iso),
        )
        return (await cur.fetchone()) is not None

    # ---------- журнал ежедневной выдачи (bot/dispatch.py) ----------

    async def get_users_in_slot(self, time_of_day: str) -> list[dict]:
        """
        ВСЕ зарегистрированные игроки слота, включая остановленных (/stop или
        автостоп): для них диспетчер пишет в журнал запись "skipped", чтобы
        возобновление Системы посреди дня не приводило к запоздалой выдаче.
        """
        cur = await self._conn.execute(
            "SELECT * FROM users WHERE time_of_day = ? AND reg_state = 'done'", (time_of_day,)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def is_slot_run_finished(self, day: str, slot: str) -> bool:
        cur = await self._conn.execute(
            "SELECT 1 FROM dispatch_runs WHERE day = ? AND slot = ? AND finished_at IS NOT NULL",
            (day, slot),
        )
        return (await cur.fetchone()) is not None

    async def mark_slot_run_finished(self, day: str, slot: str):
        await self._conn.execute(
            "INSERT INTO dispatch_runs (day, slot, finished_at) VALUES (?, ?, ?) "
            "ON CONFLICT(day, slot) DO UPDATE SET finished_at = excluded.finished_at",
            (day, slot, datetime.utcnow().isoformat()),
        )
        await self._conn.commit()

    async def get_dispatch(self, user_id: int, day: str, slot: str) -> Optional[dict]:
        cur = await self._conn.execute(
            "SELECT * FROM dispatch_log WHERE user_id = ? AND day = ? AND slot = ?",
            (user_id, day, slot),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def save_dispatch(
        self,
        user_id: int,
        day: str,
        slot: str,
        status: str,
        *,
        reason: Optional[str] = None,
        attempts: int = 0,
        last_attempt_at: Optional[str] = None,
        next_attempt_at: Optional[str] = None,
        last_error: Optional[str] = None,
    ):
        """Создаёт или перезаписывает запись журнала (одна на игрока/день/слот)."""
        now = datetime.utcnow().isoformat()
        await self._conn.execute(
            """INSERT INTO dispatch_log
                   (user_id, day, slot, status, reason, attempts, last_attempt_at,
                    next_attempt_at, last_error, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, day, slot) DO UPDATE SET
                   status = excluded.status,
                   reason = excluded.reason,
                   attempts = excluded.attempts,
                   last_attempt_at = excluded.last_attempt_at,
                   next_attempt_at = excluded.next_attempt_at,
                   last_error = excluded.last_error,
                   updated_at = excluded.updated_at""",
            (
                user_id, day, slot, status, reason, attempts, last_attempt_at,
                next_attempt_at, (last_error or "")[:300] or None, now, now,
            ),
        )
        await self._conn.commit()

    async def get_due_pending_dispatches(self, now_utc_iso: str) -> list[dict]:
        """Записи в статусе pending, у которых подошло время повторной попытки."""
        cur = await self._conn.execute(
            "SELECT * FROM dispatch_log WHERE status = 'pending' "
            "AND next_attempt_at IS NOT NULL AND next_attempt_at <= ? ORDER BY next_attempt_at ASC",
            (now_utc_iso,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def prune_dispatch_log(self, keep_days: int):
        cutoff = (datetime.utcnow() - timedelta(days=keep_days)).isoformat()
        await self._conn.execute("DELETE FROM dispatch_log WHERE updated_at < ?", (cutoff,))
        await self._conn.execute("DELETE FROM dispatch_runs WHERE finished_at < ?", (cutoff,))
        await self._conn.commit()

    # ---------- предупреждения о скором провале ----------

    async def claim_warning_level(self, challenge_id: int, minutes: int) -> bool:
        """
        Атомарно "занимает" порог предупреждения (60/30/15 минут): True, если
        этот порог ещё не отправлялся (warned_minutes пуст или больше minutes)
        и испытание всё ещё активно. Значение только уменьшается, поэтому
        повторный запуск джобы (или перезапуск бота) никогда не пришлёт одно
        и то же предупреждение дважды и не вернётся к более раннему порогу.
        """
        cur = await self._conn.execute(
            "UPDATE challenges SET warned_minutes = ? "
            "WHERE id = ? AND status = 'awaiting_action' "
            "AND (warned_minutes IS NULL OR warned_minutes > ?)",
            (minutes, challenge_id, minutes),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def set_warning_message(self, challenge_id: int, message_id: Optional[int]):
        await self._conn.execute(
            "UPDATE challenges SET warning_message_id = ? WHERE id = ?", (message_id, challenge_id)
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
