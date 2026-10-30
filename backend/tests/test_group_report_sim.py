"""
Симуляции трёх доработок:
  1) кнопка «🏳 Сдаться» исчезает, когда закрыты все ежедневные цели;
  2) отчёт в группу (включая фото) уходит ТОЛЬКО после завершения испытания (ручного/авто);
  3) при провале (таймаут или «Сдаться») в группу уходит сообщение о поражении, штрафе и сбросе серии.
Слот 'day' = 13:00 Алматы = 08:00 UTC; карточка приходит в 08:00, дедлайн - 02:00 UTC следующих суток.
Запуск:  cd backend && python -m pytest tests/test_group_report_sim.py -q
"""
import asyncio
import os
import sqlite3
import tempfile
from unittest.mock import MagicMock

from tests import sim
from tests.sim import forbidden, net_error
from tests.test_completion_guide_sim import (
    DEADLINE,
    GROUP,
    _call,
    _message,
    group_texts,
    log,
    profile_text,
    setup_quest,
    setup_quests,
    status_of,
    t,
)
from tests.test_ease_sim import callbacks, ease_yes, setup_ease_quest

PUSHUPS_TARGET = 10  # цель на 1 уровне


# ---------------------------------------------------------------- помощники


async def send_photo(s, uid=1, file_id="P"):
    """Фото реальным хендлером; возвращает мок сообщения (для проверки удаления)."""
    from bot.handlers import challenge as h
    m = _message(uid, s.bot)
    m.photo = [MagicMock(file_id=file_id)]
    await h.on_photo_received(m, s.bot)
    return m


async def finish(s, ch, uid=1):
    from bot.handlers import challenge as h
    call = _call(uid, s.bot, f"finish:{ch['id']}")
    await h.on_finish_challenge(call)
    return call


async def press(s, handler, ch, prefix, uid=1):
    from bot.handlers import challenge as h
    call = _call(uid, s.bot, f"{prefix}:{ch['id']}")
    await getattr(h, handler)(call)
    return call


async def give_up_flow(s, ch, uid=1):
    """Реальные клики: «Сдаться» -> «Да»."""
    await press(s, "on_give_up_request", ch, "giveup", uid)
    return await press(s, "on_give_up_confirmed", ch, "giveup_yes", uid)


def has_giveup(s, ch, uid=1):
    return any(cb.startswith("giveup:") for cb in callbacks(s, ch, uid))


def alert(call):
    return call.answer.await_args.args[0] if call.answer.await_args and call.answer.await_args.args else None


def failure_reports(s, group=GROUP):
    return [x for x in group_texts(s, group) if "потерпел поражение" in x]


def completion_reports(s, group=GROUP):
    return [x for x in group_texts(s, group) if "завершил испытание" in x]


def fail_method(bot, method, chat_id, exc_factory):
    """Все вызовы метода в чат падают с ошибкой."""
    def hook(m, cid, kw):
        if m == method and cid == chat_id:
            raise exc_factory()
    bot.hooks.append(hook)


async def user(s, uid=1):
    return await s.db.get_user(uid)


# ================================================================ 1. кнопка «Сдаться»


def test_giveup_button_shown_while_goals_open_and_hidden_when_all_closed():
    async def sc(s):
        ch = await setup_quest(s, focuses=("pushups", "chess"))
        assert has_giveup(s, ch)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)       # одна из двух закрыта
        assert has_giveup(s, ch)
        assert not any(cb.startswith("finish:") for cb in callbacks(s, ch))
        await log(s, 1, ch["id"], "chess", 1)                       # закрыты все
        assert not has_giveup(s, ch)
        assert any(cb.startswith("finish:") for cb in callbacks(s, ch))
    sim.run(sc)


def test_giveup_button_returns_if_progress_rolled_back_below_target():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        assert not has_giveup(s, ch)
        await log(s, 1, ch["id"], "pushups", -1)
        assert has_giveup(s, ch)
        assert not any(cb.startswith("finish:") for cb in callbacks(s, ch))
    sim.run(sc)


