"""
Симуляции доработок:
  1) авто-завершение испытания по таймауту, если все цели закрыты (bot/completion.py);
  2) PDF-гайд в /help и при регистрации (bot/guide.py);
  3) укороченные разделители.
Слот 'day' = 13:00 Алматы = 08:00 UTC; карточка приходит в 08:00 UTC, дедлайн - 02:00 UTC
следующих суток. Запуск:  cd backend && python -m pytest tests -q
"""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from tests import sim
from tests.sim import net_error, forbidden

D22 = "2026-10-22"
D23 = "2026-10-23"
GROUP = -100500


def t(h, m=0, day=22):
    return datetime(2026, 10, day, h, m)


DEADLINE = t(2, 5, day=23)  # через 18 ч после выдачи в 08:00 UTC + запас на минутный тик


# ---------------------------------------------------------------- помощники


def _call(uid, bot, data):
    call = MagicMock()
    call.data = data
    call.from_user.id = uid
    call.bot = bot
    call.message.chat.id = uid
    call.message.message_id = 4242
    call.message.delete = AsyncMock()
    call.message.edit_reply_markup = AsyncMock()
    call.message.edit_text = AsyncMock()
    call.answer = AsyncMock()
    return call


def _message(uid, bot=None, text=None):
    m = MagicMock()
    m.from_user.id = uid
    m.from_user.username = f"u{uid}"
    m.from_user.first_name = f"U{uid}"
    m.chat.id = uid
    m.text = text
    m.bot = bot
    m.delete = AsyncMock()
    return m


async def setup_quest(s, uid=1, focuses=("pushups",), group=GROUP):
    """Игрок с выданной карточкой (08:05 UTC) и, при желании, привязанной группой."""
    await s.make_user(uid, focuses=focuses)
    if group:
        await s.db.set_group(uid, group)
    await s.until(t(8, 5))
    ch = await s.db.get_active_challenge(uid)
    assert ch and ch["message_id"], "карточка не выдана"
    return ch


async def setup_quests(s, specs):
    """Несколько игроков, созданных ДО слота: specs = [(uid, focuses, group), ...]. Возвращает {uid: challenge}."""
    for uid, focuses, group in specs:
        await s.make_user(uid, focuses=focuses)
        if group:
            await s.db.set_group(uid, group)
    await s.until(t(8, 5))
    out = {}
    for uid, _, _ in specs:
        ch = await s.db.get_active_challenge(uid)
        assert ch and ch["message_id"], f"карточка {uid} не выдана"
        out[uid] = ch
    return out


async def log(s, uid, ch_id, focus, amount):
    """Ввод подхода реальными хендлерами: выбор дисциплины + число."""
    from bot.handlers import challenge as h
    await h.on_focus_picked(_call(uid, s.bot, f"focus:{ch_id}:{focus}"))
    await h.on_amount_logged(_message(uid, s.bot, text=str(amount)))


async def status_of(s, ch_id):
    return (await s.db.get_challenge(ch_id))["status"]


async def profile_text(s, uid):
    u = await s.db.get_user(uid)
    return s.bot.alive[(u["profile_chat_id"], u["profile_message_id"])]


def group_texts(s, group=GROUP):
    return [kw["text"] for kw in s.bot.sent("send_message", group)]


def failure_pushes(s, uid):
    return [kw["text"] for kw in s.bot.sent("send_message", uid) if "ПРОВАЛЕН" in kw["text"]]


# ---------------------------------------------------------------- авто-завершение: основной сценарий


