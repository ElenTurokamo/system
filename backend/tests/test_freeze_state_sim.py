"""
Симуляции двух доработок:
  1) заморозка серии: первый провал подряд прощается (🧊 вместо 🔥, сообщение о провале в группу,
     без штрафа и ограничения), полная кара - на втором провале подряд;
  2) дополнительная локальная БД состояния (bot/state_store.py): ключи сообщений профиля и
     отчёта о провале переживают откат db.sql из бэкапа, автоудаление - перезапуск, кэш
     file_id гайда - перезапуск и замену файла.
Слот 'day' = 13:00 Алматы = 08:00 UTC; карточка приходит в 08:00, дедлайн - 02:00 UTC следующих суток.
Запуск:  cd backend && python -m pytest tests/test_freeze_state_sim.py -q
"""
import asyncio
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import timedelta
from unittest.mock import MagicMock

from aiogram.exceptions import TelegramBadRequest

from tests import sim
from tests.sim import Clock, net_error
from tests.test_completion_guide_sim import (
    DEADLINE,
    GROUP,
    group_texts,
    log,
    profile_text,
    setup_quest,
    status_of,
    t,
)
from tests.test_group_report_sim import failure_reports, give_up_flow, user

FREEZE_MARK = "Сработала защита серии 🧊"


async def set_user(s, **cols):
    sets = ", ".join(f"{k} = ?" for k in cols)
    await s.db._conn.execute(f"UPDATE users SET {sets} WHERE user_id = 1", tuple(cols.values()))
    await s.db._conn.commit()


def personal_failure_cards(s, uid=1):
    return [kw["text"] for kw in s.bot.sent("send_message", uid) if "ПРОВАЛЕН" in kw["text"]]


def warnings_for(s, uid=1):
    return [kw["text"] for kw in s.bot.sent("send_message", uid) if "Предупреждение Системы" in kw["text"]]


# ================================================================ 1. заморозка серии


def test_failure_helper_rules():
    from bot.config import failure_is_forgiven, settings

    assert failure_is_forgiven(1) is True
    assert failure_is_forgiven(2) is False and failure_is_forgiven(3) is False
    assert failure_is_forgiven(0) is False                      # провала ещё не было
    old = settings.auto_stop_after_failures
    try:
        settings.auto_stop_after_failures = 1                   # любой провал останавливает Систему - прощения нет
        assert failure_is_forgiven(1) is False
        settings.auto_stop_after_failures = 5
        assert failure_is_forgiven(1) is True and failure_is_forgiven(2) is False
    finally:
        settings.auto_stop_after_failures = old


def test_first_timeout_failure_freezes_streak_without_penalty():
    async def sc(s):
        ch = await setup_quest(s)
        await set_user(s, xp=500, streak=6)
        await log(s, 1, ch["id"], "pushups", 4)
        await s.until(DEADLINE)
        assert await status_of(s, ch["id"]) == "expired"
        u = await user(s)
        # Ни XP, ни серия, ни доступ не тронуты; счётчик подряд = 1; серия заморожена.
        assert (u["xp"], u["streak"], u["streak_frozen"], u["consecutive_failures"]) == (500, 6, 1, 1)
        assert u["penalty_until"] is None and not u["is_stopped"]
        # Игроку - одна карточка провала в «замороженном» варианте.
        cards = personal_failure_cards(s)
        assert len(cards) == 1 and FREEZE_MARK in cards[0] and "Серия из 6 дней: заморожена" in cards[0]
        assert "Штраф и ограничения: не наложены" in cards[0] and "Наложена кара" not in cards[0]
        # В группу - ровно одно сообщение о поражении, тоже без штрафа.
        reports = failure_reports(s)
        assert len(reports) == 1 and FREEZE_MARK in reports[0]
        assert "Серия из 6 дней: заморожена" in reports[0] and "Наложена кара" not in reports[0]
        assert "Опыт: −" not in reports[0] and "Ограничение доступа" not in reports[0]
        # Профиль: кубик льда вместо огня, блока штрафа нет.
        text = await profile_text(s, 1)
        assert "🧊 Серия: 6 дней" in text and "🔥" not in text and "Вы не выполнили требование" not in text
    sim.run(sc)


