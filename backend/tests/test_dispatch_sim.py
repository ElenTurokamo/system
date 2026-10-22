"""
Симуляция отказоустойчивой выдачи квестов (bot/dispatch.py).
Слот 'day' = 13:00 Алматы = 08:00 UTC; все времена в тестах - UTC, если не сказано иное.
Запуск:  cd backend && python -m pytest tests -q
"""
import asyncio
from datetime import datetime, timedelta

from tests import sim
from tests.sim import Clock, bad_chat, fail_cards, forbidden, net_error, retry_after

D22 = "2026-10-22"
D23 = "2026-10-23"


def t(h, m=0, day=22):
    return datetime(2026, 10, day, h, m)


async def snapshot_user(db, uid):
    u = await db.get_user(uid)
    return {k: u[k] for k in ("xp", "level", "streak", "penalty_until", "consecutive_failures", "is_stopped")}


# ---------------------------------------------------------------- базовые сценарии


def test_happy_path():
    async def sc(s):
        await s.make_user(1)
        await s.until(t(8, 30))
        rec = await s.rec(1)
        assert rec["status"] == "delivered" and rec["attempts"] == 1
        ch = await s.challenges(1)
        assert len(ch) == 1 and ch[0]["message_id"] and ch[0]["status"] == "awaiting_action"
        assert ch[0]["started_at"] == "2026-10-22T08:00:00"
        assert len(s.bot.cards_alive(1)) == 1
        # дальше в течение дня ничего лишнего не приходит
        await s.until(t(14))
        assert s.bot.card_attempt_count(1) == 1
    sim.run(sc)


def test_one_failure_then_success_next_hour():
    async def sc(s):
        await s.make_user(1)
        fail_cards(s.bot, 1, net_error, times=3)  # вся первая попытка (3 внутренних отправки)
        await s.until(t(8, 30))
        rec = await s.rec(1)
        assert rec["status"] == "pending" and rec["attempts"] == 1
        assert rec["next_attempt_at"] == "2026-10-22T09:00:00"
        ch = await s.challenges(1)
        assert len(ch) == 1 and ch[0]["message_id"] is None  # карточка создана, но не доставлена
        assert not s.bot.cards_alive(1)

        await s.until(t(9, 5))
        rec = await s.rec(1)
        assert rec["status"] == "delivered" and rec["attempts"] == 2
        ch = await s.challenges(1)
        assert len(ch) == 1 and ch[0]["message_id"]       # тот же квест, без дубля
        assert ch[0]["started_at"] == "2026-10-22T09:00:00"  # таймаут идёт с момента доставки
        assert await s.progress_rows() == 1
        assert len(s.bot.cards_alive(1)) == 1
    sim.run(sc)


def test_inner_quick_retry_absorbs_transient_glitch():
    """Единичный сбой сети не должен стоить игроку часа: внутри попытки есть быстрые повторы."""
    async def sc(s):
        await s.make_user(1)
        fail_cards(s.bot, 1, net_error, times=1)
        await s.until(t(8, 5))
        rec = await s.rec(1)
        assert rec["status"] == "delivered" and rec["attempts"] == 1
    sim.run(sc)


def test_flood_control_retry_after():
    async def sc(s):
        await s.make_user(1)
        fail_cards(s.bot, 1, lambda: retry_after(0), times=2)
        await s.until(t(8, 5))
        assert (await s.rec(1))["status"] == "delivered"
    sim.run(sc)


def test_three_retries_then_skip_until_next_day():
    async def sc(s):
        await s.make_user(1)
        before = await snapshot_user(s.db, 1)
        fail_cards(s.bot, 1, net_error, times=None)

        attempt_times = []
        last = None
        while Clock.now < t(14):
            await s.step(1)
            rec = await s.rec(1)
            if rec and rec["last_attempt_at"] != last:
                last = rec["last_attempt_at"]
                attempt_times.append(last)
        # слот, +1ч, +2ч, +3ч - ровно четыре попытки с шагом в час
        assert attempt_times == [
            "2026-10-22T08:00:00", "2026-10-22T09:00:00", "2026-10-22T10:00:00", "2026-10-22T11:00:00",
        ], attempt_times
        rec = await s.rec(1)
        assert rec["status"] == "failed" and rec["attempts"] == 4
        # недоставленная карточка убрана полностью
        assert await s.challenges(1) == [] and await s.progress_rows() == 0
        # игрок не оштрафован за то, чего не видел
        assert await snapshot_user(s.db, 1) == before
        assert not s.bot.cards_alive(1)

        # до следующего дня попыток больше нет, даже когда Telegram "починился"
        s.bot.hooks.clear()
        attempts_then = s.bot.card_attempt_count(1)
        await s.until(t(23))
        assert s.bot.card_attempt_count(1) == attempts_then

        # следующий день - всё работает как обычно
        await s.until(t(8, 30, day=23))
        rec23 = await s.rec(1, day=D23)
        assert rec23["status"] == "delivered" and rec23["attempts"] == 1
        assert len(s.bot.cards_alive(1)) == 1
    sim.run(sc)