def test_all_goals_done_but_not_finished_is_completed_not_failed():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", 10)
        assert await status_of(s, ch["id"]) == "awaiting_action"

        # до дедлайна (17:55 с момента выдачи) ничего не происходит
        await s.until(t(1, 55, day=23))
        assert await status_of(s, ch["id"]) == "awaiting_action"
        assert group_texts(s) == []

        await s.until(DEADLINE)
        assert await status_of(s, ch["id"]) == "completed"
        u = await s.db.get_user(1)
        assert (u["xp"], u["level"], u["streak"]) == (100, 1, 1)
        assert u["penalty_until"] is None and u["consecutive_failures"] == 0 and not u["is_stopped"]
        assert failure_pushes(s, 1) == []

        # в группу ушёл ровно один текстовый отчёт, без фото
        assert group_texts(s) == ["Отчёт дня 1. Игрок 1 завершил испытание."]
        assert s.bot.sent("send_photo", GROUP) == [] and s.bot.sent("send_document", GROUP) == []

        # карточка убрана, статистика записана, профиль показывает выполненный квест
        assert not s.bot.cards_alive(1)
        rows = await s.db._conn.execute_fetchall("SELECT pushups FROM daily_stats WHERE user_id = 1")
        assert [r[0] for r in rows] == [10]
        p = await profile_text(s, 1)
        assert "✅ Вы выполнили сегодняшний квест." in p
        assert "секретное" not in p
        assert "🔥 Серия: 1 день" in p
        assert "Вы не выполнили требование системы" not in p

        # повторные тики ничего не дублируют
        await s.until(t(4, 0, day=23))
        assert len(group_texts(s)) == 1
        assert (await s.db.get_user(1))["xp"] == 100
    sim.run(sc)


def test_secret_quest_is_shown_in_profile_and_group_report():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", 20)  # x2 цели: секретный бонус (+300 XP сразу)
        await s.until(DEADLINE)
        assert await status_of(s, ch["id"]) == "completed"
        assert (await s.db.get_user(1))["xp"] == 400  # 300 секретный бонус + 100 за испытание
        p = await profile_text(s, 1)
        assert "✅ Вы выполнили сегодняшний квест." in p
        assert "✅ Вы завершили секретное испытание." in p
        assert group_texts(s) == [
            "Отчёт дня 1. Игрок 1 завершил испытание.\n\n🌟 В день 1 также пройдено секретное испытание."
        ]
    sim.run(sc)


def test_streak_continues_and_report_day_counts_the_just_completed_day():
    async def sc(s):
        ch = await setup_quest(s)
        await s.db._conn.execute("UPDATE users SET streak = 4 WHERE user_id = 1")
        await s.db._conn.commit()
        await log(s, 1, ch["id"], "pushups", 10)
        await s.until(DEADLINE)
        assert (await s.db.get_user(1))["streak"] == 5
        assert group_texts(s) == ["Отчёт дня 5. Игрок 1 завершил испытание."]
        assert "🔥 Серия: 5 дней" in await profile_text(s, 1)
    sim.run(sc)


def test_auto_finish_resets_failure_counter_and_levels_up():
    async def sc(s):
        ch = await setup_quest(s)
        await s.db._conn.execute("UPDATE users SET consecutive_failures = 1, xp = 950 WHERE user_id = 1")
        await s.db._conn.commit()
        await log(s, 1, ch["id"], "pushups", 10)
        await s.until(DEADLINE)
        u = await s.db.get_user(1)
        assert u["consecutive_failures"] == 0 and not u["is_stopped"]   # автостоп не сработал
        assert (u["xp"], u["level"]) == (1050, 2)
        assert "🏆 Уровень: 2 (50/1120 XP)" in await profile_text(s, 1)
    sim.run(sc)


def test_next_day_quest_arrives_normally_after_auto_finish():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", 10)
        await s.until(DEADLINE)
        assert "✅ Вы выполнили сегодняшний квест." in await profile_text(s, 1)
        await s.until(t(8, 5, day=23))
        rec = await s.rec(1, day=D23)
        assert rec["status"] == "delivered"
        new = await s.db.get_active_challenge(1)
        assert new and new["id"] != ch["id"]
        assert "✅ Вы выполнили сегодняшний квест." not in await profile_text(s, 1)
    sim.run(sc)


# ---------------------------------------------------------------- когда авто-завершения быть не должно