def test_first_give_up_freezes_streak_without_penalty():
    async def sc(s):
        ch = await setup_quest(s)
        await set_user(s, xp=500, streak=2)
        await give_up_flow(s, ch)
        assert await status_of(s, ch["id"]) == "gave_up"
        u = await user(s)
        assert (u["xp"], u["streak"], u["streak_frozen"], u["consecutive_failures"]) == (500, 2, 1, 1)
        assert u["penalty_until"] is None and not u["is_stopped"]
        assert len(failure_reports(s)) == 1 and FREEZE_MARK in failure_reports(s)[0]
        assert "🧊 Серия: 2 дня" in await profile_text(s, 1)
    sim.run(sc)


def test_first_failure_with_zero_streak_is_forgiven_and_text_omits_streak_line():
    async def sc(s):
        ch = await setup_quest(s)
        await give_up_flow(s, ch)
        u = await user(s)
        assert (u["xp"], u["streak"], u["consecutive_failures"], u["penalty_until"]) == (0, 0, 1, None)
        report = failure_reports(s)[0]
        assert FREEZE_MARK in report and "Серия из" not in report and "Штраф и ограничения: не наложены" in report
        assert "Серия из" not in personal_failure_cards(s)[0]
    sim.run(sc)


def test_next_quest_comes_on_schedule_after_forgiven_failure_and_completion_unfreezes():
    async def sc(s):
        ch = await setup_quest(s)
        await set_user(s, streak=4)
        await give_up_flow(s, ch)
        await s.until(t(8, 5, day=23))                               # штрафа нет - квест в обычное время
        ch2 = await s.db.get_active_challenge(1)
        assert ch2 and ch2["id"] != ch["id"] and ch2["message_id"]
        assert "🧊" in await profile_text(s, 1)                       # лёд держится, пока не выполнен квест
        from bot.handlers import challenge as h
        from tests.test_completion_guide_sim import _call
        await log(s, 1, ch2["id"], "pushups", 10)
        await h.on_finish_challenge(_call(1, s.bot, f"finish:{ch2['id']}"))
        u = await user(s)
        assert (u["streak"], u["streak_frozen"], u["consecutive_failures"]) == (5, 0, 0)
        text = await profile_text(s, 1)
        assert "🔥 Серия: 5 дней" in text and "🧊" not in text
    sim.run(sc)


def test_second_failure_in_a_row_gets_the_full_penalty_and_clears_freeze():
    async def sc(s):
        ch = await setup_quest(s)
        await set_user(s, xp=800, streak=4)
        await give_up_flow(s, ch)                                     # день 1: прощён
        await s.until(t(8, 5, day=23))
        ch2 = await s.db.get_active_challenge(1)
        await log(s, 1, ch2["id"], "pushups", 3)
        await s.until(t(2, 5, day=24))                                # день 2: просрочен
        assert await status_of(s, ch2["id"]) == "expired"
        u = await user(s)
        assert (u["xp"], u["streak"], u["streak_frozen"], u["consecutive_failures"]) == (500, 0, 0, 2)
        assert u["penalty_until"] and u["is_stopped"]                 # штраф + автостоп (порог 2)
        reports = failure_reports(s)
        assert len(reports) == 2
        assert FREEZE_MARK in reports[0]
        assert "Серия из 4 дней: сброшена" in reports[1] and "Опыт: −300 XP" in reports[1] and FREEZE_MARK not in reports[1]
        cards = personal_failure_cards(s)
        assert FREEZE_MARK in cards[0] and "Наложена кара" in cards[1]
        text = await profile_text(s, 1)
        assert "🔥 Серия: 0 дней" in text and "🧊" not in text and "Вы не выполнили требование" in text
    sim.run(sc)