def test_undelivered_card_is_never_penalized_after_18h():
    """Главный баг, который лечим: раньше недоставленная карточка сгорала через 18ч со штрафом."""
    async def sc(s):
        await s.make_user(1)
        before = await snapshot_user(s.db, 1)
        fail_cards(s.bot, 1, net_error, times=None)
        await s.until(t(8) + timedelta(hours=19))  # заведомо позже 18ч с момента слота
        assert await snapshot_user(s.db, 1) == before
        assert await s.db.get_failure_message_count(1) == 0 if hasattr(s.db, "get_failure_message_count") else True
        failure_reports = [m for m in s.bot.log if m[0] == "send_message" and "Испытание провалено" in m[2]["text"]]
        assert not failure_reports
    sim.run(sc)


def test_timer_counts_from_delivery_not_creation():
    async def sc(s):
        await s.make_user(1)
        fail_cards(s.bot, 1, net_error, times=3)
        await s.until(t(9, 5))                       # доставлено в 09:00
        ch = (await s.challenges(1))[0]
        assert ch["started_at"] == "2026-10-22T09:00:00"
        # 08:00 + 18ч = 02:00 следующего дня: по старой логике сгорело бы здесь
        await s.until(t(2, 30, day=23))
        assert (await s.challenges(1))[0]["status"] == "awaiting_action"
        # 09:00 + 18ч = 03:00: теперь сгорает, как и положено
        await s.until(t(3, 5, day=23))
        assert (await s.challenges(1))[0]["status"] == "expired"
    sim.run(sc)


# ---------------------------------------------------------------- игрок "отключил" бота


def test_user_stops_between_attempts_like_stop_handler():
    async def sc(s):
        await s.make_user(1)
        fail_cards(s.bot, 1, net_error, times=None)
        await s.until(t(8, 30))
        # то, что делает handlers/start.py::on_stop_system_confirmed
        await s.db.set_stopped(1, True)
        active = await s.db.get_active_challenge(1)
        await s.db.stop_challenge(active["id"])
        n = s.bot.card_attempt_count(1)

        await s.until(t(12))
        rec = await s.rec(1)
        assert rec["status"] == "skipped" and rec["reason"] == "stopped"
        assert s.bot.card_attempt_count(1) == n          # ни одной попытки после /stop
        ch = await s.challenges(1)
        assert [c["status"] for c in ch] == ["stopped"]
        assert await s.db._conn.execute_fetchall("SELECT 1 FROM daily_stats") == []  # нулевой день не пишется
    sim.run(sc)


def test_autostop_flag_only_drops_undelivered_card():
    async def sc(s):
        await s.make_user(1)
        fail_cards(s.bot, 1, net_error, times=None)
        await s.until(t(8, 30))
        await s.db.set_stopped(1, True)                  # без закрытия карточки (как автостоп)
        await s.until(t(9, 5))
        rec = await s.rec(1)
        assert rec["status"] == "skipped" and rec["reason"] == "stopped"
        assert await s.challenges(1) == []
    sim.run(sc)


def test_user_blocks_bot_between_attempts_no_hourly_retries():
    async def sc(s):
        await s.make_user(1)
        fail_cards(s.bot, 1, net_error, times=None)
        await s.until(t(8, 30))
        s.bot.hooks.clear()
        s.bot.blocked.add(1)
        await s.until(t(9, 5))
        rec = await s.rec(1)
        assert rec["status"] == "unreachable" and rec["attempts"] == 2
        assert await s.challenges(1) == []
        n = s.bot.card_attempt_count(1)
        await s.until(t(13))
        assert s.bot.card_attempt_count(1) == n          # 10:00 и 11:00 попыток нет

        # на следующий день бот пробует снова; игрок разблокировал - квест пришёл
        s.bot.blocked.clear()
        await s.until(t(8, 30, day=23))
        assert (await s.rec(1, day=D23))["status"] == "delivered"
    sim.run(sc)