def test_giveup_button_stays_hidden_after_overachievement_and_timer_redraws():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET * 2)   # x2 - всё равно закрыто
        assert not has_giveup(s, ch)
        await s.until(t(9, 30))                                    # таймерные перерисовки
        assert not has_giveup(s, ch)
        await s.until(t(1, 50, day=23))                            # финальный обратный отсчёт
        assert not has_giveup(s, ch)
        assert any(cb.startswith("finish:") for cb in callbacks(s, ch))
    sim.run(sc)


def test_giveup_button_hidden_when_easing_closes_all_goals():
    async def sc(s):
        ch = await setup_ease_quest(s)                              # цель 50
        await log(s, 1, ch["id"], "pushups", 30)
        assert has_giveup(s, ch)
        await ease_yes(s, ch)                                       # цель 25 -> закрыто
        assert not has_giveup(s, ch)
        assert any(cb.startswith("finish:") for cb in callbacks(s, ch))
    sim.run(sc)


def test_stale_giveup_click_after_all_goals_closed_is_rejected_without_penalty():
    async def sc(s):
        ch = await setup_quest(s)
        await s.db._conn.execute("UPDATE users SET xp = 500, streak = 3 WHERE user_id = 1")
        await s.db._conn.commit()
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        call = await press(s, "on_give_up_request", ch, "giveup")   # клиент ещё показывал старую кнопку
        assert call.answer.await_args.kwargs.get("show_alert") is True and "сдаться уже нельзя" in alert(call)
        assert await status_of(s, ch["id"]) == "awaiting_action"
        u = await user(s)
        assert (u["xp"], u["streak"], u["consecutive_failures"], u["penalty_until"]) == (500, 3, 0, None)
        assert group_texts(s) == [] and not has_giveup(s, ch)
        await finish(s, ch)                                         # завершить по-прежнему можно
        assert await status_of(s, ch["id"]) == "completed"
    sim.run(sc)


def test_confirm_screen_open_then_last_goal_closed_then_yes_is_rejected():
    async def sc(s):
        from bot.handlers import challenge as h
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", 5)
        await press(s, "on_give_up_request", ch, "giveup")          # экран «Сдаться?»
        assert s.bot.alive[(1, ch["message_id"])] == "Сдаться?"
        # Игрок закрывает цель, пока висит экран подтверждения (число вводится из чата).
        await h.on_focus_picked(_call(1, s.bot, f"focus:{ch['id']}:pushups"))
        await h.on_amount_logged(_message(1, s.bot, text="5"))
        assert s.bot.alive[(1, ch["message_id"])] != "Сдаться?"      # карточка вернулась
        call = await press(s, "on_give_up_confirmed", ch, "giveup_yes")   # устаревшее «Да»
        assert call.answer.await_args.kwargs.get("show_alert") is True
        assert await status_of(s, ch["id"]) == "awaiting_action"
        u = await user(s)
        assert (u["xp"], u["consecutive_failures"], u["penalty_until"]) == (0, 0, None)
        assert group_texts(s) == [] and not has_giveup(s, ch)
        assert s.bot.alive.get((1, ch["message_id"])) is not None   # карточка жива, не удалена
    sim.run(sc)


def test_giveup_still_works_normally_with_open_goals_and_gives_failure_flow():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", 3)
        await give_up_flow(s, ch)
        assert await status_of(s, ch["id"]) == "gave_up"
        u = await user(s)
        assert u["consecutive_failures"] == 1 and u["penalty_until"]
        assert (1, ch["message_id"]) not in s.bot.alive              # карточка удалена
    sim.run(sc)


def test_db_give_up_guard_is_atomic_and_conditional():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        assert await s.db.give_up_challenge(ch["id"]) is False       # все цели закрыты
        assert await status_of(s, ch["id"]) == "awaiting_action"
        await log(s, 1, ch["id"], "pushups", -1)
        assert await s.db.give_up_challenge(ch["id"]) is True
        assert await s.db.give_up_challenge(ch["id"]) is False       # повторно - нет
    sim.run(sc)

    async def sc2(s):                                                # испытание без строк прогресса сдать можно
        ch = await setup_quest(s)
        await s.db._conn.execute("DELETE FROM challenge_progress WHERE challenge_id = ?", (ch["id"],))
        await s.db._conn.commit()
        assert await s.db.give_up_challenge(ch["id"]) is True
    sim.run(sc2)