def test_completion_between_failures_restores_the_grace():
    async def sc(s):
        ch = await setup_quest(s)
        await set_user(s, xp=800, streak=3)
        await give_up_flow(s, ch)                                     # прощён
        await s.until(t(8, 5, day=23))
        ch2 = await s.db.get_active_challenge(1)
        from bot.handlers import challenge as h
        from tests.test_completion_guide_sim import _call
        await log(s, 1, ch2["id"], "pushups", 10)
        await h.on_finish_challenge(_call(1, s.bot, f"finish:{ch2['id']}"))   # выполнен: серия 4
        await s.until(t(8, 5, day=24))
        ch3 = await s.db.get_active_challenge(1)
        xp_before = (await user(s))["xp"]
        await give_up_flow(s, ch3)                                    # снова ПЕРВЫЙ подряд -> снова прощён
        u = await user(s)
        assert (u["streak"], u["streak_frozen"], u["consecutive_failures"], u["xp"]) == (4, 1, 1, xp_before)
        assert u["penalty_until"] is None and not u["is_stopped"]
    sim.run(sc)


def test_legacy_counter_without_freeze_flag_gets_full_penalty_not_ice():
    """БД до обновления: consecutive_failures = 1 (тогда штраф уже был), флага заморозки нет."""
    async def sc(s):
        ch = await setup_quest(s)
        await set_user(s, xp=500, streak=0, consecutive_failures=1, streak_frozen=0)
        await give_up_flow(s, ch)
        u = await user(s)
        assert (u["xp"], u["consecutive_failures"], u["streak_frozen"]) == (200, 2, 0) and u["penalty_until"]
    sim.run(sc)


def test_no_grace_when_auto_stop_threshold_is_one():
    async def sc(s):
        ch = await setup_quest(s)
        await set_user(s, xp=500, streak=3)
        await give_up_flow(s, ch)
        u = await user(s)
        assert (u["xp"], u["streak"], u["streak_frozen"], u["consecutive_failures"]) == (200, 0, 0, 1)
        assert u["penalty_until"] and u["is_stopped"]
        assert FREEZE_MARK not in failure_reports(s)[0]
    sim.run(sc, auto_stop_after_failures=1)


def test_group_down_does_not_block_the_freeze():
    async def sc(s):
        ch = await setup_quest(s)
        await set_user(s, xp=500, streak=3)
        s.bot.hooks.append(lambda m, cid, kw: (_ for _ in ()).throw(net_error()) if m == "send_message" and cid == GROUP else None)
        await log(s, 1, ch["id"], "pushups", 4)
        await s.until(DEADLINE)
        u = await user(s)
        assert (u["xp"], u["streak"], u["streak_frozen"], u["penalty_until"]) == (500, 3, 1, None)
        assert len(personal_failure_cards(s)) == 1 and group_texts(s) == []
        assert "🧊 Серия: 3 дня" in await profile_text(s, 1)
    sim.run(sc)


def test_no_double_freeze_on_double_click_or_timeout_race():
    async def sc(s):
        ch = await setup_quest(s)
        await set_user(s, xp=500, streak=3)
        from bot.handlers import challenge as h
        await give_up_flow(s, ch)
        await h._execute_give_up(s.bot, ch)                           # устаревшее повторное «Да»
        await s.until(DEADLINE)                                       # и таймаут уже закрытого квеста
        u = await user(s)
        assert (u["consecutive_failures"], u["streak"], u["streak_frozen"], u["xp"]) == (1, 3, 1, 500)
        assert len(failure_reports(s)) == 1 and len(personal_failure_cards(s)) == 1
    sim.run(sc)


def test_first_failure_warning_mentions_freeze_second_mentions_full_penalty():
    async def sc(s):
        await setup_quest(s)
        await s.until(t(1, 40, day=23))                               # до 02:00 меньше часа и меньше 30 минут
        w = warnings_for(s)
        assert w and all("заморожена" in x and "Серия дней: обнулена" not in x for x in w)
    sim.run(sc)

    async def sc2(s):
        await setup_quest(s)
        await set_user(s, consecutive_failures=1)
        await s.until(t(1, 40, day=23))
        w = warnings_for(s)
        assert w and all("Серия дней: обнулена" in x and "Опыт: −300 XP" in x and "заморожена" not in x for x in w)
    sim.run(sc2)