def test_blocked_from_the_start_single_attempt():
    async def sc(s):
        await s.make_user(1)
        s.bot.blocked.add(1)
        await s.until(t(9))
        rec = await s.rec(1)
        assert rec["status"] == "unreachable" and rec["attempts"] == 1
        assert s.bot.card_attempt_count(1) == 1
        assert await s.challenges(1) == []
        # на следующий день снова одна попытка (а не накопленные)
        await s.until(t(9, day=23))
        rec = await s.rec(1, day=D23)
        assert rec["status"] == "unreachable" and rec["attempts"] == 1
    sim.run(sc)


def test_chat_not_found_is_unreachable():
    async def sc(s):
        await s.make_user(1)
        fail_cards(s.bot, 1, bad_chat, times=None)
        await s.until(t(9))
        assert (await s.rec(1))["status"] == "unreachable"
    sim.run(sc)


def test_stopped_user_gets_no_quest_and_resume_same_day_does_not_backfill():
    async def sc(s):
        await s.make_user(1, stopped=True)
        await s.until(t(8, 5))
        rec = await s.rec(1)
        assert rec["status"] == "skipped" and rec["reason"] == "stopped"
        assert s.bot.card_attempt_count(1) == 0
        await s.db.set_stopped(1, False)                 # /start посреди дня
        await s.until(t(13))
        assert s.bot.card_attempt_count(1) == 0          # как и раньше - ждём следующего слота
        await s.until(t(8, 5, day=23))
        assert (await s.rec(1, day=D23))["status"] == "delivered"
    sim.run(sc)


def test_penalty_slot_skipped_and_no_late_delivery_after_penalty_ends():
    async def sc(s):
        await s.make_user(1)
        await s.db.set_penalty(1, 2)                     # до 09:59
        await s.until(t(8, 5))
        rec = await s.rec(1)
        assert rec["status"] == "skipped" and rec["reason"] == "penalty"
        await s.until(t(13))
        assert s.bot.card_attempt_count(1) == 0
        await s.until(t(8, 5, day=23))
        assert (await s.rec(1, day=D23))["status"] == "delivered"
    sim.run(sc)


def test_user_without_focuses_skipped():
    async def sc(s):
        await s.make_user(1, focuses=())
        await s.until(t(8, 5))
        rec = await s.rec(1)
        assert rec["status"] == "skipped" and rec["reason"] == "no_focuses"
    sim.run(sc)


def test_unregistered_user_ignored():
    async def sc(s):
        await s.db.create_user_if_missing(5, "x", "X")   # reg_state != done
        await s.db.set_time_of_day(5, "day")
        await s.until(t(8, 5))
        assert await s.rec(5) is None and s.bot.card_attempt_count(5) == 0
    sim.run(sc)


def test_active_delivered_quest_blocks_new_one():
    async def sc(s):
        await s.make_user(1)
        cid = await s.db.create_challenge(1, "old quest", ["pushups"])
        await s.db.set_message_id(cid, 777)
        await s.db._conn.execute("UPDATE challenges SET started_at = ? WHERE id = ?", ("2026-10-22T07:00:00", cid))
        await s.db._conn.commit()
        await s.until(t(8, 5))
        rec = await s.rec(1)
        assert rec["status"] == "skipped" and rec["reason"] == "active_challenge"
        assert len(await s.challenges(1)) == 1
    sim.run(sc)


# ---------------------------------------------------------------- устаревшие/недоставленные карточки


def test_legacy_undelivered_challenge_is_delivered_not_duplicated():
    async def sc(s):
        await s.make_user(1)
        cid = await s.db.create_challenge(1, "legacy", ["pushups"])      # message_id NULL
        await s.db._conn.execute("UPDATE challenges SET started_at = ? WHERE id = ?", ("2026-10-20T08:00:00", cid))
        await s.db._conn.commit()
        await s.until(t(8, 5))
        ch = await s.challenges(1)
        assert len(ch) == 1 and ch[0]["id"] == cid and ch[0]["message_id"]
        assert ch[0]["started_at"] == "2026-10-22T08:00:00"
        assert (await s.rec(1))["status"] == "delivered"
    sim.run(sc)


def test_undelivered_challenge_not_touched_by_expire_timers_warnings():
    async def sc(s):
        from bot import scheduler
        await s.make_user(1)
        cid = await s.db.create_challenge(1, "q", ["pushups"])
        await s.db._conn.execute("UPDATE challenges SET started_at = ? WHERE id = ?", ("2026-10-20T00:00:00", cid))
        await s.db._conn.commit()
        before = await snapshot_user(s.db, 1)
        await scheduler.expire_stale_challenges(s.bot)
        await scheduler.send_failure_warnings(s.bot)
        await scheduler.refresh_challenge_timers(s.bot)
        await scheduler.refresh_final_countdown(s.bot)
        assert (await s.challenges(1))[0]["status"] == "awaiting_action"
        assert await snapshot_user(s.db, 1) == before
        assert not [m for m in s.bot.log if m[0] == "send_message"]
        assert await s.db.get_awaiting_challenges() == [] and await s.db.get_expirable_challenges(18) == []
    sim.run(sc)


