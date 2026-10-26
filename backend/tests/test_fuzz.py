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

    if not (s.cron_down):
        stuck = (Clock.now - timedelta(minutes=3)).isoformat()
        cur = await q("SELECT * FROM dispatch_log WHERE status='pending' AND next_attempt_at < ?", (stuck,))
        assert not await cur.fetchall(), "I9: pending-запись просрочена и не обрабатывается"


async def _scenario(s, seed, days=3):
    _DIRECTLY_COMPLETED.clear()
    _FORGOT_TO_FINISH.clear()
    _PROFILE_UNREACHABLE.clear()
    rnd = random.Random(seed)
    Clock.now = datetime(2026, 10, 22, 0, 0)
    slots = ["morning", "day", "evening", "night"]
    uids = list(range(1, 11))
    uid_slots = {u: rnd.choice(slots) for u in uids}
    for u in uids:
        await s.make_user(u, slot=uid_slots[u], focuses=rnd.choice([("pushups",), ("pushups", "chess")]))

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