def test_manual_stop_and_start_keep_the_freeze_but_auto_stop_resume_clears_counter():
    async def sc(s):
        from bot.handlers import start as st
        from tests.test_completion_guide_sim import _call, _message
        ch = await setup_quest(s)
        await set_user(s, streak=5)
        await give_up_flow(s, ch)                                     # прощён, счётчик 1
        await st.on_stop_system_confirmed(_call(1, s.bot, "stopsys_yes:1"))
        assert (await user(s))["is_stopped"] == 1
        await st.cmd_start(_message(1, s.bot, text="/start"), s.bot)
        u = await user(s)
        assert (u["is_stopped"], u["streak_frozen"], u["consecutive_failures"], u["streak"]) == (0, 1, 1, 5)
        assert "🧊 Серия: 5" in await profile_text(s, 1)
    sim.run(sc)


def test_friend_brief_shows_ice_too():
    async def sc(s):
        from bot import friends
        ch = await setup_quest(s)
        await set_user(s, streak=3)
        await give_up_flow(s, ch)
        text, _ = await friends.render_brief(await user(s), 2, 0)
        assert "🧊 Серия: 3 дня" in text
        await set_user(s, streak_frozen=0)
        text, _ = await friends.render_brief(await user(s), 2, 0)
        assert "🔥 Серия: 3 дня" in text
    sim.run(sc)


def test_message_texts_fit_narrow_bubble_and_declension():
    from bot import messages as tx
    for text in (
        tx.challenge_failed_frozen("timeout", 7, 300, 48),
        tx.challenge_failed_frozen("gave_up", 0, 300, 48),
        tx.group_failure_frozen_report(42, 21),
        tx.group_failure_frozen_report(42, 0),
        tx.challenge_warning(15, 300, 48, frozen=True),
    ):
        assert FREEZE_MARK in text or "заморожена 🧊" in text
    assert "Серия из 21 дня: заморожена" in tx.group_failure_frozen_report(42, 21)
    assert "Серия из 11 дней: заморожена" in tx.group_failure_frozen_report(42, 11)
    assert "Серия из" not in tx.group_failure_frozen_report(42, 0)
    divider_lines = [l for l in tx.group_failure_frozen_report(42, 5).splitlines() if set(l) == {"─"}]
    assert len(divider_lines) == 3
    card = tx.challenge_failed_frozen("timeout", 1, 300, 48)
    assert "снимет 300 XP" in card and "48 ч" in card
    assert tx.challenge_warning(30, 300, 48) != tx.challenge_warning(30, 300, 48, frozen=True)


def test_upgrade_adds_streak_frozen_column_to_old_database(tmp_path):
    path = str(tmp_path / "old.sql")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT, registered_at TEXT, "
                "time_of_day TEXT, focuses TEXT DEFAULT '[]', group_id INTEGER, xp INTEGER DEFAULT 0, "
                "level INTEGER DEFAULT 1, streak INTEGER DEFAULT 0, penalty_until TEXT, reg_state TEXT DEFAULT 'done')")
    con.execute("INSERT INTO users (user_id, username, streak) VALUES (7, 'old', 9)")
    con.commit()
    con.close()

    async def sc(s):
        from bot.database import Database
        d = Database(path)
        await d.connect()
        u = await d.get_user(7)
        assert u["streak"] == 9 and (u["streak_frozen"] or 0) == 0
        await d.set_streak_frozen(7, True)
        assert (await d.get_user(7))["streak_frozen"] == 1
        await d.reset_streak(7)
        assert (await d.get_user(7))["streak_frozen"] == 0
        await d.close()
    sim.run(sc)


# ================================================================ 2. хранилище состояния: ключи сообщений


async def restart(s, restore_main_from=None):
    """Имитация перезапуска контейнера: обе БД закрываются и открываются заново; при необходимости
    db.sql перед этим подменяется копией из 'бэкапа'."""
    state_path = s.state.path
    await s.db.close()
    await s.state.close()
    if restore_main_from:
        shutil.copy(restore_main_from, s.db.path)
    await s.state.connect(state_path)
    await s.db.connect()