def test_delivered_challenge_still_expires_with_penalty():
    """Регрессия: обычная просрочка ДОСТАВЛЕННОГО квеста работает как раньше."""
    async def sc(s):
        await s.make_user(1)
        await s.until(t(8, 5))
        await s.until(t(2, 5, day=23))     # 08:00 + 18ч = 02:00
        assert (await s.challenges(1))[0]["status"] == "expired"
        u = await s.db.get_user(1)
        assert u["consecutive_failures"] == 1 and u["penalty_until"]
    sim.run(sc)


# ---------------------------------------------------------------- простой бота / проверка по таймеру


def test_missed_cron_is_caught_up_by_reconcile():
    async def sc(s):
        await s.make_user(1)
        s.cron_only_down = True
        await s.until(t(8, 1))
        assert s.bot.card_attempt_count(1) == 0          # льготные 2 минуты: пока рано
        await s.until(t(8, 5))
        rec = await s.rec(1)
        assert rec["status"] == "delivered" and s.bot.card_attempt_count(1) == 1
        assert (await s.challenges(1))[0]["started_at"] == "2026-10-22T08:02:00"
    sim.run(sc)


def test_catch_up_window_edge():
    async def sc(s):
        await s.make_user(1)
        s.cron_only_down = True
        await s.until(t(10, 59))
        assert (await s.rec(1))["status"] == "delivered"   # внутри окна 3ч
    sim.run(sc)

    async def sc2(s):
        await s.make_user(1)
        s.cron_down = True                                 # бот лежал весь слот и ещё 3.5 часа
        await s.until(t(11, 30))
        s.cron_down = False
        s.cron_only_down = True
        await s.until(t(12, 30))
        assert await s.rec(1) is None and s.bot.card_attempt_count(1) == 0   # окно закрыто - день пропущен
        s.cron_only_down = False
        await s.until(t(8, 5, day=23))
        assert (await s.rec(1, day=D23))["status"] == "delivered"
    sim.run(sc2)


def test_catch_up_skips_users_registered_after_slot():
    async def sc(s):
        await s.make_user(1)
        s.cron_only_down = True
        await s.until(t(8, 10))
        await s.make_user(2, registered_days_ago=0)        # зарегистрировался в 08:10, слот был в 08:00
        await s.until(t(9))
        assert (await s.rec(1))["status"] == "delivered"
        assert await s.rec(2) is None and s.bot.card_attempt_count(2) == 0
    sim.run(sc)


def test_cron_in_progress_blocks_catch_up():
    async def sc(s):
        from bot import dispatch
        await s.make_user(1)
        s.cron_only_down = True
        dispatch._slot_runs_in_progress.add((D22, "day"))
        await s.until(t(8, 10))
        assert s.bot.card_attempt_count(1) == 0
        dispatch._slot_runs_in_progress.discard((D22, "day"))
        await s.until(t(8, 12))
        assert s.bot.card_attempt_count(1) == 1
    sim.run(sc)


def test_catch_up_does_not_repeat_after_clean_run():
    async def sc(s):
        await s.make_user(1)
        await s.until(t(8, 5))                              # cron отработал
        s.cron_only_down = True
        await s.until(t(11))
        assert s.bot.card_attempt_count(1) == 1
        assert await s.db.is_slot_run_finished(D22, "day")
    sim.run(sc)


def test_migration_existing_todays_quest_not_duplicated():
    """Обновление кода после рассылки: квест уже выдан старой версией, записи в журнале нет."""
    async def sc(s):
        await s.make_user(1)
        cid = await s.db.create_challenge(1, "old-code quest", ["pushups"])
        await s.db.set_message_id(cid, 555)
        await s.db._conn.execute("UPDATE challenges SET started_at = ? WHERE id = ?", ("2026-10-22T08:00:03", cid))
        await s.db._conn.commit()
        s.cron_only_down = True
        await s.until(t(8, 5))
        rec = await s.rec(1)
        assert rec["status"] == "delivered" and rec["reason"] == "pre_existing"
        assert s.bot.card_attempt_count(1) == 0 and len(await s.challenges(1)) == 1
    sim.run(sc)


