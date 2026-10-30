"""
Фаззинг: случайные сбои сети, блокировки бота, /stop и /start, простои бота и
выполнение квестов в течение нескольких суток. Каждую минуту проверяются инварианты.
"""
import random
from datetime import datetime, timedelta

import pytest

from tests import sim
from tests.sim import Clock, net_error


# id испытаний, которые сам фаззер закрыл напрямую (минуя прогресс) - для инварианта I11.
_DIRECTLY_COMPLETED: set[int] = set()
# id испытаний, где игрок закрыл все цели, но "Завершить" не нажал: фаззер сам их не закрывает,
# их должно подобрать авто-завершение по таймауту (если раньше не сработал /stop).
_FORGOT_TO_FINISH: set[int] = set()
# группа каждого игрока (id чата) - для инвариантов про отчёты и сообщения о поражении.
_GROUPS: dict[int, int] = {}
# сообщения профиля, которые бот не мог обновить вовремя (игрок заблокирован/бот выключен в окне 42-48 ч).
_PROFILE_UNREACHABLE: set[tuple[int, int]] = set()


async def _check_invariants(s, uid_slots):
    db = s.db
    q = db._conn.execute
    cur = await q("SELECT user_id, COUNT(*) c FROM challenges WHERE status='awaiting_action' GROUP BY user_id HAVING c > 1")
    assert not await cur.fetchall(), "I1: больше одного активного испытания у игрока"

    cur = await q("SELECT id, user_id FROM challenges WHERE status='awaiting_action' AND message_id IS NULL")
    for row in await cur.fetchall():
        c2 = await q("SELECT 1 FROM dispatch_log WHERE user_id=? AND status='pending'", (row["user_id"],))
        assert await c2.fetchone(), f"I2: недоставленная карточка {row['id']} без pending-записи (зависла)"

    cur = await q("SELECT * FROM dispatch_log WHERE attempts > 4")
    assert not await cur.fetchall(), "I3: больше 4 попыток"

    cur = await q("SELECT id FROM challenges WHERE status IN ('expired','gave_up') AND message_id IS NULL")
    assert not await cur.fetchall(), "I4: оштрафовано испытание, которое игрок не получал"

    # I10: просроченным (с штрафом) не может оказаться испытание, у которого ВСЕ цели
    # закрыты - такое должно быть авто-завершено (см. bot/completion.py).
    cur = await q("SELECT id FROM challenges WHERE status='expired'")
    for row in await cur.fetchall():
        c2 = await q("SELECT COUNT(*) n, SUM(completed) done FROM challenge_progress WHERE challenge_id=?", (row["id"],))
        r = await c2.fetchone()
        assert not (r["n"] and r["n"] == r["done"]), f"I10: испытание {row['id']} просрочено при всех закрытых целях"

    # I11: у выполненных испытаний все цели закрыты (авто-завершение не засчитывает недоделанное).
    cur = await q("SELECT id FROM challenges WHERE status IN ('completed','completed_with_photo')")
    for row in await cur.fetchall():
        c2 = await q("SELECT COUNT(*) n, SUM(completed) done FROM challenge_progress WHERE challenge_id=?", (row["id"],))
        r = await c2.fetchone()
        if r["n"]:  # испытания, закрытые напрямую через db.complete_challenge в самом фаззере, строк прогресса не трогают
            assert r["n"] == (r["done"] or 0) or row["id"] in _DIRECTLY_COMPLETED, f"I11: {row['id']} выполнено без закрытых целей"

    # I13: при сниженной сложности секретного испытания нет - ни бонуса, ни огоньков; кнопка
    # снижения сложности есть только у неснижённого испытания, у которого не все цели закрыты.
    cur = await q("SELECT id, user_id, status, bonus_claimed, message_id FROM challenges WHERE eased = 1")
    for row in await cur.fetchall():
        assert not row["bonus_claimed"], f"I13: у сниженного испытания {row['id']} есть секретный бонус"
        c2 = await q("SELECT COUNT(*) n FROM challenge_progress WHERE challenge_id=? AND bonus_claimed=1", (row["id"],))
        assert (await c2.fetchone())["n"] == 0, f"I13: огоньки у сниженного испытания {row['id']}"
        # Заблокировавший бота игрок кнопок не жмёт, а правка его карточки невозможна (Forbidden):
        # разметка у него может быть устаревшей - фаззер же вызывает хендлеры независимо от блокировки.
        if row["status"] == "awaiting_action" and row["message_id"] and row["user_id"] not in s.bot.blocked:
            cbs = [b.callback_data for r_ in (s.bot.markups.get((row["user_id"], row["message_id"])).inline_keyboard
                   if s.bot.markups.get((row["user_id"], row["message_id"])) else []) for b in r_]
            assert not any(cb and cb.startswith("ease:") for cb in cbs), (
                f"I13: кнопка снижения при eased {row['id']}; cron_down={s.cron_down} blocked={row['user_id'] in s.bot.blocked} "
                f"buttons={cbs} now={Clock.now}"
            )

    # I12: в чате у каждого игрока не больше одного сообщения профиля. Единственное
    # допустимое исключение - ограничение Telegram: если пока профилю было 42+ часа (время
    # пересоздания), игрок был заблокирован или бот не работал, профиль мог состариться
    # за 48 ч, а такое сообщение удалить уже нельзя. Такой "неуязвимый" старый профиль
    # (его id попал в _PROFILE_UNREACHABLE) дублем не считается.
    for uid in uid_slots:
        alive = s.bot.profiles_alive(uid)
        for key in alive:
            if Clock.now - s.bot.born[key] >= timedelta(hours=42) and (uid in s.bot.blocked or s.cron_down):
                _PROFILE_UNREACHABLE.add(key)
        if len(alive) > 1:
            newest = max(alive, key=lambda k: s.bot.born[k])
            stuck = [k for k in alive if k != newest and k not in _PROFILE_UNREACHABLE]
            assert not stuck, f"I12: у игрока {uid} дубль профиля {stuck} в чате в {Clock.now}"


    # I14: у карточки с ЗАКРЫТЫМИ целями нет кнопки "Сдаться" (и есть "Завершить"), а у
    # карточки с открытой целью "Сдаться" есть. Проверяется только когда перерисовка возможна:
    # игрок не заблокировал бота и до провала больше 15 минут (там карточку обновляет отдельная
    # джоба, которой в минутном тикере нет).
    from bot import challenge_render as _cr
    cur = await q("SELECT * FROM challenges WHERE status='awaiting_action' AND message_id IS NOT NULL")
    for row in await cur.fetchall():
        row = dict(row)
        if s.cron_down or row["user_id"] in s.bot.blocked or _cr.in_final_countdown(row):
            continue   # бот "выключен" (карточки не перерисовываются) / заблокирован / финальный отсчёт
        left = _cr.seconds_left(row)
        if left is not None and left <= 0:
            continue
        markup = s.bot.markups.get((row["user_id"], row["message_id"]))
        if markup is None:
            continue
        cbs = [b.callback_data for r_ in markup.inline_keyboard for b in r_]
        rows_ = await db.get_progress_rows(row["id"])
        all_done = bool(rows_) and all(p["completed"] for p in rows_)
        has_giveup = any(cb and cb.startswith("giveup:") for cb in cbs)
        confirm = any(cb and cb.startswith("giveup_yes:") for cb in cbs)
        if not confirm:
            assert has_giveup != all_done, (
                f"I14: кнопка Сдаться/цели не согласованы у {row['id']} (все закрыты={all_done}); "
                f"cron_down={s.cron_down} only_down={s.cron_only_down} forgot={row['id'] in _FORGOT_TO_FINISH} "
                f"gu_conf={_cr._giveup_confirm_active(row['id'])} ease_conf={_cr._ease_confirm_active(row['id'])} "
                f"buttons={cbs} text={s.bot.alive.get((row['user_id'], row['message_id']))!r}"
            )

    # I15: проваленные/сданные испытания не отправляли фото в группу; фото ушло -> оно было и статус "с фото".
    cur = await q("SELECT id FROM challenges WHERE status IN ('expired','gave_up') AND COALESCE(physical_photo_posted,0)=1")
    assert not await cur.fetchall(), "I15: фото проваленного испытания ушло в группу"
    cur = await q("SELECT id FROM challenges WHERE COALESCE(physical_photo_posted,0)=1 "
                  "AND (physical_photo_file_id IS NULL OR status <> 'completed_with_photo')")
    assert not await cur.fetchall(), "I15: posted без фото/статуса completed_with_photo"

    # I16: сдаться можно только при незакрытой цели (или без строк прогресса).
    cur = await q("SELECT id FROM challenges WHERE status='gave_up'")
    for row in await cur.fetchall():
        c2 = await q("SELECT COUNT(*) n, SUM(completed) done FROM challenge_progress WHERE challenge_id=?", (row["id"],))
        r = await c2.fetchone()
        assert not (r["n"] and r["n"] == (r["done"] or 0)), f"I16: сдано испытание {row['id']} с закрытыми целями"

    # I17: в группе игрока сообщений о поражении не больше, чем проваленных испытаний, фото-отчётов
    # не больше, чем выполненных, а число ушедших фото равно числу posted=1 (ничего не дублируется).
    for uid, gid in _GROUPS.items():
        cur = await q("SELECT COUNT(*) n FROM challenges WHERE user_id=? AND status IN ('expired','gave_up')", (uid,))
        failed = (await cur.fetchone())["n"]
        defeats = [kw for kw in s.bot.sent("send_message", gid) if "потерпел поражение" in kw["text"]]
        assert len(defeats) <= failed, f"I17: игроку {uid} {len(defeats)} сообщений о поражении при {failed} провалах"
        cur = await q("SELECT COUNT(*) n FROM challenges WHERE user_id=? AND status IN ('completed','completed_with_photo')", (uid,))
        done = (await cur.fetchone())["n"]
        reports = len(s.bot.sent("send_photo", gid)) + len(
            [kw for kw in s.bot.sent("send_message", gid) if "завершил испытание" in kw["text"]]
        )
        assert reports <= done, f"I17: игроку {uid} {reports} отчётов о выполнении при {done} выполненных"
        cur = await q("SELECT COUNT(*) n FROM challenges WHERE user_id=? AND COALESCE(physical_photo_posted,0)=1", (uid,))
        assert len(s.bot.sent("send_photo", gid)) == (await cur.fetchone())["n"], f"I17: фото игрока {uid} задублировано/потеряно"

    if not (s.cron_down):
        stuck = (Clock.now - timedelta(minutes=3)).isoformat()
        cur = await q("SELECT * FROM dispatch_log WHERE status='pending' AND next_attempt_at < ?", (stuck,))
        assert not await cur.fetchall(), "I9: pending-запись просрочена и не обрабатывается"