def snapshot_main(s):
    dst = os.path.join(tempfile.mkdtemp(), "backup.sql")
    shutil.copy(s.db.path, dst)
    return dst


def test_profile_and_failure_keys_are_mirrored_write_through():
    async def sc(s):
        from bot import profile
        ch = await setup_quest(s)
        u = await user(s)
        rec = await s.state.get_message_key("profile", 1)
        assert (rec["chat_id"], rec["message_id"], rec["created_at"]) == (1, u["profile_message_id"], u["profile_created_at"])
        await give_up_flow(s, ch)
        u = await user(s)
        frec = await s.state.get_message_key("failure", 1)
        assert frec["message_id"] == u["failure_message_id"] and frec["created_at"] == u["failure_sent_at"]
        await profile.clear_failure_message(s.bot, u)
        frec = await s.state.get_message_key("failure", 1)
        assert frec["message_id"] is None and (await user(s))["failure_message_id"] is None
    sim.run(sc)


def test_database_rollback_does_not_break_profile_editing():
    """Главный сценарий: db.sql откатили из бэкапа - профиль всё равно редактируется на месте."""
    async def sc(s):
        from bot import profile
        await s.make_user(1)
        await profile.sync_profile_message(s.bot, 1)                  # профиль A
        a = (await user(s))["profile_message_id"]
        backup = snapshot_main(s)                                     # бэкап: в нём ключ A
        Clock.now += timedelta(hours=5)
        await profile.resend_profile_message(s.bot, 1)                # профиль B (A удалён)
        b = (await user(s))["profile_message_id"]
        assert b != a and s.bot.profiles_alive(1) == [(1, b)]

        await restart(s, restore_main_from=backup)                    # контейнер пересоздан, БД из бэкапа

        u = await user(s)
        assert u["profile_message_id"] == b and u["profile_chat_id"] == 1     # ключ восстановлен из хранилища
        assert u["profile_created_at"] is not None
        await s.db.add_xp(1, 250)
        await profile.sync_profile_message(s.bot, 1)
        assert s.bot.profiles_alive(1) == [(1, b)]                    # дубля нет
        assert "250/" in s.bot.alive[(1, b)] or "Уровень" in s.bot.alive[(1, b)]
        edits = [kw for kw in s.bot.sent("edit_message_text", 1) if kw["message_id"] == b]
        assert edits and not [kw for kw in s.bot.sent("edit_message_text", 1) if kw["message_id"] == a]
    sim.run(sc)


def test_without_state_store_rollback_would_lose_the_profile_key_control():
    """Контроль для теста выше: без сверки после отката в основной БД остаётся старый ключ."""
    async def sc(s):
        from bot import profile
        await s.make_user(1)
        await profile.sync_profile_message(s.bot, 1)
        a = (await user(s))["profile_message_id"]
        backup = snapshot_main(s)
        Clock.now += timedelta(hours=5)
        await profile.resend_profile_message(s.bot, 1)
        await s.db.close()
        await s.state.close()
        shutil.copy(backup, s.db.path)
        await s.db.connect()                                           # хранилище НЕ подключено
        assert (await user(s))["profile_message_id"] == a             # откатился к старому id
        await s.state.connect(s.state.path)
    sim.run(sc)


def test_tombstone_beats_stale_key_after_rollback():
    async def sc(s):
        from bot import profile
        await s.make_user(1)
        await profile.sync_profile_message(s.bot, 1)
        backup = snapshot_main(s)                                     # в бэкапе профиль есть
        Clock.now += timedelta(hours=2)
        await s.db.set_stopped(1, True)
        assert await profile.retire_profile_message(s.bot, 1)         # профиль удалён, ключ очищен
        assert (await user(s))["profile_message_id"] is None
        await restart(s, restore_main_from=backup)
        u = await user(s)
        assert u["profile_message_id"] is None and u["profile_created_at"] is None   # мёртвый ключ не воскрес
    sim.run(sc)