def test_unfinished_goals_still_fail_exactly_as_before():
    async def sc(s):
        ch = await setup_quest(s, focuses=("pushups", "chess"))
        await s.db._conn.execute("UPDATE users SET xp = 500, streak = 3 WHERE user_id = 1")
        await s.db._conn.commit()
        await log(s, 1, ch["id"], "pushups", 10)       # шахматы не сыграны
        await s.until(DEADLINE)
        assert await status_of(s, ch["id"]) == "expired"
        u = await s.db.get_user(1)
        assert (u["xp"], u["streak"], u["consecutive_failures"]) == (200, 0, 1)
        assert u["penalty_until"] is not None
        assert len(failure_pushes(s, 1)) == 1
        assert group_texts(s) == []                      # провал в группу не шлётся
        assert "✅ Вы выполнили сегодняшний квест." not in await profile_text(s, 1)
    sim.run(sc)


def test_nine_of_ten_fails_and_rollback_below_target_fails():
    async def sc(s):
        chs = await setup_quests(s, [(1, ("pushups",), GROUP), (2, ("pushups",), GROUP)])
        ch1, ch2 = chs[1], chs[2]
        await log(s, 1, ch1["id"], "pushups", 9)
        await log(s, 2, ch2["id"], "pushups", 10)
        await log(s, 2, ch2["id"], "pushups", -1)      # откатил ниже цели
        await s.until(DEADLINE)
        assert await status_of(s, ch1["id"]) == "expired"
        assert await status_of(s, ch2["id"]) == "expired"
        assert group_texts(s) == []
    sim.run(sc)


def test_challenge_without_progress_rows_is_not_auto_completed():
    async def sc(s):
        ch = await setup_quest(s)
        await s.db._conn.execute("DELETE FROM challenge_progress WHERE challenge_id = ?", (ch["id"],))
        await s.db._conn.commit()
        await s.until(DEADLINE)
        assert await status_of(s, ch["id"]) == "expired"   # пустое испытание не засчитывается "само"
    sim.run(sc)


def test_gave_up_and_stopped_challenges_are_not_touched():
    async def sc(s):
        from bot.handlers import challenge as h
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", 10)
        await h._execute_give_up(s.bot, await s.db.get_challenge(ch["id"]))
        await s.until(DEADLINE)
        assert await status_of(s, ch["id"]) == "gave_up"
        assert group_texts(s) == []
        assert (await s.db.get_user(1))["streak"] == 0
    sim.run(sc)


# ---------------------------------------------------------------- группа и фото


def test_no_group_bound_completes_silently():
    async def sc(s):
        ch = await setup_quest(s, group=None)
        await log(s, 1, ch["id"], "pushups", 10)
        await s.until(DEADLINE)
        assert await status_of(s, ch["id"]) == "completed"
        assert (await s.db.get_user(1))["xp"] == 100
        assert [m for m in s.bot.log if m[0] in ("send_photo", "send_document")] == []
    sim.run(sc)


def test_photo_already_sent_to_group_is_not_duplicated_by_text_report():
    async def sc(s):
        from bot.handlers import challenge as h
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", 10)
        photo = _message(1, s.bot)
        photo.photo = [MagicMock(file_id="PHOTO_ID")]
        await h.on_photo_received(photo, s.bot)                 # реальный хендлер: фото ушло в группу
        assert len(s.bot.sent("send_photo", GROUP)) == 1
        await s.until(DEADLINE)
        assert await status_of(s, ch["id"]) == "completed_with_photo"
        assert group_texts(s) == []                              # второго отчёта нет
        assert (await s.db.get_user(1))["xp"] == 100
        assert "✅ Вы выполнили сегодняшний квест." in await profile_text(s, 1)
    sim.run(sc)