def test_foreign_user_cannot_give_up_someone_elses_challenge():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", 3)
        await press(s, "on_give_up_confirmed", ch, "giveup_yes", uid=777)
        assert await status_of(s, ch["id"]) == "awaiting_action"
        assert group_texts(s) == []
    sim.run(sc)


# ================================================================ 2. отчёт только после завершения


def test_photo_is_stored_silently_nothing_goes_to_group_and_prompt_disappears():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        assert "📸 Пришли фото" in s.bot.alive[(1, ch["message_id"])]
        assert any(cb.startswith("skipphysicalphoto:") for cb in callbacks(s, ch))
        m = await send_photo(s, file_id="F1")
        m.delete.assert_awaited()                                    # сообщение с фото убрано из чата
        row = await s.db.get_challenge(ch["id"])
        assert (row["physical_photo_done"], row["physical_photo_file_id"], row["physical_photo_posted"]) == (1, "F1", 0)
        assert s.bot.sent("send_photo", GROUP) == [] and group_texts(s) == []
        assert "📸 Пришли фото" not in s.bot.alive[(1, ch["message_id"])]
        assert not any(cb.startswith("skipphysicalphoto:") for cb in callbacks(s, ch))
        assert await status_of(s, ch["id"]) == "awaiting_action"
    sim.run(sc)


def test_manual_finish_sends_photo_with_caption_exactly_once():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        await send_photo(s, file_id="F1")
        await finish(s, ch)
        sent = s.bot.sent("send_photo", GROUP)
        assert len(sent) == 1 and sent[0]["photo"] == "F1"
        assert sent[0]["caption"] == "Отчёт дня 1. Игрок 1 завершил испытание."
        assert group_texts(s) == []
        assert await status_of(s, ch["id"]) == "completed_with_photo"
        assert (await s.db.get_challenge(ch["id"]))["physical_photo_posted"] == 1
        assert (await user(s))["xp"] == 100
        await s.until(DEADLINE)                                      # дальше никаких дублей
        assert len(s.bot.sent("send_photo", GROUP)) == 1 and group_texts(s) == []
    sim.run(sc)


def test_report_not_sent_before_manual_finish_even_with_all_goals_and_photo():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        await send_photo(s)
        await s.until(t(12, 0))                                      # часы идут, игрок не завершает
        assert s.bot.sent("send_photo", GROUP) == [] and group_texts(s) == []
    sim.run(sc)


def test_auto_finish_with_stored_photo_sends_photo_not_text():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        await send_photo(s, file_id="F9")
        await s.until(DEADLINE)
        assert await status_of(s, ch["id"]) == "completed_with_photo"
        sent = s.bot.sent("send_photo", GROUP)
        assert len(sent) == 1 and sent[0]["photo"] == "F9"
        assert group_texts(s) == []
    sim.run(sc)


def test_manual_finish_without_photo_stays_silent_as_before_but_auto_finish_sends_text():
    async def sc(s):
        chs = await setup_quests(s, [(1, ("pushups",), GROUP), (2, ("pushups",), GROUP)])
        await log(s, 1, chs[1]["id"], "pushups", PUSHUPS_TARGET)
        await log(s, 2, chs[2]["id"], "pushups", PUSHUPS_TARGET)
        await press(s, "on_skip_physical_photo", chs[1], "skipphysicalphoto", uid=1)
        await finish(s, chs[1], uid=1)                               # ручное завершение, фото пропущено
        assert group_texts(s) == [] and s.bot.sent("send_photo", GROUP) == []
        assert await status_of(s, chs[1]["id"]) == "completed"
        await s.until(DEADLINE)                                      # игрок 2 забыл завершить -> авто, текст
        assert group_texts(s) == ["Отчёт дня 1. Игрок 2 завершил испытание."]
    sim.run(sc)


def test_finish_while_photo_still_pending_sends_no_report():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)         # фото не прислал и не пропустил
        await finish(s, ch)
        assert await status_of(s, ch["id"]) == "completed"
        assert group_texts(s) == [] and s.bot.sent("send_photo", GROUP) == []
    sim.run(sc)