def test_midnight_crossing_night_slot():
    async def sc(s):
        Clock.now = datetime(2026, 10, 22, 17, 59)          # 22:59 по Алматы
        await s.make_user(1, slot="night")
        fail_cards(s.bot, 1, net_error, times=None)
        await s.until(datetime(2026, 10, 22, 21, 30))        # 02:30 локально 23-го
        rec = await s.db.get_dispatch(1, D22, "night")       # день записи - день СЛОТА, не день попытки
        assert rec["status"] == "failed" and rec["attempts"] == 4
        assert await s.db.get_dispatch(1, D23, "night") is None
        s.bot.hooks.clear()
        await s.until(datetime(2026, 10, 23, 18, 5))         # 23:05 локально 23-го
        assert (await s.db.get_dispatch(1, D23, "night"))["status"] == "delivered"
    sim.run(sc)


def test_midnight_crossing_catch_up_uses_yesterday_day_key():
    async def sc(s):
        Clock.now = datetime(2026, 10, 22, 17, 59)
        await s.make_user(1, slot="night")
        s.cron_only_down = True
        await s.until(datetime(2026, 10, 22, 19, 30))        # 00:30 локально 23-го
        assert (await s.db.get_dispatch(1, D22, "night"))["status"] == "delivered"
        assert await s.db.get_dispatch(1, D23, "night") is None
    sim.run(sc)


# ---------------------------------------------------------------- идемпотентность / гонки


def test_double_cron_and_concurrent_calls_single_delivery():
    async def sc(s):
        from bot import dispatch, scheduler
        await s.make_user(1)
        Clock.now = t(8, 0)
        await asyncio.gather(
            scheduler.dispatch_daily_challenges(s.bot, "day"),
            scheduler.dispatch_daily_challenges(s.bot, "day"),
            dispatch.process_dispatch(s.bot, 1, D22, "day"),
            dispatch.process_dispatch(s.bot, 1, D22, "day"),
        )
        assert s.bot.card_attempt_count(1) == 1 and len(await s.challenges(1)) == 1
        await dispatch.dispatch_slot(s.bot, "day")
        assert s.bot.card_attempt_count(1) == 1
    sim.run(sc)


def test_concurrent_pending_retry_single_attempt():
    async def sc(s):
        from bot import dispatch
        await s.make_user(1)
        fail_cards(s.bot, 1, net_error, times=3)
        await s.until(t(8, 30))
        Clock.now = t(9, 0)
        res = await asyncio.gather(*[dispatch.process_dispatch(s.bot, 1, D22, "day") for _ in range(5)])
        assert sorted(res).count("delivered") == 1
        assert s.bot.card_attempt_count(1) == 4 and len(await s.challenges(1)) == 1
    sim.run(sc)


def test_slot_change_cancels_pending_retries():
    async def sc(s):
        await s.make_user(1)
        fail_cards(s.bot, 1, net_error, times=3)
        await s.until(t(8, 30))
        await s.db.set_time_of_day(1, "evening")             # /change_time
        await s.until(t(9, 5))
        rec = await s.rec(1)
        assert rec["status"] == "skipped" and rec["reason"] == "slot_changed"
        assert await s.challenges(1) == []
        await s.until(t(14, 5))                              # 19:00 локально - новый слот
        assert (await s.rec(1, slot="evening"))["status"] == "delivered"
    sim.run(sc)


def test_stop_during_send_race_removes_fresh_card():
    async def sc(s):
        from bot import dispatch
        await s.make_user(1)
        orig = s.bot.send_message

        async def racing(chat_id, text, **kw):
            msg = await orig(chat_id, text, **kw)
            if kw.get("reply_markup") is not None:
                await s.db.set_stopped(chat_id, True)       # /stop нажат ровно во время отправки
                active = await s.db.get_active_challenge(chat_id)
                await s.db.stop_challenge(active["id"])
            return msg

        s.bot.send_message = racing
        Clock.now = t(8, 0)
        out = await dispatch.process_dispatch(s.bot, 1, D22, "day")
        assert out == "skipped:closed_during_send"
        assert not s.bot.cards_alive(1)                      # карточка с мёртвыми кнопками удалена
    sim.run(sc)