def test_photo_skipped_or_not_sent_gets_text_report():
    async def sc(s):
        from bot.handlers import challenge as h
        chs = await setup_quests(s, [(1, ("pushups",), GROUP), (2, ("pushups",), GROUP)])
        ch1, ch2 = chs[1], chs[2]
        await log(s, 1, ch1["id"], "pushups", 10)
        await log(s, 2, ch2["id"], "pushups", 10)
        await h.on_skip_physical_photo(_call(2, s.bot, f"skipphysicalphoto:{ch2['id']}"))
        await s.until(DEADLINE)
        assert sorted(group_texts(s)) == [
            "Отчёт дня 1. Игрок 1 завершил испытание.",
            "Отчёт дня 1. Игрок 2 завершил испытание.",
        ]
        assert await status_of(s, ch2["id"]) == "completed"
    sim.run(sc)


def test_mental_only_challenge_auto_completes_with_report_and_secret():
    async def sc(s):
        ch = await setup_quest(s, focuses=("chess",))
        await log(s, 1, ch["id"], "chess", 2)                   # x2 цели (цель на 1 уровне - 1 партия)
        await s.until(DEADLINE)
        assert await status_of(s, ch["id"]) == "completed"
        assert group_texts(s) == [
            "Отчёт дня 1. Игрок 1 завершил испытание.\n\n🌟 В день 1 также пройдено секретное испытание."
        ]
        assert "✅ Вы завершили секретное испытание." in await profile_text(s, 1)
    sim.run(sc)


def test_physical_x2_but_mental_not_gives_group_secret_line_but_not_profile_secret():
    """Прежняя семантика фото-отчёта сохранена: секретная строка в группе - по физическим дисциплинам."""
    async def sc(s):
        ch = await setup_quest(s, focuses=("pushups", "chess"))
        await log(s, 1, ch["id"], "pushups", 20)
        await log(s, 1, ch["id"], "chess", 1)
        await s.until(DEADLINE)
        assert await status_of(s, ch["id"]) == "completed"
        assert "🌟" in group_texts(s)[0]
        assert "секретное" not in await profile_text(s, 1)      # профиль - общий бонус, его нет
    sim.run(sc)


def test_group_send_failures_never_break_completion():
    async def sc(s):
        # группа недоступна (бот удалён) - награда и профиль всё равно есть
        def hook(method, cid, kw):
            if method == "send_message" and cid == GROUP:
                raise forbidden()
        s.bot.hooks.append(hook)
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", 10)
        await s.until(DEADLINE)
        assert await status_of(s, ch["id"]) == "completed"
        assert (await s.db.get_user(1))["xp"] == 100
        assert "✅ Вы выполнили сегодняшний квест." in await profile_text(s, 1)
        assert failure_pushes(s, 1) == []
    sim.run(sc)


def test_transient_group_error_is_retried_once_delivered():
    async def sc(s):
        left = {"n": 2}

        def hook(method, cid, kw):
            if method == "send_message" and cid == GROUP and left["n"] > 0:
                left["n"] -= 1
                raise net_error()
        s.bot.hooks.append(hook)
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", 10)
        await s.until(DEADLINE)
        assert group_texts(s) == ["Отчёт дня 1. Игрок 1 завершил испытание."]
    sim.run(sc)


def test_one_users_group_failure_does_not_block_others_in_same_sweep():
    async def sc(s):
        def hook(method, cid, kw):
            if method == "send_message" and cid == GROUP:
                raise forbidden()
        chs = await setup_quests(s, [(1, ("pushups",), GROUP), (2, ("pushups", "chess"), None)])
        ch1, ch2 = chs[1], chs[2]
        await log(s, 1, ch1["id"], "pushups", 10)
        await log(s, 2, ch2["id"], "pushups", 10)               # шахматы не закрыты - провал
        s.bot.hooks.append(hook)
        await s.until(DEADLINE)
        assert await status_of(s, ch1["id"]) == "completed"
        assert await status_of(s, ch2["id"]) == "expired"
    sim.run(sc)