def test_empty_state_store_is_seeded_from_main_db_and_main_wins_when_fresher():
    async def sc(s):
        from bot import profile
        await s.make_user(1)
        await profile.sync_profile_message(s.bot, 1)
        a = (await user(s))["profile_message_id"]
        # хранилище потеряно целиком
        state_path = s.state.path
        await s.db.close()
        await s.state.close()
        os.remove(state_path)
        await s.state.connect(state_path)
        assert await s.state.list_message_keys() == []
        await s.db.connect()
        rec = await s.state.get_message_key("profile", 1)
        assert rec["message_id"] == a
        # хранилище отстало (старая запись), основная БД свежее -> побеждает основная
        old = (Clock.now - timedelta(days=3)).isoformat()
        await s.state._conn.execute(
            "UPDATE message_keys SET message_id = 9, created_at = ?, updated_at = ? WHERE kind='profile'", (old, old))
        await s.state._conn.commit()
        res = await s.db.reconcile_message_keys()
        assert (await user(s))["profile_message_id"] == a
        assert (await s.state.get_message_key("profile", 1))["message_id"] == a and res["seeded"] >= 1
        # второй проход ничего не меняет
        assert await s.db.reconcile_message_keys() == {"restored": 0, "seeded": 0}
    sim.run(sc)


def test_missing_creation_time_is_filled_from_state_and_unknown_age_stays_unknown():
    async def sc(s):
        from bot import profile
        await s.make_user(1)
        await profile.sync_profile_message(s.bot, 1)
        created = (await user(s))["profile_created_at"]
        await s.db._conn.execute("UPDATE users SET profile_created_at = NULL WHERE user_id = 1")
        await s.db._conn.commit()
        await s.db.reconcile_message_keys()
        assert (await user(s))["profile_created_at"] == created
        # legacy-профиль без возраста: в хранилище возраст не выдумывается
        await s.db._conn.execute("DELETE FROM users WHERE user_id = 1")
        await s.state._conn.execute("DELETE FROM message_keys")
        await s.state._conn.commit()
        await s.make_user(2)
        await s.db._conn.execute("UPDATE users SET profile_chat_id = 2, profile_message_id = 77 WHERE user_id = 2")
        await s.db._conn.commit()
        await s.db.reconcile_message_keys()
        rec = await s.state.get_message_key("profile", 2)
        assert rec["message_id"] == 77 and rec["created_at"] is None
    sim.run(sc)


def test_state_only_user_without_main_row_is_ignored():
    async def sc(s):
        await s.state.put_message_key("profile", 999, 999, 5, created_at=Clock.now.isoformat())
        assert await s.db.reconcile_message_keys() == {"restored": 0, "seeded": 0}
        assert await s.db.get_user(999) is None
    sim.run(sc)


def test_state_store_failures_never_break_the_bot():
    async def sc(s):
        ch = await setup_quest(s)
        await set_user(s, xp=500, streak=3)
        await s.state.close()                                          # хранилище недоступно
        await give_up_flow(s, ch)
        u = await user(s)
        assert (u["streak"], u["streak_frozen"], u["xp"]) == (3, 1, 500)
        assert await s.state.get_message_key("profile", 1) is None
        # а при поломанном соединении (операция падает) - тоже только лог
        await s.state.connect(s.state.path)
        await s.state._conn.close()
        assert await s.state.put_message_key("profile", 1, 1, 5) is False
        assert await s.state.list_message_keys() == []
        s.state._conn = None
    sim.run(sc)


def test_unwritable_state_path_connect_returns_false():
    async def sc(s):
        from bot.state_store import StateStore
        bad = StateStore()
        blocker = os.path.join(tempfile.mkdtemp(), "file")
        open(blocker, "w").close()
        assert await bad.connect(os.path.join(blocker, "sub", "x.db")) is False     # родитель - файл
        assert bad.connected is False
        assert await bad.put_message_key("profile", 1, 1, 1) is False
        assert await bad.kv_get("x") is None and await bad.list_pending_deletes() == []
    sim.run(sc)


# ================================================================ 3. автоудаление переживает перезапуск