def test_start_command_delivers_pending_card_and_stops_retries():
    async def sc(s):
        from bot import challenge_render
        await s.make_user(1)
        fail_cards(s.bot, 1, net_error, times=3)
        await s.until(t(8, 30))
        active = await s.db.get_active_challenge(1)
        await challenge_render.resend_challenge_message(s.bot, active["id"])  # то, что делает /start
        assert len(s.bot.cards_alive(1)) == 1
        await s.until(t(9, 5))
        rec = await s.rec(1)
        assert rec["status"] == "delivered" and rec["reason"] == "delivered_elsewhere"
        assert len(s.bot.cards_alive(1)) == 1 and len(await s.challenges(1)) == 1
        ch = (await s.challenges(1))[0]
        assert ch["started_at"] == "2026-10-22T08:30:00"     # таймер пошёл с реальной доставки
    sim.run(sc)


# ---------------------------------------------------------------- сбои внутри попытки


def test_crash_marker_resumes_and_reuses_challenge():
    async def sc(s):
        from bot import dispatch
        await s.make_user(1)
        cid = await s.db.create_challenge(1, "half-created", ["pushups"])
        await s.db.save_dispatch(1, D22, "day", "pending", reason="attempt_in_progress", attempts=1,
                                 last_attempt_at="2026-10-22T08:00:00", next_attempt_at="2026-10-22T09:00:00")
        Clock.now = t(9, 0)
        assert await dispatch.process_dispatch(s.bot, 1, D22, "day") == "delivered"
        rec = await s.rec(1)
        assert rec["attempts"] == 2
        ch = await s.challenges(1)
        assert len(ch) == 1 and ch[0]["id"] == cid and ch[0]["message_id"]
    sim.run(sc)


def test_crash_on_last_attempt_finalizes_as_failed():
    async def sc(s):
        from bot import dispatch
        await s.make_user(1)
        await s.db.create_challenge(1, "half-created", ["pushups"])
        await s.db.save_dispatch(1, D22, "day", "pending", reason="attempt_in_progress", attempts=4,
                                 last_attempt_at="2026-10-22T11:00:00", next_attempt_at="2026-10-22T12:00:00")
        Clock.now = t(12, 0)
        assert await dispatch.process_dispatch(s.bot, 1, D22, "day") == "failed"
        assert await s.challenges(1) == [] and s.bot.card_attempt_count(1) == 0
    sim.run(sc)


def test_zero_retries_configured():
    async def sc(s):
        await s.make_user(1)
        fail_cards(s.bot, 1, net_error, times=None)
        await s.until(t(12))
        rec = await s.rec(1)
        assert rec["status"] == "failed" and rec["attempts"] == 1
    sim.run(sc, dispatch_retry_attempts=0)


def test_custom_interval_and_attempts():
    async def sc(s):
        await s.make_user(1)
        fail_cards(s.bot, 1, net_error, times=None)
        seen = []
        while Clock.now < t(10):
            await s.step(1)
            r = await s.rec(1)
            if r and (not seen or seen[-1] != r["last_attempt_at"]):
                seen.append(r["last_attempt_at"])
        assert seen == ["2026-10-22T08:00:00", "2026-10-22T08:30:00", "2026-10-22T09:00:00"]
        assert (await s.rec(1))["status"] == "failed"
    sim.run(sc, dispatch_retry_attempts=2, dispatch_retry_interval_minutes=30)


def test_profile_failure_does_not_count_as_failed_attempt():
    async def sc(s):
        from bot import profile
        async def boom(*a, **k):
            raise RuntimeError("profile exploded")
        profile.ensure_fresh_profile_message = boom
        await s.make_user(1)
        await s.until(t(8, 5))
        rec = await s.rec(1)
        assert rec["status"] == "delivered" and rec["attempts"] == 1
    from bot import profile
    orig = profile.ensure_fresh_profile_message
    try:
        sim.run(sc)
    finally:
        profile.ensure_fresh_profile_message = orig


def test_db_error_on_create_counts_as_attempt_and_recovers_without_duplicates():
    async def sc(s):
        orig = s.db.create_challenge
        calls = {"n": 0}

        async def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("database is locked")
            return await orig(*a, **k)

        s.db.create_challenge = flaky
        await s.make_user(1)
        await s.until(t(8, 30))
        rec = await s.rec(1)
        assert rec["status"] == "pending" and rec["attempts"] == 1 and "database is locked" in rec["last_error"]
        await s.until(t(9, 5))
        assert (await s.rec(1))["status"] == "delivered"
        assert len(await s.challenges(1)) == 1 and await s.progress_rows() == 1
    sim.run(sc)


