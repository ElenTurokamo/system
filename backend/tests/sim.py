"""
Стенд симуляции: фальшивый Telegram-бот с управляемыми сбоями, подменяемые часы и
"минутный тикер", который гоняет те же задачи, что и реальный планировщик.
"""
import asyncio
import dataclasses
import sys
import tempfile
import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)

REAL_DATETIME = datetime


class Clock:
    now = datetime(2026, 10, 22, 7, 59, 0)  # 12:59 по Алматы


class FakeDT(REAL_DATETIME):
    @classmethod
    def utcnow(cls):
        return Clock.now


def _method():
    return MagicMock()


def forbidden():
    return TelegramForbiddenError(method=_method(), message="Forbidden: bot was blocked by the user")


def net_error():
    return TelegramNetworkError(method=_method(), message="Request timeout error")


def bad_chat():
    return TelegramBadRequest(method=_method(), message="Bad Request: chat not found")


def retry_after(sec=1):
    return TelegramRetryAfter(method=_method(), message="Flood control", retry_after=sec)


class _Doc:
    def __init__(self, file_id):
        self.file_id = file_id


class Msg:
    def __init__(self, message_id, document=None):
        self.message_id = message_id
        self.document = document


class FakeBot:
    """Запоминает вызовы; сбоями управляют hooks и blocked."""

    def __init__(self):
        self._id = 1000
        self.blocked: set[int] = set()
        self.hooks = []
        self.log = []           # (method, chat_id, kwargs)
        self.alive = {}         # (chat_id, message_id) -> text   (не удалённые сообщения)
        self.card_attempts = [] # (chat_id, ok:bool)  - попытки отправки карточки квеста
        self.card_ids = []      # (chat_id, message_id) - успешно отправленные карточки
        self.card_log = []      # (chat_id, message_id, utc_now)
        self.on_card_sent = None  # async-колбэк(chat_id): проверки инвариантов в момент отправки

    def _check(self, method, chat_id, kw):
        if chat_id in self.blocked:
            raise forbidden()
        for hook in list(self.hooks):
            hook(method, chat_id, kw)

    async def send_message(self, chat_id, text, **kw):
        is_card = kw.get("reply_markup") is not None
        try:
            self._check("send_message", chat_id, kw)
        except Exception:
            if is_card:
                self.card_attempts.append((chat_id, False))
            raise
        self._id += 1
        self.alive[(chat_id, self._id)] = text
        self.log.append(("send_message", chat_id, {"text": text, **kw}))
        if is_card:
            self.card_attempts.append((chat_id, True))
            self.card_ids.append((chat_id, self._id))
            self.card_log.append((chat_id, self._id, Clock.now))
            if self.on_card_sent:
                await self.on_card_sent(chat_id)
        return Msg(self._id)

    async def send_document(self, chat_id, document, caption=None, **kw):
        """Документ (PDF-гайд). Хранит, ЧТО именно передали: str - это file_id, иначе файл с диска."""
        self._check("send_document", chat_id, {"document": document, **kw})
        self._id += 1
        self.alive[(chat_id, self._id)] = caption or ""
        self.log.append(("send_document", chat_id, {"document": document, "caption": caption, **kw}))
        return Msg(self._id, document=_Doc("FAKE_FILE_ID"))

    async def send_photo(self, chat_id, photo, caption=None, **kw):
        self._check("send_photo", chat_id, {"photo": photo, **kw})
        self._id += 1
        self.alive[(chat_id, self._id)] = caption or ""
        self.log.append(("send_photo", chat_id, {"photo": photo, "caption": caption, **kw}))
        return Msg(self._id)

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        self._check("edit_message_text", chat_id, kw)
        self.log.append(("edit_message_text", chat_id, {"text": text, "message_id": message_id}))
        if (chat_id, message_id) in self.alive:
            self.alive[(chat_id, message_id)] = text
        return Msg(message_id)

    async def delete_message(self, chat_id, message_id, **kw):
        self._check("delete_message", chat_id, kw)
        self.log.append(("delete_message", chat_id, {"message_id": message_id}))
        self.alive.pop((chat_id, message_id), None)
        return True

    async def pin_chat_message(self, chat_id, message_id, **kw):
        self._check("pin_chat_message", chat_id, kw)
        return True

    async def unpin_all_chat_messages(self, chat_id, **kw):
        self._check("unpin_all_chat_messages", chat_id, kw)
        return True

    async def unpin_chat_message(self, chat_id, message_id=None, **kw):
        self._check("unpin_chat_message", chat_id, kw)
        return True

    # --- удобные запросы для проверок ---
    def cards_alive(self, chat_id):
        return [k for k in self.card_ids if k in self.alive and k[0] == chat_id]

    def sent(self, method, chat_id):
        """Все вызовы метода в чат: список kwargs (для send_message там есть 'text')."""
        return [kw for (m, cid, kw) in self.log if m == method and cid == chat_id]

    def card_attempt_count(self, chat_id):
        return len([a for a in self.card_attempts if a[0] == chat_id])


