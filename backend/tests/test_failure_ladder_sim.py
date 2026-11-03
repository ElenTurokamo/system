"""
Симуляции: лестница провалов (1-й - заморозка серии, 2-й - штраф без остановки, 3-й - заморозка
Системы без штрафа), короткие уведомления, автоудаление через 5 минут и видео/кружок в отчёте.
Запуск:  cd backend && python -m pytest tests/test_failure_ladder_sim.py -q
"""
from datetime import timedelta
from unittest.mock import MagicMock

from tests import sim
from tests.sim import net_error
from tests.test_completion_guide_sim import DEADLINE, GROUP, _message, log, profile_text, setup_quest, t
from tests.test_freeze_state_sim import personal_failure_cards, set_user
from tests.test_group_report_sim import (
    PUSHUPS_TARGET, fail_method, failure_reports, finish, give_up_flow, group_texts, send_photo, user,
)

HEADER = "『 Уведомление Системы 』\nЕжедневное испытание провалено.\n\n"
FROZEN = HEADER + "🧊 Ваша серия была заморожена."
PENALTY = HEADER + "🚫 На вас был наложен штраф."
SYSTEM = HEADER + "❄️ Система была заморожена."


async def fail_three_times(s, third="give_up"):
    """Три провала подряд; возвращает (ch3, пользователь после 2-го провала)."""
    ch = await setup_quest(s)
    await set_user(s, xp=5000, streak=4)
    await give_up_flow(s, ch)                                       # 1: заморозка серии
    await s.until(t(8, 5, day=23))
    ch2 = await s.db.get_active_challenge(1)
    await give_up_flow(s, ch2)                                      # 2: штраф
    after2 = await user(s)
    await s.until(t(8, 5, day=26))                                  # штраф (48 ч) закончился
    ch3 = await s.db.get_active_challenge(1)
    assert ch3 and ch3["id"] not in (ch["id"], ch2["id"])
    if third == "give_up":
        await give_up_flow(s, ch3)
    else:
        await s.until(t(2, 5, day=27))                              # дедлайн третьего квеста
    return ch3, after2


def test_three_failures_in_a_row_ladder_give_up():
    async def sc(s):
        ch3, after2 = await fail_three_times(s)
        u = await user(s)
        assert (after2["is_stopped"], after2["xp"], after2["streak"]) == (0, 4700, 0) and after2["penalty_until"]
        # 3-й провал: Система заморожена, штрафа нет (XP, серия и penalty_until не тронуты)
        assert u["is_stopped"] == 1 and u["consecutive_failures"] == 3
        assert (u["xp"], u["streak"], u["penalty_until"]) == (4700, 0, after2["penalty_until"])
        assert personal_failure_cards(s) == [FROZEN, PENALTY, SYSTEM]
        reports = failure_reports(s)
        assert len(reports) == 3 and "Система заморожена" in reports[2] and "Опыт" not in reports[2]
        text = await profile_text(s, 1)
        assert text.endswith("❄️ Система заморожена. Для активации отправьте /start.")
        assert "🚫 Штраф" not in text
        await s.until(t(8, 5, day=27))                              # новые испытания не приходят
        assert await s.db.get_active_challenge(1) is None
    sim.run(sc)


def test_three_failures_in_a_row_ladder_timeout():
    async def sc(s):
        await fail_three_times(s, third="timeout")
        u = await user(s)
        assert u["is_stopped"] == 1 and u["consecutive_failures"] == 3 and u["xp"] == 4700
        assert personal_failure_cards(s)[-1] == SYSTEM
        assert "Система заморожена" in failure_reports(s)[-1]
    sim.run(sc)


def test_start_after_freeze_resumes_with_clean_counter():
    async def sc(s):
        from bot.handlers import start as st
        await fail_three_times(s)
        await st.cmd_start(_message(1, s.bot, text="/start"), s.bot)
        u = await user(s)
        assert (u["is_stopped"], u["consecutive_failures"], u["streak_frozen"]) == (0, 0, 0)
        assert "Система заморожена" not in await profile_text(s, 1)
        await s.until(t(8, 5, day=28))
        assert await s.db.get_active_challenge(1) is not None       # испытания снова приходят
    sim.run(sc)


def test_completed_quest_between_failures_resets_ladder():
    async def sc(s):
        ch = await setup_quest(s)
        await give_up_flow(s, ch)                                   # 1: заморозка
        await s.until(t(8, 5, day=23))
        ch2 = await s.db.get_active_challenge(1)
        await log(s, 1, ch2["id"], "pushups", PUSHUPS_TARGET)
        await finish(s, ch2)
        assert (await user(s))["consecutive_failures"] == 0
        await s.until(t(8, 5, day=24))
        await give_up_flow(s, await s.db.get_active_challenge(1))
        assert personal_failure_cards(s) == [FROZEN, FROZEN]         # снова только заморозка
    sim.run(sc)