class _Hold:
    """Подменяет asyncio.sleep: таймеры автоудаления зависают, пока не вызван release()."""

    def __init__(self):
        self.saved = asyncio.sleep
        self.ev = asyncio.Event()

    async def _hold(self, delay, *a, **k):
        if delay and delay > 1:
            await self.ev.wait()
        else:
            await self.saved(0)

    def __enter__(self):
        asyncio.sleep = self._hold
        return self

    def release(self):
        self.ev.set()

    def __exit__(self, *exc):
        asyncio.sleep = self.saved


async def settle():
    """Даёт фоновым задачам (запись в SQLite идёт в потоке) отработать: реальная пауза, а не sleep(0)."""
    for _ in range(3):
        await asyncio.tasks.sleep(0.03)   # настоящий sleep: sim подменяет только asyncio.sleep


def test_pending_delete_is_persisted_then_dropped_after_deletion():
    async def sc(s):
        from bot import utils
        with _Hold() as hold:
            msg = await utils.send_command_message(s.bot, 1, "help", "hello")
            await settle()
            rows = await s.state.list_pending_deletes()
            assert [(r["chat_id"], r["message_id"], r["command_key"]) for r in rows] == [(1, msg.message_id, "help")]
            assert rows[0]["delete_at"] > time.time() + 20
            hold.release()
            await settle()
        assert (1, msg.message_id) not in s.bot.alive
        assert await s.state.list_pending_deletes() == []
    sim.run(sc)


def test_cancel_and_reschedule_keep_exactly_one_correct_row():
    async def sc(s):
        from bot import utils
        with _Hold() as hold:
            m1 = await s.bot.send_message(1, "a")
            m2 = await s.bot.send_message(1, "b")
            utils.schedule_delete(s.bot, 1, m1.message_id, 100)
            await settle()
            first = (await s.state.list_pending_deletes())[0]["delete_at"]
            utils.schedule_delete(s.bot, 1, m1.message_id, 500)                # сброс таймера
            utils.schedule_delete(s.bot, 1, m2.message_id, 100)
            await settle()
            rows = {r["message_id"]: r for r in await s.state.list_pending_deletes()}
            assert set(rows) == {m1.message_id, m2.message_id} and rows[m1.message_id]["delete_at"] > first + 300
            utils.cancel_scheduled_delete(1, m2.message_id)                    # игрок сам удалил / нажал «Да»
            await settle()
            assert {r["message_id"] for r in await s.state.list_pending_deletes()} == {m1.message_id}
            hold.release()
            await settle()
        assert (1, m2.message_id) in s.bot.alive                                # отменённое не удалено
        assert await s.state.list_pending_deletes() == []
    sim.run(sc)


def test_process_shutdown_keeps_pending_rows_and_resume_deletes_them():
    async def sc(s):
        from bot import utils
        with _Hold():
            msg = await utils.send_command_message(s.bot, 1, "friendlist", "list")
            await settle()
            for task in list(utils._delete_tasks.values()):                    # остановка процесса: задачи отменяются
                task.cancel()
            await settle()
        assert len(await s.state.list_pending_deletes()) == 1                  # запись пережила отмену
        utils._delete_tasks.clear()
        utils._command_messages.clear()
        await restart(s)
        assert await utils.resume_pending_deletes(s.bot) == 1
        assert utils._command_messages[(1, "friendlist")] == msg.message_id    # правило «одно сообщение на команду» вернулось
        await settle()
        assert (1, msg.message_id) not in s.bot.alive and await s.state.list_pending_deletes() == []
    sim.run(sc)


def test_resume_handles_overdue_future_and_ancient_rows():
    async def sc(s):
        from bot import utils
        now = time.time()
        for mid, at in ((501, now - 100), (502, now + 600), (503, now - 49 * 3600)):
            s.bot.alive[(1, mid)] = "x"
            await s.state.put_pending_delete(1, mid, at)
        calls = []
        real = utils.schedule_delete
        utils.schedule_delete = lambda bot, chat, mid, delay=30: calls.append((mid, round(delay)))
        try:
            assert await utils.resume_pending_deletes(s.bot) == 2
        finally:
            utils.schedule_delete = real
        by_mid = dict(calls)
        assert by_mid[501] <= 1 and 595 <= by_mid[502] <= 601                  # просроченное - сразу, будущее - в срок
        assert 503 not in by_mid
        assert {r["message_id"] for r in await s.state.list_pending_deletes()} == {501, 502}   # древнее выброшено
    sim.run(sc)