def test_player_blocked_the_bot_after_getting_the_card_still_gets_credit():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", 10)
        s.bot.blocked.add(1)
        await s.until(DEADLINE)
        assert await status_of(s, ch["id"]) == "completed"
        u = await s.db.get_user(1)
        assert (u["xp"], u["streak"]) == (100, 1)
        assert group_texts(s) == ["Отчёт дня 1. Игрок 1 завершил испытание."]
    sim.run(sc)


# ---------------------------------------------------------------- гонки


def test_manual_finish_twice_rewards_once():
    async def sc(s):
        from bot.handlers import challenge as h
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", 10)
        c1, c2 = _call(1, s.bot, f"finish:{ch['id']}"), _call(1, s.bot, f"finish:{ch['id']}")
        await asyncio.gather(h.on_finish_challenge(c1), h.on_finish_challenge(c2))
        u = await s.db.get_user(1)
        assert (u["xp"], u["streak"]) == (100, 1)
        assert await status_of(s, ch["id"]) == "completed"
        assert group_texts(s) == []                              # ручное завершение отчёта без фото не шлёт
        # игрок, нажавший позже, получает алерт, а не вторую награду
        await h.on_finish_challenge(_call(1, s.bot, f"finish:{ch['id']}"))
        assert (await s.db.get_user(1))["xp"] == 100
    sim.run(sc)


def test_manual_finish_not_ready_is_refused_with_alert():
    async def sc(s):
        from bot.handlers import challenge as h
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", 5)
        call = _call(1, s.bot, f"finish:{ch['id']}")
        await h.on_finish_challenge(call)
        call.answer.assert_awaited_once()
        assert call.answer.await_args.kwargs.get("show_alert") is True
        assert await status_of(s, ch["id"]) == "awaiting_action"
        assert (await s.db.get_user(1))["xp"] == 0
    sim.run(sc)


def test_manual_finish_and_auto_finish_at_the_same_moment_reward_once():
    async def sc(s):
        from bot import scheduler
        from bot.handlers import challenge as h
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", 10)
        row = await s.db.get_challenge(ch["id"])
        await asyncio.gather(
            h.on_finish_challenge(_call(1, s.bot, f"finish:{ch['id']}")),
            scheduler._expire_one(s.bot, row),
        )
        u = await s.db.get_user(1)
        assert (u["xp"], u["streak"], u["consecutive_failures"]) == (100, 1, 0)
        assert u["penalty_until"] is None
        assert await status_of(s, ch["id"]) == "completed"
        assert len(group_texts(s)) <= 1
    sim.run(sc)


def test_give_up_and_auto_finish_at_the_same_moment_never_both_apply():
    async def sc(s):
        from bot import scheduler
        from bot.handlers import challenge as h
        ch = await setup_quest(s)
        await s.db._conn.execute("UPDATE users SET xp = 500 WHERE user_id = 1")
        await s.db._conn.commit()
        await log(s, 1, ch["id"], "pushups", 10)
        row = await s.db.get_challenge(ch["id"])
        await asyncio.gather(h._execute_give_up(s.bot, row), scheduler._expire_one(s.bot, row))
        u = await s.db.get_user(1)
        st = await status_of(s, ch["id"])
        if st == "completed":
            assert (u["xp"], u["streak"], u["consecutive_failures"]) == (600, 1, 0) and u["penalty_until"] is None
        else:
            assert st == "gave_up"
            assert (u["xp"], u["streak"], u["consecutive_failures"]) == (200, 0, 1) and u["penalty_until"]
    sim.run(sc)


def test_auto_finish_and_expire_called_twice_concurrently_rewards_once():
    async def sc(s):
        from bot import scheduler
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", 10)
        row = await s.db.get_challenge(ch["id"])
        await asyncio.gather(scheduler._expire_one(s.bot, row), scheduler._expire_one(s.bot, row))
        assert (await s.db.get_user(1))["xp"] == 100
        assert len(group_texts(s)) == 1
    sim.run(sc)


