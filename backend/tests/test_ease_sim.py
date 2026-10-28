"""
Симуляции кнопки «📉 Снизить сложность» (config.EASE_*, Database.ease_challenge,
handlers/challenge.py::on_ease_*). Игрок 22+ уровня (отжимания = 50) может после
подтверждения «Да» вдвое уменьшить цели всех сегодняшних дисциплин: награда - половина XP,
секретное испытание недоступно, серия продолжается, в профиле и отчёте группы есть пометка.
"""
import asyncio
from unittest.mock import MagicMock

from bot import config
from bot.config import (
    EASE_UNLOCK_LEVEL,
    ease_equivalent_targets,
    eased_reward,
    eased_target,
    level_from_total_xp,
    target_for_level,
    xp_reward_for_challenge,
)
from tests import sim
from tests.test_completion_guide_sim import (
    DEADLINE,
    GROUP,
    _call,
    _message,
    group_texts,
    log,
    profile_text,
    status_of,
    t,
)

EASED_NOTE = "📉 Сложность в этот день была понижена."


# ---------------------------------------------------------------- помощники


def xp_for_level(level: int) -> int:
    """Минимальный суммарный XP, при котором игрок достигает этого уровня."""
    lo, hi = 0, 10_000_000
    while lo < hi:
        mid = (lo + hi) // 2
        if level_from_total_xp(mid)[0] >= level:
            hi = mid
        else:
            lo = mid + 1
    return lo


async def setup_ease_quest(s, level=EASE_UNLOCK_LEVEL, focuses=("pushups",), group=GROUP, uid=1):
    """Игрок нужного уровня с выданной карточкой (08:05 UTC)."""
    await s.make_user(uid, focuses=focuses)
    xp = xp_for_level(level)
    await s.db._conn.execute("UPDATE users SET xp = ?, level = ? WHERE user_id = ?", (xp, level, uid))
    await s.db._conn.commit()
    if group:
        await s.db.set_group(uid, group)
    await s.until(t(8, 5))
    ch = await s.db.get_active_challenge(uid)
    assert ch and ch["message_id"], "карточка не выдана"
    return ch


def card(s, ch, uid=1):
    """(текст, кнопки) карточки испытания в чате."""
    key = (uid, ch["message_id"])
    return s.bot.alive[key], s.bot.buttons(uid, ch["message_id"])


def callbacks(s, ch, uid=1):
    return [cb for row in s.bot.buttons(uid, ch["message_id"]) for (_, cb) in row]


def texts_of_buttons(s, ch, uid=1):
    return [txt for row in s.bot.buttons(uid, ch["message_id"]) for (txt, _) in row]


async def targets(s, ch):
    return {p["focus"]: p["target"] for p in await s.db.get_progress_rows(ch["id"])}


async def press(s, handler_name, ch, uid=1, prefix=None):
    from bot.handlers import challenge as h
    call = _call(uid, s.bot, f"{prefix}:{ch['id']}")
    call.message.message_id = ch["message_id"]
    await getattr(h, handler_name)(call)
    return call


async def ease_yes(s, ch, uid=1):
    await press(s, "on_ease_request", ch, uid, "ease")
    return await press(s, "on_ease_confirmed", ch, uid, "ease_yes")


def alert_text(call):
    return call.answer.await_args.args[0] if call.answer.await_args and call.answer.await_args.args else None


# ---------------------------------------------------------------- правила: уровень, цели, награда


def test_unlock_level_is_where_pushups_reach_fifty_and_equivalents_are_listed():
    assert target_for_level("pushups", EASE_UNLOCK_LEVEL) >= 50 > target_for_level("pushups", EASE_UNLOCK_LEVEL - 1)
    assert EASE_UNLOCK_LEVEL == 22
    assert ease_equivalent_targets() == {
        "pushups": 50, "squats": 50, "abs": 50, "pullups": 40, "running": 25, "chess": 3, "reading": 30,
    }


def test_eased_target_halves_rounding_up_and_never_below_one():
    assert [eased_target(x) for x in (50, 55, 40, 25, 30, 3, 2, 1, 100)] == [25, 28, 20, 13, 15, 2, 1, 1, 50]
    assert eased_reward(505) == 252 and eased_reward(1) == 1