def test_failed_delete_still_drops_row_and_never_loops():
    async def sc(s):
        from bot import utils
        m = await s.bot.send_message(1, "z")
        s.bot.hooks.append(lambda method, cid, kw: (_ for _ in ()).throw(net_error()) if method == "delete_message" else None)
        utils.schedule_delete(s.bot, 1, m.message_id, 40)
        await settle()
        assert await s.state.list_pending_deletes() == []
    sim.run(sc)


# ================================================================ 4. кэш file_id гайда


def _doc_args(s):
    return [kw["document"] for kw in s.bot.sent("send_document", 1)]


def test_guide_file_id_survives_restart_and_invalidates_on_change():
    async def sc(s):
        from bot import guide
        from aiogram.types import FSInputFile
        await guide.send_guide(s.bot, 1, "c")
        assert isinstance(_doc_args(s)[0], FSInputFile)                       # первая отправка - загрузка файла
        assert "FAKE_FILE_ID" in (await s.state.kv_get("guide_file_id"))
        guide._file_id = None                                                  # перезапуск процесса
        await guide.send_guide(s.bot, 1, "c")
        assert _doc_args(s)[1] == "FAKE_FILE_ID"                               # file_id из хранилища, без загрузки
        # заменили guide.pdf -> отпечаток другой -> загрузка заново
        tmp = os.path.join(tempfile.mkdtemp(), "guide.pdf")
        with open(tmp, "wb") as f:
            f.write(open(guide.GUIDE_PATH, "rb").read() + b"\n%new")
        from pathlib import Path
        real = guide.GUIDE_PATH
        guide.GUIDE_PATH = Path(tmp)
        try:
            guide._file_id = None
            await guide.send_guide(s.bot, 1, "c")
            assert isinstance(_doc_args(s)[2], FSInputFile)
        finally:
            guide.GUIDE_PATH = real
        # другой бот (токен) -> тоже загрузка заново
        from bot.config import settings
        guide._file_id = None
        old_token = settings.bot_token
        settings.bot_token = "999999:OTHER"
        try:
            await guide.send_guide(s.bot, 1, "c")
            assert isinstance(_doc_args(s)[3], FSInputFile)
        finally:
            settings.bot_token = old_token
    sim.run(sc)


def test_guide_rejected_cached_id_is_dropped_and_replaced():
    async def sc(s):
        from bot import guide
        from aiogram.types import FSInputFile
        await guide.send_guide(s.bot, 1, "c")
        guide._file_id = None

        def hook(method, cid, kw):
            if method == "send_document" and isinstance(kw.get("document"), str):
                raise TelegramBadRequest(method=MagicMock(), message="Bad Request: wrong file identifier")
        s.bot.hooks.append(hook)
        msg = await guide.send_guide(s.bot, 1, "c")
        assert msg is not None and isinstance(_doc_args(s)[-1], FSInputFile)   # один раз перезагрузили
        assert "FAKE_FILE_ID" in (await s.state.kv_get("guide_file_id"))       # и сохранили новый
    sim.run(sc)


def test_guide_corrupted_or_foreign_cache_value_is_ignored():
    async def sc(s):
        from bot import guide
        from aiogram.types import FSInputFile
        for bad in ("not json", "[1,2]", '{"fp": "x", "file_id": "OLD"}', '{"fp": null}'):
            await s.state.kv_set("guide_file_id", bad)
            guide._file_id = None
            await guide.send_guide(s.bot, 1, "c")
            assert isinstance(_doc_args(s)[-1], FSInputFile), bad
    sim.run(sc)


def test_guide_works_without_state_store():
    async def sc(s):
        from bot import guide
        await s.state.close()
        await guide.send_guide(s.bot, 1, "c")
        await guide.send_guide(s.bot, 1, "c")
        assert _doc_args(s)[1] == "FAKE_FILE_ID"                               # кэш в памяти работает по-прежнему
        await s.state.connect(s.state.path)
    sim.run(sc)