def test_give_up_after_photo_never_posts_photo_only_failure_message():
    async def sc(s):
        ch = await setup_quest(s, focuses=("pushups", "chess"))
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)         # шахматы открыты -> сдаться можно
        await send_photo(s)
        await give_up_flow(s, ch)
        assert await status_of(s, ch["id"]) == "gave_up"
        assert s.bot.sent("send_photo", GROUP) == []
        assert completion_reports(s) == [] and len(failure_reports(s)) == 1
        row = await s.db.get_challenge(ch["id"])
        assert row["physical_photo_posted"] == 0
        await s.until(DEADLINE)
        assert s.bot.sent("send_photo", GROUP) == [] and len(group_texts(s)) == 1
    sim.run(sc)


def test_timeout_failure_after_photo_never_posts_photo():
    async def sc(s):
        ch = await setup_quest(s, focuses=("pushups", "chess"))
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        await send_photo(s)
        await s.until(DEADLINE)
        assert await status_of(s, ch["id"]) == "expired"
        assert s.bot.sent("send_photo", GROUP) == []
        assert completion_reports(s) == [] and len(failure_reports(s)) == 1
    sim.run(sc)


def test_photo_ignored_until_physical_goals_closed():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", 5)
        m = await send_photo(s)
        m.delete.assert_not_awaited()
        row = await s.db.get_challenge(ch["id"])
        assert (row["physical_photo_done"], row["physical_photo_file_id"]) == (0, None)
    sim.run(sc)


def test_mental_only_challenge_has_no_photo_flow_and_text_report_on_finish_is_not_sent_manually():
    async def sc(s):
        ch = await setup_quest(s, focuses=("chess",))
        await log(s, 1, ch["id"], "chess", 1)
        m = await send_photo(s)
        m.delete.assert_not_awaited()                                # фото не ожидается
        assert "📸" not in s.bot.alive[(1, ch["message_id"])]
        assert not has_giveup(s, ch)
        await finish(s, ch)
        assert group_texts(s) == []                                  # ручное завершение без фото - тишина
    sim.run(sc)


def test_second_photo_is_ignored_and_first_is_kept():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        await send_photo(s, file_id="FIRST")
        m2 = await send_photo(s, file_id="SECOND")
        m2.delete.assert_not_awaited()
        assert (await s.db.get_challenge(ch["id"]))["physical_photo_file_id"] == "FIRST"
        await finish(s, ch)
        assert [x["photo"] for x in s.bot.sent("send_photo", GROUP)] == ["FIRST"]
    sim.run(sc)


def test_simultaneous_photos_store_only_one():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        from bot.handlers import challenge as h
        ms = []
        for fid in ("A", "B", "C"):
            m = _message(1, s.bot)
            m.photo = [MagicMock(file_id=fid)]
            ms.append(m)
        await asyncio.gather(*(h.on_photo_received(m, s.bot) for m in ms))
        stored = (await s.db.get_challenge(ch["id"]))["physical_photo_file_id"]
        assert stored in ("A", "B", "C")
        # Принято и убрано из чата ровно одно сообщение с фото; остальные проигнорированы.
        assert sum(m.delete.await_count for m in ms) == 1
        await finish(s, ch)
        assert len(s.bot.sent("send_photo", GROUP)) == 1
        assert s.bot.sent("send_photo", GROUP)[0]["photo"] == stored
    sim.run(sc)