def test_unexpected_error_in_checks_is_retried_by_reconcile_not_lost():
    async def sc(s):
        orig = s.db.get_focuses
        state = {"fail": True}

        async def flaky(uid):
            if uid == 1 and state["fail"]:
                raise RuntimeError("db hiccup")
            return await orig(uid)

        await s.make_user(1)
        await s.make_user(2)
        s.db.get_focuses = flaky
        await s.until(t(8, 1))
        assert (await s.rec(2))["status"] == "delivered"     # соседа сбой не задел
        assert await s.rec(1) is None
        state["fail"] = False
        await s.until(t(8, 5))
        assert (await s.rec(1))["status"] == "delivered"      # слот не помечен обойдённым - досылка
        assert s.bot.card_attempt_count(2) == 1                # а у соседа дубля нет
    sim.run(sc)


# ---------------------------------------------------------------- много игроков


def test_mixed_users_isolated():
    async def sc(s):
        for uid in (1, 2, 3, 4, 5):
            await s.make_user(uid)
        await s.db.set_stopped(4, True)
        s.bot.blocked.add(1)
        fail_cards(s.bot, 2, net_error, times=None)
        await s.until(t(8, 5))
        st = {u: (await s.rec(u))["status"] for u in (1, 2, 3, 4, 5)}
        assert st == {1: "unreachable", 2: "pending", 3: "delivered", 4: "skipped", 5: "delivered"}
        s.bot.hooks.clear()
        await s.until(t(9, 5))
        assert (await s.rec(2))["status"] == "delivered"
        assert (await s.rec(1))["status"] == "unreachable"
    sim.run(sc)


def test_many_users_flaky_network():
    import random
    async def sc(s):
        rnd = random.Random(7)
        for uid in range(100, 160):
            await s.make_user(uid)
        flaky = set(rnd.sample(range(100, 160), 20))
        dead = set(rnd.sample(sorted(set(range(100, 160)) - flaky), 5))
        for uid in flaky:
            fail_cards(s.bot, uid, net_error, times=rnd.choice([3, 6]))   # 1 или 2 неудачные попытки
        for uid in dead:
            fail_cards(s.bot, uid, net_error, times=None)
        await s.until(t(12))
        for uid in range(100, 160):
            rec = await s.rec(uid)
            if uid in dead:
                assert rec["status"] == "failed" and rec["attempts"] == 4, uid
                assert await s.challenges(uid) == []
            else:
                assert rec["status"] == "delivered", (uid, rec)
                assert len(s.bot.cards_alive(uid)) == 1 and len(await s.challenges(uid)) == 1
    sim.run(sc)


# ---------------------------------------------------------------- вспомогательное


def test_slot_time_helpers():
    async def sc(s):
        from bot import dispatch
        from datetime import date
        assert dispatch.slot_start_utc(date(2026, 10, 22), "13:00") == datetime(2026, 10, 22, 8, 0)
        assert dispatch.slot_start_utc(date(2026, 10, 22), "23:00") == datetime(2026, 10, 22, 18, 0)
        assert dispatch.slot_start_utc(date(2026, 10, 22), "02:30") == datetime(2026, 10, 21, 21, 30)
        Clock.now = datetime(2026, 10, 22, 19, 30)           # 00:30 локально 23-го
        assert dispatch.local_today() == date(2026, 10, 23)
        assert dispatch.resolve_slot_day("night") == D22
        assert dispatch.resolve_slot_day("morning") == D22   # утренний слот 23-го (05:00) ещё не наступил
        Clock.now = datetime(2026, 10, 23, 0, 30)            # 05:30 локально 23-го
        assert dispatch.resolve_slot_day("morning") == D23
        Clock.now = datetime(2026, 10, 22, 7, 59, 40)        # cron чуть раньше - допуск 30с
        assert dispatch.resolve_slot_day("day") == D22
    sim.run(sc)


def test_scheduler_registers_jobs():
    async def sc(s):
        from bot import scheduler
        sch = scheduler.setup_scheduler(s.bot)
        sch.start()
        try:
            jobs = {j.id: j for j in sch.get_jobs()}
            assert "dispatch_reconcile" in jobs and jobs["dispatch_reconcile"].next_run_time is not None
            for slot in ("morning", "day", "evening", "night"):
                j = jobs[f"dispatch_{slot}"]
                assert j.misfire_grace_time == 300 and j.max_instances == 1
            assert "expire_check" in jobs
        finally:
            sch.shutdown(wait=False)
    sim.run(sc)