def test_progress_rolled_back_right_before_deadline_is_not_counted():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", 10)
        await s.until(t(1, 59, day=23))
        await log(s, 1, ch["id"], "pushups", -3)                # поправил число
        await s.until(DEADLINE)
        assert await status_of(s, ch["id"]) == "expired"
    sim.run(sc)


def test_warnings_are_still_sent_before_auto_finish_and_cleaned_after():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", 10)
        await s.until(t(1, 20, day=23))
        warnings = [kw["text"] for kw in s.bot.sent("send_message", 1) if "Предупреждение Системы" in kw["text"]]
        assert warnings, "предупреждение-напоминание должно приходить как раньше"
        await s.until(DEADLINE)
        alive_warn = [txt for (cid, _), txt in s.bot.alive.items() if cid == 1 and "Предупреждение Системы" in txt]
        assert alive_warn == []                                  # после завершения предупреждения убраны
    sim.run(sc)


# ---------------------------------------------------------------- гайд: /help


def _stub_schedule_delete(monkeypatch_target):
    """Тестовый asyncio.sleep «мгновенный» - реальное автоудаление сработало бы сразу, поэтому записываем."""
    calls = []
    import bot.utils as utils
    saved = utils.schedule_delete
    utils.schedule_delete = lambda bot, chat_id, message_id, delay=utils.AUTO_DELETE_SECONDS: calls.append(
        (chat_id, message_id, delay)
    )
    return calls, lambda: setattr(utils, "schedule_delete", saved)


def test_help_sends_pdf_with_command_list_in_one_message_and_caches_file_id():
    async def sc(s):
        from bot import guide
        from bot.handlers import misc
        calls, restore = _stub_schedule_delete(None)
        try:
            await s.make_user(1)
            await misc.cmd_help(_message(1), s.bot)
            docs = s.bot.sent("send_document", 1)
            assert len(docs) == 1
            assert docs[0]["caption"] == guide.HELP_TEXT and len(guide.HELP_TEXT) <= 1024
            assert str(docs[0]["document"].path).endswith("guide.pdf")
            assert docs[0]["document"].filename == "Гайд по системе.pdf"
            assert s.bot.sent("send_message", 1) == []           # отдельного текстового сообщения нет
            assert calls and calls[-1][2] == guide.GUIDE_TTL_SECONDS and guide.GUIDE_TTL_SECONDS > 30

            first_id = calls[-1][1]
            await misc.cmd_help(_message(1), s.bot)              # повторный /help
            docs = s.bot.sent("send_document", 1)
            assert len(docs) == 2 and docs[1]["document"] == "FAKE_FILE_ID"   # без повторной загрузки с диска
            assert (1, first_id) not in s.bot.alive             # прежний ответ убран - одно сообщение на команду
            assert len([k for k in s.bot.alive if k[0] == 1 and k[1] != 0]) >= 1
        finally:
            restore()
    sim.run(sc)


def test_help_falls_back_to_text_when_file_missing_or_telegram_fails():
    async def sc(s):
        from pathlib import Path
        from bot import guide
        from bot.handlers import misc
        calls, restore = _stub_schedule_delete(None)
        real_path = guide.GUIDE_PATH
        try:
            await s.make_user(1)
            guide.GUIDE_PATH = Path("/nonexistent/guide.pdf")
            await misc.cmd_help(_message(1), s.bot)
            assert [kw["text"] for kw in s.bot.sent("send_message", 1)] == [guide.HELP_TEXT]
            assert s.bot.sent("send_document", 1) == []

            guide.GUIDE_PATH = real_path
            hook = lambda m, cid, kw: (_ for _ in ()).throw(net_error()) if m == "send_document" else None
            s.bot.hooks.append(hook)
            await misc.cmd_help(_message(1), s.bot)
            texts = [kw["text"] for kw in s.bot.sent("send_message", 1)]
            assert texts == [guide.HELP_TEXT, guide.HELP_TEXT]
            # правило одного сообщения: предыдущий текстовый ответ убран
            assert len([1 for (c, _), tx in s.bot.alive.items() if c == 1 and tx == guide.HELP_TEXT]) == 1
        finally:
            guide.GUIDE_PATH = real_path
            restore()
    sim.run(sc)