def test_button_appears_only_from_unlock_level():
    async def sc(s):
        ch = await setup_ease_quest(s, level=EASE_UNLOCK_LEVEL - 1)
        assert f"ease:{ch['id']}" not in callbacks(s, ch)
        assert "📉 Снизить сложность" not in texts_of_buttons(s, ch)
    sim.run(sc)

    async def sc2(s):
        ch = await setup_ease_quest(s, level=EASE_UNLOCK_LEVEL)
        assert await targets(s, ch) == {"pushups": 50}
        rows = s.bot.buttons(1, ch["message_id"])
        assert rows[-2] == [("📉 Снизить сложность", f"ease:{ch['id']}")]   # прямо над "Сдаться"
        assert rows[-1][0][1] == f"giveup:{ch['id']}"
    sim.run(sc2)


# ---------------------------------------------------------------- подтверждение Да / Нет


def test_request_shows_confirmation_and_no_returns_the_normal_card_with_button():
    async def sc(s):
        ch = await setup_ease_quest(s, focuses=("pushups", "chess", "reading"))
        normal_text, _ = card(s, ch)
        await press(s, "on_ease_request", ch, prefix="ease")

        text, rows = card(s, ch)
        assert "Снизить сложность?" in text
        assert "Отжимания: 50 → 25" in text and "Шахматы: 3 → 2" in text and "Чтение" in text and "→ 15" in text
        assert "половина XP" in text and "Секретное испытание" in text and "Серия сохранится" in text
        assert rows == [[("Да", f"ease_yes:{ch['id']}"), ("Нет", f"ease_no:{ch['id']}")]]
        assert (await s.db.get_challenge(ch["id"]))["eased"] == 0                 # до "Да" ничего не меняется
        assert await targets(s, ch) == {"pushups": 50, "chess": 3, "reading": 30}

        await press(s, "on_ease_declined", ch, prefix="ease_no")
        text, _ = card(s, ch)
        assert "Снизить сложность?" not in text and text.startswith("[ Информация о квесте ]")
        assert f"ease:{ch['id']}" in callbacks(s, ch)                              # кнопка никуда не делась
        assert await targets(s, ch) == {"pushups": 50, "chess": 3, "reading": 30}
        assert (await s.db.get_challenge(ch["id"]))["eased"] == 0
    sim.run(sc)


def test_confirmation_screen_survives_timer_redraws():
    async def sc(s):
        ch = await setup_ease_quest(s)
        await press(s, "on_ease_request", ch, prefix="ease")
        await s.step(3)                                                            # тики таймера карточки
        assert "Снизить сложность?" in card(s, ch)[0]
    sim.run(sc)


# ---------------------------------------------------------------- "Да": цели, прогресс, карточка


def test_yes_halves_all_targets_keeps_progress_and_updates_the_card():
    async def sc(s):
        ch = await setup_ease_quest(s, focuses=("pushups", "pullups", "running", "chess", "reading"))
        assert await targets(s, ch) == {"pushups": 50, "pullups": 40, "running": 25, "chess": 3, "reading": 30}
        await log(s, 1, ch["id"], "pushups", 30)
        call = await ease_yes(s, ch)

        assert await targets(s, ch) == {"pushups": 25, "pullups": 20, "running": 13, "chess": 2, "reading": 15}
        rows = {p["focus"]: p for p in await s.db.get_progress_rows(ch["id"])}
        assert rows["pushups"]["amount"] == 30 and rows["pushups"]["completed"] == 1   # набрано больше новой цели
        assert rows["pullups"]["completed"] == 0
        assert (await s.db.get_challenge(ch["id"]))["eased"] == 1

        text, _ = card(s, ch)
        assert "📉 Сложность снижена" in text and "Снизить сложность?" not in text
        assert f"ease:{ch['id']}" not in callbacks(s, ch)                          # кнопка пропала до конца дня
        assert any("25" in b for b in texts_of_buttons(s, ch) if "Отжимания" in b)
        assert any("0/20" in b for b in texts_of_buttons(s, ch) if "Подтягивания" in b)
        assert (await s.db.get_active_challenge(1))["active_focus"] is None
    sim.run(sc)