def fail_cards(bot, chat_id, exc_factory, times=None):
    """Карточки квеста (send_message с клавиатурой) для chat_id падают `times` раз (None - всегда)."""
    state = {"left": times}

    def hook(method, cid, kw):
        if method != "send_message" or cid != chat_id or kw.get("reply_markup") is None:
            return
        if state["left"] is None:
            raise exc_factory()
        if state["left"] > 0:
            state["left"] -= 1
            raise exc_factory()

    bot.hooks.append(hook)
    return hook


class Sim:
    """Минутный тикер: имитирует APScheduler (cron слотов + периодические задачи)."""

    def __init__(self, db, bot, settings):
        self.db, self.bot, self.settings = db, bot, settings
        self.cron_down = False   # True - "бот выключен": ничего не запускается
        self.cron_only_down = False  # True - cron слотов не срабатывает, остальное работает

    async def make_user(self, uid, slot="day", focuses=("pushups",), registered_days_ago=30, stopped=False):
        db = self.db
        await db.create_user_if_missing(uid, f"u{uid}", f"U{uid}")
        await db.set_time_of_day(uid, slot)
        for f in focuses:
            await db.toggle_focus(uid, f)
        await db.finish_registration(uid)
        reg = (Clock.now - timedelta(days=registered_days_ago)).isoformat()
        await db._conn.execute("UPDATE users SET registered_at = ? WHERE user_id = ?", (reg, uid))
        await db._conn.commit()
        if stopped:
            await db.set_stopped(uid, True)

    async def step(self, minutes=1):
        from bot import dispatch, scheduler
        for _ in range(minutes):
            prev = Clock.now
            Clock.now = prev + timedelta(minutes=1)
            if self.cron_down:
                continue
            if not self.cron_only_down:
                today = dispatch.local_today()
                for slot, hhmm in self.settings.time_slots.items():
                    for day in (today - timedelta(days=1), today):
                        s = dispatch.slot_start_utc(day, hhmm)
                        if prev < s <= Clock.now:
                            await scheduler.dispatch_daily_challenges(self.bot, slot)
            await scheduler.dispatch.reconcile_dispatches(self.bot)
            await scheduler.expire_stale_challenges(self.bot)
            await scheduler.send_failure_warnings(self.bot)
            await scheduler.refresh_challenge_timers(self.bot)

    async def until(self, target: datetime):
        while Clock.now < target:
            await self.step(1)

    async def rec(self, uid, slot="day", day="2026-10-22"):
        return await self.db.get_dispatch(uid, day, slot)

    async def challenges(self, uid):
        cur = await self.db._conn.execute("SELECT * FROM challenges WHERE user_id = ? ORDER BY id", (uid,))
        return [dict(r) for r in await cur.fetchall()]

    async def progress_rows(self):
        cur = await self.db._conn.execute("SELECT COUNT(*) FROM challenge_progress")
        return (await cur.fetchone())[0]


def fresh_modules():
    """Подменяем datetime во всех модулях проекта и сбрасываем межтестовое состояние."""
    for name, mod in list(sys.modules.items()):
        if name.startswith("bot") and getattr(mod, "datetime", None) is REAL_DATETIME:
            mod.datetime = FakeDT
    import bot.dispatch as dispatch
    dispatch.utcnow = lambda: Clock.now
    dispatch._locks.clear()
    dispatch._slot_runs_in_progress.clear()
    import bot.profile as profile
    profile._profile_locks.clear()
    import bot.guide as guide
    guide._file_id = None
    import bot.utils as utils
    utils._command_messages.clear()
    for task in list(utils._delete_tasks.values()):
        task.cancel()
    utils._delete_tasks.clear()
    import bot.challenge_render as cr
    cr._giveup_confirm_until.clear()
    cr._flood_until.clear()


async def run_scenario(fn, **setting_overrides):
    """Поднимает чистую БД, выполняет сценарий fn(sim), закрывает БД, возвращает настройки назад."""
    import bot.database as database
    from bot.config import settings
    import bot.utils

    fresh_modules()
    saved = dataclasses.asdict(settings)
    for k, v in setting_overrides.items():
        setattr(settings, k, v)
    settings.profile_retry_delay_seconds = 0.0
    settings.profile_edit_retry_interval_seconds = 0.0

    real_sleep = asyncio.sleep

    async def fast_sleep(delay, *a, **k):
        await real_sleep(0)

    asyncio.sleep = fast_sleep

    db = database.db
    db.path = os.path.join(tempfile.mkdtemp(), "t.sql")
    Clock.now = datetime(2026, 10, 22, 7, 59, 0)
    await db.connect()
    sim = Sim(db, FakeBot(), settings)
    try:
        return await fn(sim)
    finally:
        asyncio.sleep = real_sleep
        await db.close()
        for k, v in saved.items():
            setattr(settings, k, v)


def run(fn, **overrides):
    return asyncio.run(run_scenario(fn, **overrides))