def test_rejected_cached_file_id_is_replaced_by_fresh_upload():
    async def sc(s):
        from bot import guide
        from bot.handlers import misc
        from tests.sim import bad_chat
        calls, restore = _stub_schedule_delete(None)
        try:
            await s.make_user(1)
            await misc.cmd_help(_message(1), s.bot)
            assert guide._file_id == "FAKE_FILE_ID"

            def hook(method, cid, kw):
                if method == "send_document" and isinstance(kw.get("document"), str):
                    raise bad_chat()                             # Telegram отверг file_id
            s.bot.hooks.append(hook)
            await misc.cmd_help(_message(1), s.bot)
            docs = s.bot.sent("send_document", 1)
            assert len(docs) == 2 and not isinstance(docs[1]["document"], str)
            assert s.bot.sent("send_message", 1) == []           # текстового fallback не понадобилось
        finally:
            restore()
    sim.run(sc)


# ---------------------------------------------------------------- гайд: регистрация


def test_registration_sends_guide_once_above_the_first_step():
    async def sc(s):
        from bot import guide
        from bot.handlers import start as h
        await h.cmd_start(_message(7), s.bot)
        u = await s.db.get_user(7)
        assert u["guide_sent"] == 1 and u["reg_state"] == "time_of_day"

        order = [m for (m, cid, kw) in s.bot.log if cid == 7 and m in ("send_document", "send_message")]
        assert order == ["send_document", "send_message"]        # гайд выше шага с кнопками
        doc = s.bot.sent("send_document", 7)[0]
        assert doc["caption"] == guide.REGISTRATION_CAPTION and "/help" in doc["caption"]

        # повторный /start в процессе регистрации: шаг заново, гайд - нет
        await h.cmd_start(_message(7), s.bot)
        assert len(s.bot.sent("send_document", 7)) == 1
        assert len(s.bot.sent("send_message", 7)) == 2
    sim.run(sc)


def test_registration_survives_guide_failure_and_retries_on_next_start():
    async def sc(s):
        from bot.handlers import start as h
        hook = lambda m, cid, kw: (_ for _ in ()).throw(net_error()) if m == "send_document" else None
        s.bot.hooks.append(hook)
        await h.cmd_start(_message(7), s.bot)
        u = await s.db.get_user(7)
        assert u["guide_sent"] == 0 and u["reg_state"] == "time_of_day"
        assert len(s.bot.sent("send_message", 7)) == 1           # шаг регистрации всё равно показан

        s.bot.hooks.clear()
        await h.cmd_start(_message(7), s.bot)
        assert len(s.bot.sent("send_document", 7)) == 1
        assert (await s.db.get_user(7))["guide_sent"] == 1
    sim.run(sc)


def test_registration_guide_missing_file_does_not_block():
    async def sc(s):
        from pathlib import Path
        from bot import guide
        from bot.handlers import start as h
        real = guide.GUIDE_PATH
        guide.GUIDE_PATH = Path("/nonexistent/guide.pdf")
        try:
            await h.cmd_start(_message(7), s.bot)
        finally:
            guide.GUIDE_PATH = real
        assert s.bot.sent("send_document", 7) == []
        assert len(s.bot.sent("send_message", 7)) == 1
        assert (await s.db.get_user(7))["guide_sent"] == 0
    sim.run(sc)


def test_double_start_at_once_sends_single_guide():
    async def sc(s):
        from bot.handlers import start as h
        await asyncio.gather(h.cmd_start(_message(7), s.bot), h.cmd_start(_message(7), s.bot))
        assert len(s.bot.sent("send_document", 7)) == 1
    sim.run(sc)


def test_registered_players_do_not_get_guide_on_start():
    async def sc(s):
        from bot.handlers import start as h
        await s.make_user(1)
        await h.cmd_start(_message(1), s.bot)
        assert s.bot.sent("send_document", 1) == []
    sim.run(sc)