def test_yes_when_halved_targets_are_already_met_closes_everything():
    async def sc(s):
        ch = await setup_ease_quest(s)
        await log(s, 1, ch["id"], "pushups", 30)
        assert f"ease:{ch['id']}" in callbacks(s, ch)
        await ease_yes(s, ch)
        assert await s.db.goals_ready(ch["id"])
        assert f"finish:{ch['id']}" in callbacks(s, ch)                            # появилась "Завершить испытание"
        assert f"ease:{ch['id']}" not in callbacks(s, ch)
    sim.run(sc)


def test_pressing_a_discipline_waiting_for_a_number_is_reset_by_ease():
    async def sc(s):
        from bot.handlers import challenge as h
        ch = await setup_ease_quest(s, focuses=("pushups", "reading"))
        await h.on_focus_picked(_call(1, s.bot, f"focus:{ch['id']}:reading"))
        assert (await s.db.get_active_challenge(1))["active_focus"] == "reading"
        await ease_yes(s, ch)
        assert (await s.db.get_active_challenge(1))["active_focus"] is None
    sim.run(sc)


def test_double_yes_halves_only_once():
    async def sc(s):
        ch = await setup_ease_quest(s)
        await press(s, "on_ease_request", ch, prefix="ease")
        c1 = _call(1, s.bot, f"ease_yes:{ch['id']}")
        c2 = _call(1, s.bot, f"ease_yes:{ch['id']}")
        from bot.handlers import challenge as h
        await asyncio.gather(h.on_ease_confirmed(c1), h.on_ease_confirmed(c2))
        assert await targets(s, ch) == {"pushups": 25}                             # а не 13
    sim.run(sc)


# ---------------------------------------------------------------- когда кнопки / понижения быть не должно


def test_button_disappears_when_all_goals_are_closed_and_stale_press_is_refused():
    async def sc(s):
        ch = await setup_ease_quest(s)
        await log(s, 1, ch["id"], "pushups", 50)
        assert f"ease:{ch['id']}" not in callbacks(s, ch) and f"finish:{ch['id']}" in callbacks(s, ch)
        call = await press(s, "on_ease_request", ch, prefix="ease")               # устаревшая кнопка
        assert call.answer.await_args.kwargs.get("show_alert") is True
        assert "все цели" in alert_text(call)
        assert await targets(s, ch) == {"pushups": 50}
        assert (await s.db.get_challenge(ch["id"]))["eased"] == 0
    sim.run(sc)


def test_button_reappears_if_progress_is_rolled_back_below_target():
    async def sc(s):
        ch = await setup_ease_quest(s)
        await log(s, 1, ch["id"], "pushups", 50)
        await log(s, 1, ch["id"], "pushups", -10)
        assert f"ease:{ch['id']}" in callbacks(s, ch)
    sim.run(sc)


def test_second_ease_foreign_user_and_closed_challenge_are_refused():
    async def sc(s):
        ch = await setup_ease_quest(s, focuses=("pushups", "reading"))
        await ease_yes(s, ch)
        again = await press(s, "on_ease_request", ch, prefix="ease")
        assert "уже снижена" in alert_text(again)
        assert await targets(s, ch) == {"pushups": 25, "reading": 15}

        foreign = await press(s, "on_ease_confirmed", ch, uid=777, prefix="ease_yes")
        assert foreign.answer.await_args.kwargs.get("show_alert") is True
        assert await targets(s, ch) == {"pushups": 25, "reading": 15}
    sim.run(sc)

    async def sc2(s):
        from bot.handlers import challenge as h
        ch = await setup_ease_quest(s)
        await h._execute_give_up(s.bot, await s.db.get_challenge(ch["id"]))
        call = await press(s, "on_ease_confirmed", ch, prefix="ease_yes")
        assert call.answer.await_args.kwargs.get("show_alert") is True
        assert await targets(s, ch) == {"pushups": 50}
    sim.run(sc2)