def test_stop_threshold_two_skips_the_penalty_step():
    async def sc(s):
        ch = await setup_quest(s)
        await set_user(s, xp=500, streak=2)
        await give_up_flow(s, ch)
        await s.until(t(8, 5, day=23))
        await give_up_flow(s, await s.db.get_active_challenge(1))
        u = await user(s)
        assert u["is_stopped"] == 1 and u["xp"] == 500 and u["penalty_until"] is None
        assert personal_failure_cards(s) == [FROZEN, SYSTEM]
    sim.run(sc, auto_stop_after_failures=2)


def test_group_down_does_not_block_system_freeze():
    async def sc(s):
        await setup_quest(s)
        await set_user(s, consecutive_failures=2)
        fail_method(s.bot, "send_message", GROUP, net_error)
        await s.until(DEADLINE)
        u = await user(s)
        assert u["is_stopped"] == 1 and u["penalty_until"] is None and personal_failure_cards(s) == [SYSTEM]
        assert group_texts(s) == []
    sim.run(sc)


async def _wait_for_failure_notice(s):
    """Доводит симуляцию до первого уведомления о провале; возвращает (ключ сообщения, время отправки)."""
    await setup_quest(s)
    await s.until(t(2, 2, day=23))                                   # квест сгорает в 02:01 UTC
    u = await user(s)
    assert u["failure_message_id"], "уведомление о провале не отправлено"
    from datetime import datetime
    return (1, u["failure_message_id"]), datetime.fromisoformat(u["failure_sent_at"])


def test_failure_notice_is_deleted_after_five_minutes():
    async def sc(s):
        key, sent = await _wait_for_failure_notice(s)
        assert key in s.bot.alive and personal_failure_cards(s) == [FROZEN]
        await s.until(sent + timedelta(minutes=4))
        assert key in s.bot.alive                                    # ещё не прошло 5 минут
        await s.until(sent + timedelta(minutes=6))
        assert key not in s.bot.alive                                # удалено сразу после 5-й минуты
        assert (await user(s))["failure_message_id"] is None
    sim.run(sc)


def test_failure_notice_retried_after_network_error():
    async def sc(s):
        key, sent = await _wait_for_failure_notice(s)
        hooks_before = len(s.bot.hooks)
        fail_method(s.bot, "delete_message", 1, net_error)
        await s.until(sent + timedelta(minutes=8))
        assert key in s.bot.alive and (await user(s))["failure_message_id"]   # запись сохранена для повтора
        del s.bot.hooks[hooks_before:]                               # сеть вернулась
        await s.until(sent + timedelta(minutes=10))
        assert key not in s.bot.alive and (await user(s))["failure_message_id"] is None
    sim.run(sc)


async def _media(s, kind, file_id):
    from bot.handlers import challenge as h
    m = _message(1, s.bot)
    m.photo = m.video = m.video_note = None
    setattr(m, kind, MagicMock(file_id=file_id))
    await h.on_photo_received(m, s.bot)
    return m


def test_video_report_goes_to_group_with_caption():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        m = await _media(s, "video", "V1")
        assert m.delete.await_count == 1
        assert (await s.db.get_challenge(ch["id"]))["physical_media_type"] == "video"
        assert s.bot.sent("send_video", GROUP) == []                 # до завершения в группу ничего не уходит
        await finish(s, ch)
        sent = s.bot.sent("send_video", GROUP)
        assert len(sent) == 1 and sent[0]["video"] == "V1" and "завершил испытание" in sent[0]["caption"]
        assert s.bot.sent("send_photo", GROUP) == []
    sim.run(sc)


def test_video_note_report_goes_to_group_with_separate_text():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        await _media(s, "video_note", "N1")
        await finish(s, ch)
        notes = s.bot.sent("send_video_note", GROUP)
        assert len(notes) == 1 and notes[0]["video_note"] == "N1"
        assert any("завершил испытание" in x for x in group_texts(s))
    sim.run(sc)


def test_video_falls_back_to_text_when_group_rejects_media():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        await _media(s, "video", "V1")
        fail_method(s.bot, "send_video", GROUP, net_error)
        await finish(s, ch)
        assert s.bot.sent("send_video", GROUP) == []
        assert any("завершил испытание" in x for x in group_texts(s))   # отчёт ушёл текстом
    sim.run(sc)


def test_photo_still_works_and_first_media_wins_over_album():
    async def sc(s):
        ch = await setup_quest(s)
        await log(s, 1, ch["id"], "pushups", PUSHUPS_TARGET)
        await send_photo(s, file_id="P1")
        late = await _media(s, "video", "V2")                        # второй файл игнорируется
        assert late.delete.await_count == 0
        await finish(s, ch)
        assert s.bot.sent("send_photo", GROUP)[0]["photo"] == "P1" and s.bot.sent("send_video", GROUP) == []
    sim.run(sc)


def test_media_ignored_until_physical_goals_done():
    async def sc(s):
        ch = await setup_quest(s)
        m = await _media(s, "video_note", "N1")
        assert m.delete.await_count == 0
        assert (await s.db.get_challenge(ch["id"]))["physical_photo_file_id"] is None
    sim.run(sc)