def test_full_registration_flow_keeps_working_with_guide():
    async def sc(s):
        from bot.handlers import group as g
        from bot.handlers import start as h
        await h.cmd_start(_message(9), s.bot)
        await h.on_time_chosen(_call(9, s.bot, "time:evening"), s.bot)
        await h.on_focus_toggle(_call(9, s.bot, "focus_toggle:pushups"))
        await h.on_focus_done(_call(9, s.bot, "focus_done"), s.bot)
        await h.group_skip(_call(9, s.bot, "group_skip"), s.bot)
        u = await s.db.get_user(9)
        assert u["reg_state"] == "done" and u["profile_message_id"]
        assert len(s.bot.sent("send_document", 9)) == 1
        # гайд остался в чате (его не удаляют шаги регистрации), служебные шаги убраны
        assert any(cid == 9 and "Гайд по Системе" in txt for (cid, _), txt in s.bot.alive.items())
    sim.run(sc)


# ---------------------------------------------------------------- гайд: файл, подписи, миграция


def test_guide_file_is_a_real_pdf_and_captions_fit_telegram_limit():
    from bot import guide
    assert guide.guide_available()
    head = guide.GUIDE_PATH.read_bytes()[:5]
    assert head == b"%PDF-"
    assert guide.GUIDE_PATH.stat().st_size < 50 * 1024 * 1024
    assert len(guide.HELP_TEXT) <= 1024 and len(guide.REGISTRATION_CAPTION) <= 1024


def test_old_database_gets_guide_column_and_existing_players_are_not_affected(tmp_path):
    import sqlite3
    old = str(tmp_path / "old.sql")
    con = sqlite3.connect(old)
    con.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, registered_at TEXT, "
                "time_of_day TEXT, focuses TEXT DEFAULT '[]', group_id INTEGER, xp INTEGER DEFAULT 0, "
                "level INTEGER DEFAULT 1, streak INTEGER DEFAULT 0, penalty_until TEXT, reg_state TEXT DEFAULT 'done')")
    con.execute("INSERT INTO users (user_id, username, registered_at, reg_state) VALUES (5, 'old', '2026-01-01T00:00:00', 'done')")
    con.commit()
    con.close()

    async def sc(s):
        from bot.database import Database
        d = Database(old)
        await d.connect()
        cols = {r[1] for r in await d._conn.execute_fetchall("PRAGMA table_info(users)")}
        assert "guide_sent" in cols
        assert await d.claim_guide(5) is True and await d.claim_guide(5) is False
        await d.release_guide(5)
        assert await d.claim_guide(5) is True
        await d.close()
    sim.run(sc)


# ---------------------------------------------------------------- разделители


def test_dividers_are_short_enough_for_small_screens():
    from bot import messages as tx
    from bot import challenge_render as cr
    assert set(tx.SYSTEM_DIVIDER) == {"─"} and len(tx.SYSTEM_DIVIDER) <= 12
    assert set(cr.DIVIDER) == {"="} and len(cr.DIVIDER) <= 20

    for text in (tx.challenge_warning(15, 300, 48), tx.challenge_warning(60, 300, 48),
                 tx.challenge_failed("timeout", 300, 48), tx.challenge_failed("gave_up", 300, 48)):
        lines = text.split("\n")
        seps = [ln for ln in lines if ln and set(ln) == {"─"}]
        assert len(seps) == 3
        assert all(ln == tx.SYSTEM_DIVIDER for ln in seps)
        assert "─" * 13 not in text


def test_quest_card_uses_the_short_divider():
    async def sc(s):
        ch = await setup_quest(s)
        text = s.bot.alive[(1, ch["message_id"])]
        lines = text.split("\n")
        seps = [ln for ln in lines if ln and set(ln) == {"="}]
        assert len(seps) == 1 and len(seps[0]) <= 20
    sim.run(sc)