async def _scenario(s, seed, days=3):
    _DIRECTLY_COMPLETED.clear()
    _FORGOT_TO_FINISH.clear()
    _PROFILE_UNREACHABLE.clear()
    _GROUPS.clear()
    rnd = random.Random(seed)
    Clock.now = datetime(2026, 10, 22, 0, 0)
    slots = ["morning", "day", "evening", "night"]
    uids = list(range(1, 11))
    uid_slots = {u: rnd.choice(slots) for u in uids}
    for u in uids:
        await s.make_user(u, slot=uid_slots[u], focuses=rnd.choice([("pushups",), ("pushups", "chess")]))
        if u >= 6:   # половина игроков - 22+ уровня: им доступна кнопка "Снизить сложность"
            from bot.config import level_from_total_xp
            xp = next(x for x in range(0, 200_000, 100) if level_from_total_xp(x)[0] >= 22)
            await s.db._conn.execute("UPDATE users SET xp = ?, level = 22 WHERE user_id = ?", (xp, u))
            await s.db._conn.commit()

    for u in uids:
        if u % 3:                            # у двух третей игроков привязана группа
            _GROUPS[u] = -1000 - u
            await s.db.set_group(u, _GROUPS[u])

    net_bad = {u: None for u in uids}   # None - ок, число - сколько ещё карточек уронить, -1 - всегда

    def hook(method, cid, kw):
        if method == "send_message" and kw.get("reply_markup") is not None and net_bad.get(cid):
            if net_bad[cid] == -1:
                raise net_error()
            net_bad[cid] -= 1
            raise net_error()

    s.bot.hooks.append(hook)

    async def on_card(chat_id):
        u = await s.db.get_user(chat_id)
        assert not u["is_stopped"], f"I6: карточка отправлена остановленному игроку {chat_id} в {Clock.now}"
        assert not s.db.is_penalty_active(u), f"I7: карточка отправлена игроку {chat_id} под штрафом в {Clock.now}"
    s.bot.on_card_sent = on_card

    down_until = None
    end = Clock.now + timedelta(days=days)
    while Clock.now < end:
        # --- случайные события ---
        for u in uids:
            r = rnd.random()
            if r < 0.0015:
                net_bad[u] = rnd.choice([3, 3, 6, 9, 12, -1])
            elif r < 0.0030:
                net_bad[u] = None
            elif r < 0.0036:
                s.bot.blocked.symmetric_difference_update({u})
            elif r < 0.0042:                      # /stop как в handlers/start.py
                await s.db.set_stopped(u, True)
                a = await s.db.get_active_challenge(u)
                if a:
                    await s.db.stop_challenge(a["id"])
            elif r < 0.0050:                      # /start
                await s.db.set_stopped(u, False)
            elif r < 0.0070:                      # выполнил квест
                a = await s.db.get_active_challenge(u)
                if a and a.get("message_id") and a["id"] not in _FORGOT_TO_FINISH:
                    await s.db.complete_challenge(a["id"], False)
                    _DIRECTLY_COMPLETED.add(a["id"])
            elif 0.0104 <= r < 0.0150:            # игрок снижает сложность (запрос + "Да"), если кнопка доступна
                a = await s.db.get_active_challenge(u)
                if a and a.get("message_id"):
                    from bot.handlers import challenge as _ch
                    from tests.test_completion_guide_sim import _call as _mkcall
                    for name, data in (("on_ease_request", f"ease:{a['id']}"), ("on_ease_confirmed", f"ease_yes:{a['id']}")):
                        c = _mkcall(u, s.bot, data)
                        await getattr(_ch, name)(c)
            elif r < 0.0104 and r >= 0.0100:      # игрок набрал /profile: профиль пересоздаётся в случайное время суток
                from bot import profile as _profile
                await _profile.resend_profile_message(s.bot, u)
            elif r < 0.0100:                      # закрыл ВСЕ цели, но "Завершить" не нажал
                a = await s.db.get_active_challenge(u)
                if a and a.get("message_id") and a["id"] not in _FORGOT_TO_FINISH:
                    _FORGOT_TO_FINISH.add(a["id"])
                    for p in await s.db.get_progress_rows(a["id"]):
                        await s.db.add_progress_amount(a["id"], p["focus"], p["target"])
                        await s.db.sync_progress_completed(a["id"], p["focus"])
            elif 0.0150 <= r < 0.0185:            # "Сдаться" + "Да" реальными хендлерами
                a = await s.db.get_active_challenge(u)
                if a and a.get("message_id"):
                    from bot.handlers import challenge as _ch
                    from tests.test_completion_guide_sim import _call as _mkcall
                    for name, data in (("on_give_up_request", f"giveup:{a['id']}"), ("on_give_up_confirmed", f"giveup_yes:{a['id']}")):
                        await getattr(_ch, name)(_mkcall(u, s.bot, data))
            elif 0.0185 <= r < 0.0225:            # ввод подхода (иногда отрицательный - откат) реальными хендлерами
                a = await s.db.get_active_challenge(u)
                if a and a.get("message_id"):
                    from bot.handlers import challenge as _ch
                    from tests.test_completion_guide_sim import _call as _mkcall, _message as _mkmsg
                    p = rnd.choice(await s.db.get_progress_rows(a["id"]))
                    await _ch.on_focus_picked(_mkcall(u, s.bot, f"focus:{a['id']}:{p['focus']}"))
                    amount = rnd.choice([1, 3, 5, 10, 10, 20, -3, -10])
                    await _ch.on_amount_logged(_mkmsg(u, s.bot, text=str(amount)))
            elif 0.0225 <= r < 0.0255:            # присылает фото (сработает, только если физические цели закрыты)
                a = await s.db.get_active_challenge(u)
                if a and a.get("message_id"):
                    from unittest.mock import MagicMock as _MM
                    from bot.handlers import challenge as _ch
                    from tests.test_completion_guide_sim import _message as _mkmsg
                    m = _mkmsg(u, s.bot)
                    m.photo = [_MM(file_id=f"F{u}")]
                    await _ch.on_photo_received(m, s.bot)
            elif 0.0255 <= r < 0.0285:            # нажимает "Завершить испытание" (сработает, только если цели закрыты)
                a = await s.db.get_active_challenge(u)
                if a and a.get("message_id"):
                    from bot.handlers import challenge as _ch
                    from tests.test_completion_guide_sim import _call as _mkcall
                    await _ch.on_finish_challenge(_mkcall(u, s.bot, f"finish:{a['id']}"))
            elif 0.0285 <= r < 0.0290 and u in _GROUPS:   # группа "падает"/возвращается (бота выгнали/добавили)
                s.bot.blocked.symmetric_difference_update({_GROUPS[u]})
        if down_until is None and rnd.random() < 0.0012:
            down_until = Clock.now + timedelta(minutes=rnd.choice([5, 15, 90, 200, 400]))
            s.cron_down = rnd.random() < 0.5
            s.cron_only_down = not s.cron_down
        if down_until and Clock.now >= down_until:
            down_until = None
            s.cron_down = s.cron_only_down = False

        await s.step(1)
        await _check_invariants(s, uid_slots)

    # --- итог: интервал между доставленными карточками одного игрока ---
    by_user = {}
    for chat_id, _mid, ts in s.bot.card_log:
        by_user.setdefault(chat_id, []).append(ts)
    for u, times in by_user.items():
        for a, b in zip(times, times[1:]):
            assert b - a >= timedelta(hours=17), f"I5: две карточки игроку {u} подряд за {b - a} ({a} -> {b})"
    return len(s.bot.card_log)


@pytest.mark.parametrize("seed", list(range(1, 13)))
def test_fuzz(seed):
    delivered = sim.run(lambda s: _scenario(s, seed, days=4))
    assert delivered >= 3   # сценарий действительно что-то доставлял