def test_upgrade_of_existing_database_keeps_data(tmp_path):
    import os, shutil, sqlite3
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data_storage", "db.sql")
    if not os.path.exists(src):
        return
    dst = str(tmp_path / "copy.sql")
    shutil.copy(src, dst)
    con = sqlite3.connect(dst)
    before = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("users", "challenges", "challenge_progress", "daily_stats")}
    con.close()

    async def sc(s):
        from bot.database import Database
        d = Database(dst)
        await d.connect()
        for tname, n in before.items():
            cur = await d._conn.execute(f"SELECT COUNT(*) FROM {tname}")
            assert (await cur.fetchone())[0] == n, tname
        cur = await d._conn.execute("SELECT name FROM sqlite_master WHERE name IN ('dispatch_log','dispatch_runs')")
        assert len(await cur.fetchall()) == 2
        await d.save_dispatch(1, D22, "day", "delivered")
        assert (await d.get_dispatch(1, D22, "day"))["status"] == "delivered"
        await d.close()
    sim.run(sc)


def test_changing_to_a_slot_that_already_passed_does_not_trigger_instant_quest():
    """/change_time на слот, который сегодня уже был: ждём следующего дня, как и раньше."""
    async def sc(s):
        await s.make_user(1, slot="morning")                 # 05:00 локально = 00:00 UTC
        await s.make_user(2, slot="day")                     # чтобы слот 'day' отработал штатно
        await s.until(t(8, 10))
        await s.db.set_time_of_day(1, "day")                 # в 13:10 локально меняет утро -> день
        await s.until(t(11))                                 # окно досылки слота 'day' ещё открыто
        assert await s.rec(1, slot="day") is None and s.bot.card_attempt_count(1) == 0
        await s.until(t(8, 5, day=23))
        assert (await s.rec(1, slot="day", day=D23))["status"] == "delivered"
    sim.run(sc)


# ---------------------------------------------------------------- реальные обработчики /stop и /start


def _call(uid, bot, data):
    from unittest.mock import AsyncMock, MagicMock
    call = MagicMock()
    call.data = data
    call.from_user.id = uid
    call.bot = bot
    call.message.chat.id = uid
    call.message.message_id = 4242
    call.message.delete = AsyncMock()
    call.answer = AsyncMock()
    return call


def _message(uid):
    from unittest.mock import MagicMock
    m = MagicMock()
    m.from_user.id = uid
    m.from_user.username = f"u{uid}"
    m.from_user.first_name = f"U{uid}"
    m.chat.id = uid
    return m


def test_real_stop_handler_with_undelivered_card_then_real_start():
    async def sc(s):
        from bot.handlers import start as h
        await s.make_user(1)
        fail_cards(s.bot, 1, net_error, times=None)
        await s.until(t(8, 30))
        assert (await s.rec(1))["status"] == "pending"

        await h.on_stop_system_confirmed(_call(1, s.bot, "stopsys_yes:1"))   # реальный /stop
        assert (await s.db.get_user(1))["is_stopped"] == 1
        assert [c["status"] for c in await s.challenges(1)] == ["stopped"]
        assert await s.db._conn.execute_fetchall("SELECT 1 FROM daily_stats") == []

        n = s.bot.card_attempt_count(1)
        await s.until(t(9, 5))
        rec = await s.rec(1)
        assert rec["status"] == "skipped" and rec["reason"] == "stopped"
        assert s.bot.card_attempt_count(1) == n

        s.bot.hooks.clear()
        await h.cmd_start(_message(1), s.bot)                                 # реальный /start
        assert (await s.db.get_user(1))["is_stopped"] == 0
        await s.until(t(13))
        assert s.bot.card_attempt_count(1) == n                               # в тот же день квест не приходит
        await s.until(t(8, 5, day=23))
        assert (await s.rec(1, day=D23))["status"] == "delivered"
    sim.run(sc)


def test_real_start_handler_delivers_pending_card():
    async def sc(s):
        from bot.handlers import start as h
        await s.make_user(1)
        fail_cards(s.bot, 1, net_error, times=3)
        await s.until(t(8, 30))
        assert not s.bot.cards_alive(1)
        await h.cmd_start(_message(1), s.bot)
        assert len(s.bot.cards_alive(1)) == 1
        await s.until(t(9, 5))
        rec = await s.rec(1)
        assert rec["status"] == "delivered" and rec["reason"] == "delivered_elsewhere"
        assert len(s.bot.cards_alive(1)) == 1 and len(await s.challenges(1)) == 1
    sim.run(sc)


def test_real_stop_handler_on_delivered_card_still_records_stats():
    async def sc(s):
        from bot.handlers import start as h
        await s.make_user(1)
        await s.until(t(8, 5))
        await h.on_stop_system_confirmed(_call(1, s.bot, "stopsys_yes:1"))
        rows = await s.db._conn.execute_fetchall("SELECT user_id FROM daily_stats")
        assert len(rows) == 1                                                  # прежнее поведение сохранено
    sim.run(sc)