def test_db_ease_is_atomic_and_conditional():
    async def sc(s):
        ch = await setup_ease_quest(s, focuses=("pushups", "reading"))
        assert await s.db.ease_challenge(ch["id"]) is True
        assert await s.db.ease_challenge(ch["id"]) is False                        # второй раз - нет
        ch2 = await s.db.get_challenge(ch["id"])
        assert ch2["eased"] == 1 and await targets(s, ch) == {"pushups": 25, "reading": 15}
    sim.run(sc)

    async def sc2(s):
        ch = await setup_ease_quest(s)
        await log(s, 1, ch["id"], "pushups", 50)                                   # все цели закрыты
        assert await s.db.ease_challenge(ch["id"]) is False
        assert await targets(s, ch) == {"pushups": 50}
    sim.run(sc2)


def test_every_discipline_is_halved_rounding_up_at_higher_levels():
    async def sc(s):
        all_focuses = tuple(config.FOCUS_OPTIONS)
        ch = await setup_ease_quest(s, level=40, focuses=all_focuses)
        before = await targets(s, ch)
        await ease_yes(s, ch)
        after = await targets(s, ch)
        assert set(before) == set(all_focuses)
        for key in all_focuses:
            assert after[key] == max(1, -(-before[key] // 2)), (key, before[key], after[key])
    sim.run(sc)


# ---------------------------------------------------------------- награда, серия, секретное испытание


def test_reward_is_half_and_streak_continues():
    async def sc(s):
        from bot.handlers import challenge as h
        ch = await setup_ease_quest(s)
        await s.db._conn.execute("UPDATE users SET streak = 4 WHERE user_id = 1")
        await s.db._conn.commit()
        xp0 = (await s.db.get_user(1))["xp"]
        await ease_yes(s, ch)
        await log(s, 1, ch["id"], "pushups", 25)
        await h.on_finish_challenge(_call(1, s.bot, f"finish:{ch['id']}"))

        u = await s.db.get_user(1)
        full = xp_reward_for_challenge(EASE_UNLOCK_LEVEL)
        assert u["xp"] - xp0 == eased_reward(full) == full // 2
        assert u["streak"] == 5 and u["consecutive_failures"] == 0
        assert await status_of(s, ch["id"]) == "completed"
    sim.run(sc)


def test_control_without_ease_gets_full_reward():
    async def sc(s):
        from bot.handlers import challenge as h
        ch = await setup_ease_quest(s)
        xp0 = (await s.db.get_user(1))["xp"]
        await log(s, 1, ch["id"], "pushups", 50)
        await h.on_finish_challenge(_call(1, s.bot, f"finish:{ch['id']}"))
        assert (await s.db.get_user(1))["xp"] - xp0 == xp_reward_for_challenge(EASE_UNLOCK_LEVEL)
    sim.run(sc)


def test_secret_quest_is_unavailable_even_with_double_of_the_old_norm():
    async def sc(s):
        from bot.handlers import challenge as h
        ch = await setup_ease_quest(s, focuses=("pushups", "reading"))
        await ease_yes(s, ch)
        xp0 = (await s.db.get_user(1))["xp"]
        await log(s, 1, ch["id"], "pushups", 100)                                  # x2 прежней нормы, x4 новой
        await log(s, 1, ch["id"], "reading", 60)
        c = await s.db.get_challenge(ch["id"])
        assert c["bonus_claimed"] == 0
        assert all(p["bonus_claimed"] == 0 for p in await s.db.get_progress_rows(ch["id"]))
        assert (await s.db.get_user(1))["xp"] == xp0                               # бонусных XP нет
        assert not any("🔥" in b for b in texts_of_buttons(s, ch))

        await h.on_finish_challenge(_call(1, s.bot, f"finish:{ch['id']}"))
        assert "секретное" not in await profile_text(s, 1)
    sim.run(sc)


def test_progress_flames_earned_before_easing_are_removed():
    async def sc(s):
        ch = await setup_ease_quest(s, focuses=("pushups", "reading"))
        await log(s, 1, ch["id"], "pushups", 100)                                  # x2: личный огонёк по отжиманиям
        assert any("🔥" in b for b in texts_of_buttons(s, ch))
        assert await s.db.physical_bonus_achieved(ch["id"]) is True
        await ease_yes(s, ch)
        assert not any("🔥" in b for b in texts_of_buttons(s, ch))
        assert await s.db.physical_bonus_achieved(ch["id"]) is False
    sim.run(sc)


# ---------------------------------------------------------------- пометки: профиль и группа


def test_profile_and_group_report_carry_the_eased_note_after_auto_finish():
    async def sc(s):
        ch = await setup_ease_quest(s)
        await s.db._conn.execute("UPDATE users SET streak = 4 WHERE user_id = 1")
        await s.db._conn.commit()
        await ease_yes(s, ch)
        await log(s, 1, ch["id"], "pushups", 40)                                   # закрыто (>= 25), "Завершить" не нажали
        await s.until(DEADLINE)

        assert await status_of(s, ch["id"]) == "completed"
        assert group_texts(s) == [f"Отчёт дня 5. Игрок 1 завершил испытание.\n\n{EASED_NOTE}"]
        p = await profile_text(s, 1)
        assert "✅ Вы выполнили сегодняшний квест." in p and EASED_NOTE in p
        assert "секретное" not in p
        assert "🔥 Серия: 5 дней" in p
    sim.run(sc)


def test_normal_day_has_no_eased_note():
    async def sc(s):
        ch = await setup_ease_quest(s)
        await log(s, 1, ch["id"], "pushups", 50)
        await s.until(DEADLINE)
        assert EASED_NOTE not in await profile_text(s, 1)
        assert EASED_NOTE not in group_texts(s)[0]
    sim.run(sc)


def test_photo_report_after_easing_has_the_note_and_no_secret_line():
    async def sc(s):
        from bot.handlers import challenge as h
        ch = await setup_ease_quest(s, focuses=("pushups", "reading"))
        await ease_yes(s, ch)
        await log(s, 1, ch["id"], "pushups", 100)
        photo = _message(1, s.bot)
        photo.photo = [MagicMock(file_id="P")]
        await h.on_photo_received(photo, s.bot)
        caption = s.bot.sent("send_photo", GROUP)[0]["caption"]
        assert caption == f"Отчёт дня 1. Игрок 1 завершил испытание.\n\n{EASED_NOTE}"
        assert "🌟" not in caption
    sim.run(sc)


def test_photo_sent_before_easing_gets_a_separate_note_at_completion():
    async def sc(s):
        from bot.handlers import challenge as h
        ch = await setup_ease_quest(s, focuses=("pushups", "reading"))
        await log(s, 1, ch["id"], "pushups", 50)
        photo = _message(1, s.bot)
        photo.photo = [MagicMock(file_id="P")]
        await h.on_photo_received(photo, s.bot)                                    # фото ушло, пометки в нём нет
        assert EASED_NOTE not in s.bot.sent("send_photo", GROUP)[0]["caption"]
        await ease_yes(s, ch)
        await log(s, 1, ch["id"], "reading", 15)
        await h.on_finish_challenge(_call(1, s.bot, f"finish:{ch['id']}"))
        assert group_texts(s) == [f"Отчёт дня 1. Игрок 1.\n\n{EASED_NOTE}"]
        assert await status_of(s, ch["id"]) == "completed_with_photo"
    sim.run(sc)


def test_manual_finish_without_a_group_report_sends_nothing_extra():
    async def sc(s):
        from bot.handlers import challenge as h
        ch = await setup_ease_quest(s)
        await ease_yes(s, ch)
        await log(s, 1, ch["id"], "pushups", 25)
        await h.on_finish_challenge(_call(1, s.bot, f"finish:{ch['id']}"))
        assert group_texts(s) == []                                                # как и в обычный день
        assert EASED_NOTE in await profile_text(s, 1)
    sim.run(sc)


# ---------------------------------------------------------------- провал и следующий день


def test_failing_an_eased_quest_has_the_normal_penalty():
    async def sc(s):
        ch = await setup_ease_quest(s, focuses=("pushups", "reading"))
        await s.db._conn.execute("UPDATE users SET streak = 6 WHERE user_id = 1")
        await s.db._conn.commit()
        xp0 = (await s.db.get_user(1))["xp"]
        await ease_yes(s, ch)
        await log(s, 1, ch["id"], "pushups", 25)                                   # чтение не сделано
        await s.until(DEADLINE)
        assert await status_of(s, ch["id"]) == "expired"
        u = await s.db.get_user(1)
        assert u["streak"] == 0 and u["xp"] == max(0, xp0 - 300) and u["penalty_until"]
        assert EASED_NOTE not in await profile_text(s, 1)
    sim.run(sc)


def test_next_day_quest_is_full_difficulty_with_the_button_again():
    async def sc(s):
        ch = await setup_ease_quest(s)
        await ease_yes(s, ch)
        await log(s, 1, ch["id"], "pushups", 25)
        await s.until(DEADLINE)
        assert await status_of(s, ch["id"]) == "completed"
        await s.until(t(8, 5, day=23))
        nxt = await s.db.get_active_challenge(1)
        assert nxt["id"] != ch["id"] and nxt["eased"] == 0
        assert await targets(s, nxt) == {"pushups": 50}
        assert f"ease:{nxt['id']}" in callbacks(s, nxt)
        assert EASED_NOTE not in await profile_text(s, 1)
    sim.run(sc)


# ---------------------------------------------------------------- миграция


def test_old_database_gets_eased_column(tmp_path):
    import sqlite3
    old = str(tmp_path / "old.sql")
    con = sqlite3.connect(old)
    con.execute("CREATE TABLE challenges (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, date TEXT, "
                "status TEXT, message_id INTEGER, started_at TEXT)")
    con.execute("INSERT INTO challenges (user_id, date, status) VALUES (5, '2026-01-01', 'completed')")
    con.commit()
    con.close()

    async def sc(s):
        from bot.database import Database
        d = Database(old)
        await d.connect()
        cols = {r[1] for r in await d._conn.execute_fetchall("PRAGMA table_info(challenges)")}
        assert "eased" in cols
        row = (await d._conn.execute_fetchall("SELECT COALESCE(eased, 0) FROM challenges"))[0]
        assert row[0] == 0                                                         # старые испытания - не сниженные
        await d.close()
    sim.run(sc)


# ---------------------------------------------------------------- граничные случаи


def test_confirmation_open_when_time_runs_out_gives_failure_and_later_yes_is_refused():
    async def sc(s):
        ch = await setup_ease_quest(s, focuses=("pushups", "reading"))
        await press(s, "on_ease_request", ch, prefix="ease")                      # экран подтверждения открыт
        await s.until(DEADLINE)
        assert await status_of(s, ch["id"]) == "expired"
        assert "Снизить сложность?" not in s.bot.alive.get((1, ch["message_id"]), "")
        late = await press(s, "on_ease_confirmed", ch, prefix="ease_yes")          # "Да" на устаревшем экране
        assert late.answer.await_args.kwargs.get("show_alert") is True
        assert await targets(s, ch) == {"pushups": 50, "reading": 30}
        assert (await s.db.get_challenge(ch["id"]))["eased"] == 0
    sim.run(sc)


def test_ease_then_deadline_completes_with_half_reward_when_halved_goals_are_met():
    async def sc(s):
        ch = await setup_ease_quest(s, focuses=("pushups", "reading"))
        xp0 = (await s.db.get_user(1))["xp"]
        await log(s, 1, ch["id"], "pushups", 30)
        await log(s, 1, ch["id"], "reading", 20)                                   # обе цели набраны больше новых (25/15)
        await ease_yes(s, ch)
        assert await s.db.goals_ready(ch["id"])
        await s.until(DEADLINE)                                                    # "Завершить" не нажали
        assert await status_of(s, ch["id"]) == "completed"
        u = await s.db.get_user(1)
        assert u["xp"] - xp0 == eased_reward(xp_reward_for_challenge(EASE_UNLOCK_LEVEL))
        assert u["penalty_until"] is None and u["streak"] == 1
        assert group_texts(s) == [f"Отчёт дня 1. Игрок 1 завершил испытание.\n\n{EASED_NOTE}"]
    sim.run(sc)


def test_guide_pdf_mentions_the_feature_and_has_sequential_page_numbers():
    import re
    import pypdf
    from bot import guide
    reader = pypdf.PdfReader(guide.GUIDE_PATH)
    texts = [(pg.extract_text() or "") for pg in reader.pages]
    assert any("Снизь сложность" in t and "Снизить сложность" in t for t in texts)
    for idx in range(1, len(texts)):                                               # страница 1 - обложка, на остальных номер = позиция
        numbers = re.findall(r"(?m)^\s*(\d{1,2})\s*$", texts[idx])
        assert numbers and numbers[-1] == str(idx + 1), (idx + 1, numbers)