def test_photo_after_skip_is_ignored_and_skip_after_photo_keeps_photo():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        await press(s, "on_skip_physical_photo", ch, "skipphysicalphoto")
        m = await send_photo(s, file_id="LATE")
        m.delete.assert_not_awaited()
        assert (await s.db.get_challenge(ch["id"]))["physical_photo_file_id"] is None
    sim.run(sc)

    async def sc2(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        await send_photo(s, file_id="KEPT")
        await press(s, "on_skip_physical_photo", ch, "skipphysicalphoto")   # устаревшая кнопка
        await finish(s, ch)
        assert [x["photo"] for x in s.bot.sent("send_photo", GROUP)] == ["KEPT"]
    sim.run(sc2)


def test_photo_survives_rollback_below_target_and_goes_out_on_completion():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        await send_photo(s, file_id="F")
        await log(s, 1, ch["id"], "pushups", -1)                     # откат ниже цели
        assert s.bot.sent("send_photo", GROUP) == []
        await log(s, 1, ch["id"], "pushups", 1)                      # снова закрыто; новое фото не просят
        assert "📸" not in s.bot.alive[(1, ch["message_id"])]
        await finish(s, ch)
        assert [x["photo"] for x in s.bot.sent("send_photo", GROUP)] == ["F"]
    sim.run(sc)


def test_secret_line_is_evaluated_at_completion_not_at_photo_time():
    async def sc(s):
        # Физический x2 (шахматы закрыты на 1x, значит общий бонус не выдан): личный флаг по
        # отжиманиям снимается откатом, и строки про секретное испытание в отчёте быть не должно.
        ch = await setup_quest(s, focuses=("pushups", "chess"))
        await log(s, 1, ch["id"], "chess", 1)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET * 2)     # x2 на момент фото
        await send_photo(s)
        await log(s, 1, ch["id"], "pushups", -5)                     # откатил ниже x2 до завершения
        await finish(s, ch)
        assert "🌟" not in s.bot.sent("send_photo", GROUP)[0]["caption"]
    sim.run(sc)

    async def sc_one_time(s):
        # Общий секретный бонус (x2 по ВСЕМ дисциплинам) одноразовый и не откатывается - как и раньше.
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET * 2)
        await send_photo(s)
        await log(s, 1, ch["id"], "pushups", -5)
        await finish(s, ch)
        assert "🌟" in s.bot.sent("send_photo", GROUP)[0]["caption"]
        from bot.config import BONUS_XP_MULTIPLIER
        assert (await user(s))["xp"] == 100 + 100 * BONUS_XP_MULTIPLIER   # бонусный XP действительно выдан
    sim.run(sc_one_time)

    async def sc2(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        await send_photo(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)         # дожал до x2 уже после фото
        await finish(s, ch)
        assert s.bot.sent("send_photo", GROUP)[0]["caption"] == (
            "Отчёт дня 1. Игрок 1 завершил испытание.\n\n🌟 В день 1 также пройдено секретное испытание."
        )
    sim.run(sc2)


def test_double_finish_and_finish_vs_auto_complete_send_one_report():
    async def sc(s):
        from bot import scheduler
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        await send_photo(s)
        row = await s.db.get_challenge(ch["id"])
        await asyncio.gather(finish(s, ch), finish(s, ch), scheduler._expire_one(s.bot, row))
        assert len(s.bot.sent("send_photo", GROUP)) == 1 and group_texts(s) == []
        assert (await user(s))["xp"] == 100 and (await user(s))["streak"] == 1
    sim.run(sc)


def test_group_bound_after_photo_receives_report_at_completion():
    async def sc(s):
        ch = await setup_quest(s, group=None)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        await send_photo(s, file_id="F")                             # группы нет - фото всё равно сохранено
        assert (await s.db.get_challenge(ch["id"]))["physical_photo_file_id"] == "F"
        await s.db.set_group(1, GROUP)
        await finish(s, ch)
        assert [x["photo"] for x in s.bot.sent("send_photo", GROUP)] == ["F"]
    sim.run(sc)


def test_group_unbound_after_photo_completes_without_error_or_report():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        await send_photo(s)
        await s.db._conn.execute("UPDATE users SET group_id = NULL WHERE user_id = 1")
        await s.db._conn.commit()
        await finish(s, ch)
        assert await status_of(s, ch["id"]) == "completed_with_photo"
        assert s.bot.sent("send_photo", GROUP) == [] and group_texts(s) == []
        assert (await user(s))["xp"] == 100
    sim.run(sc)


def test_photo_send_failure_falls_back_to_text_report_and_completion_is_intact():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        await send_photo(s)
        fail_method(s.bot, "send_photo", GROUP, net_error)
        await finish(s, ch)
        assert group_texts(s) == ["Отчёт дня 1. Игрок 1 завершил испытание."]
        assert (await s.db.get_challenge(ch["id"]))["physical_photo_posted"] == 0
        assert await status_of(s, ch["id"]) == "completed_with_photo"
        u = await user(s)
        assert (u["xp"], u["streak"]) == (100, 1)
    sim.run(sc)


def test_photo_send_flaky_then_ok_retries_and_sends_photo_once():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        await send_photo(s)
        state = {"left": 2}

        def hook(m, cid, kw):
            if m == "send_photo" and cid == GROUP and state["left"] > 0:
                state["left"] -= 1
                raise net_error()
        s.bot.hooks.append(hook)
        await finish(s, ch)
        assert len(s.bot.sent("send_photo", GROUP)) == 1 and group_texts(s) == []
        assert (await s.db.get_challenge(ch["id"]))["physical_photo_posted"] == 1
    sim.run(sc)


def test_group_forbidden_does_not_break_completion_and_no_text_attempt_after_photo():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        await send_photo(s)
        s.bot.blocked.add(GROUP)                                     # бота выгнали из группы
        await finish(s, ch)
        assert await status_of(s, ch["id"]) == "completed_with_photo"
        u = await user(s)
        assert (u["xp"], u["streak"]) == (100, 1)
        assert "✅ Вы выполнили сегодняшний квест." in await profile_text(s, 1)
    sim.run(sc)


def test_eased_challenge_photo_report_has_note_at_completion():
    async def sc(s):
        ch = await setup_ease_quest(s, focuses=("pushups",))
        await ease_yes(s, ch)
        await log(s, 1, ch["id"], "pushups", 25)
        await send_photo(s)
        assert s.bot.sent("send_photo", GROUP) == []
        await finish(s, ch)
        assert s.bot.sent("send_photo", GROUP)[0]["caption"].endswith("📉 Сложность в этот день была понижена.")
    sim.run(sc)


def test_stopped_challenge_with_photo_sends_nothing_to_group():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        await send_photo(s)
        await s.db.stop_challenge(ch["id"])
        await s.until(DEADLINE)
        assert s.bot.sent("send_photo", GROUP) == [] and group_texts(s) == []
    sim.run(sc)


def test_next_day_gets_its_own_photo_and_report_day_numbers_continue():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        await send_photo(s, file_id="D1")
        await finish(s, ch)
        await s.until(t(8, 5, day=23))
        ch2 = await s.db.get_active_challenge(1)
        assert ch2 and ch2["id"] != ch["id"] and ch2["physical_photo_file_id"] is None
        await log(s, 1, ch2["id"], "pushups", 11)
        await send_photo(s, file_id="D2")
        await finish(s, ch2)
        caps = [(x["photo"], x["caption"]) for x in s.bot.sent("send_photo", GROUP)]
        assert caps[0] == ("D1", "Отчёт дня 1. Игрок 1 завершил испытание.")
        assert caps[1][0] == "D2" and caps[1][1].startswith("Отчёт дня 2. Игрок 1")
    sim.run(sc)


# ================================================================ 3. сообщение о поражении в группу


def _expected_failure(uid, streak):
    from bot import messages as tx
    from bot.config import settings
    return tx.group_failure_report(
        uid, xp_loss=settings.missed_day_xp_penalty, penalty_hours=settings.penalty_hours, streak_lost=streak
    )


def test_failure_message_text_format():
    from bot import messages as tx
    text = tx.group_failure_report(42, xp_loss=300, penalty_hours=48, streak_lost=7)
    assert "Игрок 42 потерпел поражение." in text
    assert "▸ Опыт: −300 XP" in text
    assert "▸ Серия из 7 дней: сброшена" in text
    assert "▸ Ограничение доступа: 48 ч" in text
    assert "▸ Серия дней: сброшена" in tx.group_failure_report(42, 300, 48, 0)
    assert "Серия из 1 дня:" in tx.group_failure_report(42, 300, 48, 1)
    assert "Серия из 21 дня:" in tx.group_failure_report(42, 300, 48, 21)
    assert "Серия из 11 дней:" in tx.group_failure_report(42, 300, 48, 11)
    assert max(len(line) for line in text.splitlines()) < 40       # влезает в узкий пузырь


def test_timeout_failure_sends_group_message_once_with_lost_streak():
    async def sc(s):
        ch = await setup_quest(s)
        await s.db._conn.execute("UPDATE users SET xp = 500, streak = 6 WHERE user_id = 1")
        await s.db._conn.commit()
        await log(s, 1, ch["id"], "pushups", 4)
        await s.until(DEADLINE)
        assert await status_of(s, ch["id"]) == "expired"
        assert group_texts(s) == [_expected_failure(1, 6)]
        assert "Серия из 6 дней: сброшена" in group_texts(s)[0]
        assert (await user(s))["streak"] == 0
        await s.until(t(8, 5, day=24))                               # позже ничего не дублируется
        assert len(failure_reports(s)) == 1
    sim.run(sc)


def test_give_up_failure_sends_group_message_with_lost_streak_and_card_to_player():
    async def sc(s):
        ch = await setup_quest(s)
        await s.db._conn.execute("UPDATE users SET xp = 500, streak = 1 WHERE user_id = 1")
        await s.db._conn.commit()
        await log(s, 1, ch["id"], "pushups", 4)
        await give_up_flow(s, ch)
        assert group_texts(s) == [_expected_failure(1, 1)]
        assert "Серия из 1 дня: сброшена" in group_texts(s)[0]
        personal = [kw["text"] for kw in s.bot.sent("send_message", 1) if "ПРОВАЛЕН" in kw["text"]]
        assert len(personal) == 1                                    # личная карточка провала по-прежнему приходит
    sim.run(sc)


def test_failure_on_zero_streak_says_streak_reset_without_number():
    async def sc(s):
        ch = await setup_quest(s)
        await give_up_flow(s, ch)
        assert group_texts(s) == [_expected_failure(1, 0)]
        assert "▸ Серия дней: сброшена" in group_texts(s)[0]
    sim.run(sc)


def test_failure_without_group_sends_nothing_and_does_not_break_penalty():
    async def sc(s):
        ch = await setup_quest(s, group=None)
        await log(s, 1, ch["id"], "pushups", 4)
        await s.until(DEADLINE)
        assert await status_of(s, ch["id"]) == "expired"
        u = await user(s)
        assert u["consecutive_failures"] == 1 and u["penalty_until"]
        assert [m for m in s.bot.log if m[1] == GROUP] == []
    sim.run(sc)


def test_failure_in_one_group_does_not_leak_into_another():
    async def sc(s):
        chs = await setup_quests(s, [(1, ("pushups",), GROUP), (2, ("pushups",), -777)])
        await log(s, 2, chs[2]["id"], "pushups", PUSHUPS_TARGET)
        await finish(s, chs[2], uid=2)
        await s.until(DEADLINE)                                      # игрок 1 провалил
        assert len(failure_reports(s, GROUP)) == 1
        assert failure_reports(s, -777) == []
    sim.run(sc)


def test_group_down_during_failure_still_applies_penalty_card_and_profile():
    async def sc(s):
        ch = await setup_quest(s)
        await s.db._conn.execute("UPDATE users SET xp = 500, streak = 3 WHERE user_id = 1")
        await s.db._conn.commit()
        await log(s, 1, ch["id"], "pushups", 4)
        fail_method(s.bot, "send_message", GROUP, net_error)
        await s.until(DEADLINE)
        u = await user(s)
        assert await status_of(s, ch["id"]) == "expired"
        assert (u["xp"], u["streak"], u["consecutive_failures"]) == (200, 0, 1) and u["penalty_until"]
        assert any("ПРОВАЛЕН" in kw["text"] for kw in s.bot.sent("send_message", 1))
        assert group_texts(s) == []
    sim.run(sc)

    async def sc2(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", 4)
        s.bot.blocked.add(GROUP)                                     # бот выгнан из группы
        await give_up_flow(s, ch)
        assert await status_of(s, ch["id"]) == "gave_up"
        assert (await user(s))["consecutive_failures"] == 1
    sim.run(sc2)


def test_group_message_flaky_then_ok_is_retried_without_duplicates():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", 4)
        state = {"left": 2}

        def hook(m, cid, kw):
            if m == "send_message" and cid == GROUP and state["left"] > 0:
                state["left"] -= 1
                raise net_error()
        s.bot.hooks.append(hook)
        await give_up_flow(s, ch)
        assert len(failure_reports(s)) == 1
    sim.run(sc)


def test_player_blocked_bot_in_private_chat_group_still_learns_about_defeat():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", 4)
        s.bot.blocked.add(1)
        await s.until(DEADLINE)
        assert await status_of(s, ch["id"]) == "expired"
        assert len(failure_reports(s)) == 1
    sim.run(sc)


def test_double_yes_and_giveup_vs_timeout_race_send_exactly_one_failure_message():
    async def sc(s):
        from bot import scheduler
        from bot.handlers import challenge as h
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", 4)
        row = await s.db.get_challenge(ch["id"])
        await asyncio.gather(
            h._execute_give_up(s.bot, row),
            h._execute_give_up(s.bot, row),
            scheduler._expire_one(s.bot, row),
        )
        assert len(failure_reports(s)) == 1
        u = await user(s)
        assert u["consecutive_failures"] == 1
    sim.run(sc)


def test_successful_completion_never_sends_defeat_message():
    async def sc(s):
        chs = await setup_quests(s, [(1, ("pushups",), GROUP), (2, ("pushups",), GROUP)])
        await log(s, 1, chs[1]["id"], "pushups", PUSHUPS_TARGET)
        await log(s, 2, chs[2]["id"], "pushups", PUSHUPS_TARGET)
        await send_photo(s, uid=1)
        await finish(s, chs[1], uid=1)
        await s.until(DEADLINE)                                      # игрок 2 - авто-завершение
        assert failure_reports(s) == []
    sim.run(sc)


def test_two_failures_in_a_row_send_two_messages_and_auto_stop_still_works():
    async def sc(s):
        ch = await setup_quest(s)
        await s.db._conn.execute("UPDATE users SET xp = 5000, streak = 4 WHERE user_id = 1")
        await s.db._conn.commit()
        await log(s, 1, ch["id"], "pushups", 3)
        await give_up_flow(s, ch)                                    # провал 1 (серия 4)
        await s.until(t(8, 5, day=25))                               # штраф 48 ч прошёл, новый квест
        ch2 = await s.db.get_active_challenge(1)
        assert ch2 and ch2["id"] != ch["id"]
        await give_up_flow(s, ch2)                                   # провал 2 -> автостоп
        u = await user(s)
        assert u["is_stopped"] and u["consecutive_failures"] == 2
        reports = failure_reports(s)
        assert len(reports) == 2
        assert "Серия из 4 дней: сброшена" in reports[0] and "▸ Серия дней: сброшена" in reports[1]
    sim.run(sc)


def test_stale_giveup_after_all_goals_closed_sends_no_defeat_message():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        await give_up_flow(s, ch)
        assert failure_reports(s) == [] and await status_of(s, ch["id"]) == "awaiting_action"
    sim.run(sc)


# ================================================================ миграция старой БД


def test_migration_adds_photo_file_id_column_to_an_old_database():
    async def run():
        import bot.database as database
        from bot.database import Database
        path = os.path.join(tempfile.mkdtemp(), "old.sql")
        con = sqlite3.connect(path)
        con.executescript(
            """CREATE TABLE challenges (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, status TEXT,
               quest_text TEXT, active_focus TEXT, bonus_claimed INTEGER DEFAULT 0,
               physical_photo_done INTEGER DEFAULT 0, physical_photo_posted INTEGER DEFAULT 0,
               started_at TEXT, completed_at TEXT, message_id INTEGER);
               INSERT INTO challenges (user_id, status, physical_photo_posted) VALUES (1, 'completed_with_photo', 1);"""
        )
        con.commit()
        con.close()
        d = Database(path)
        await d.connect()
        cur = await d._conn.execute("PRAGMA table_info(challenges)")
        cols = {r[1] for r in await cur.fetchall()}
        assert "physical_photo_file_id" in cols
        cur = await d._conn.execute("SELECT status, physical_photo_posted, physical_photo_file_id FROM challenges")
        row = await cur.fetchone()
        assert tuple(row) == ("completed_with_photo", 1, None)       # старые данные не тронуты
        await d.close()
    asyncio.run(run())
