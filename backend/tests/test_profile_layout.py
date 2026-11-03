"""
Проверки доработок: новый профиль (блок «время + группа» внизу), короткое предупреждение,
ссылка на группу и бэкап обеих БД. Тесты не требуют сети и Telegram.
Запуск:  cd backend && python -m pytest tests/test_profile_layout.py -q
"""
import asyncio
import sqlite3
from pathlib import Path

from bot import messages as tx
from bot import profile
from bot.config import time_of_day_short


def _user(**kw):
    base = dict(
        user_id=1762488695, username="turokamo", first_name="turokamo",
        registered_at="2026-07-26T10:00:00", time_of_day="evening",
        focuses='["pushups","squats","abs","chess"]', group_id=-1001234567890,
        group_link="https://t.me/c/1234567890/1", xp=690, streak=1, streak_frozen=0,
        penalty_until=None, is_stopped=0, daily_pushups=405, daily_squats=505,
        daily_abs=510, daily_chess=25, daily_pullups=0, daily_running=0, daily_reading=0,
    )
    base.update(kw)
    return base


def test_time_short():
    assert time_of_day_short("evening") == "🌇 19:00"
    assert time_of_day_short("morning") == "🌅 05:00"
    assert time_of_day_short("bogus") == "—"


def test_profile_footer_and_done_marks_order():
    text = profile.render_profile_text(_user(), quest_done_today=True, bonus_claimed_today=True)
    lines = text.split("\n")
    assert lines[-1] == "✅ Вы завершили секретное испытание."
    assert lines[-2] == "✅ Вы выполнили сегодняшний квест."
    assert "Группа привязана" not in text and "Вечером" not in text
    assert text.index("Серия:") < text.index("Отжимания") < text.index("Время испытаний")
    assert text.index("Время испытаний") < text.index("Вы выполнили сегодняшний квест")
    plain = profile.render_profile_text(_user()).split("\n")
    assert plain[-2] == "⏰ Время испытаний: 🌇 19:00" and plain[-1].startswith('👥 Группа: <a href="https://t.me/c/1234567890/1">')


def test_profile_stop_and_penalty_between_footer_and_done():
    from datetime import datetime, timedelta

    until = (datetime.utcnow() + timedelta(hours=47, minutes=30)).isoformat()
    text = profile.render_profile_text(
        _user(is_stopped=1, penalty_until=until), quest_done_today=True, eased_today=True
    )
    i = [text.index(x) for x in ("Группа:", "Система остановлена", "Вы не выполнили требование", "Вы выполнили сегодняшний")]
    assert i == sorted(i)
    assert "До снятия: 47 ч 2" in text or "До снятия: 47 ч 3" in text
    assert "Смотреть контент" not in text and text.count("🚫") == 1
    no_group = profile.render_profile_text(_user(group_id=None, group_link=None))
    assert "👥 Группа: нет (напиши /bind_group в группе)" in no_group
    assert profile.render_profile_text(_user(group_link=None)).endswith("👥 Группа: привязана")
    assert profile.render_profile_text(_user(time_of_day=None)).count("Время испытаний: —") == 1


def test_group_link_is_escaped_and_validated():
    bad = profile.render_profile_text(_user(group_link='javascript:alert("x")'))
    assert "<a " not in bad and bad.endswith("привязана")
    esc = profile.render_profile_text(_user(group_link='https://t.me/a"b&c'))
    assert 'href="https://t.me/a&quot;b&amp;c"' in esc


def test_warning_is_short():
    for minutes in (60, 30, 15):
        for frozen in (True, False):
            text = tx.challenge_warning(minutes, 300, 48, frozen=frozen)
            assert text.startswith("⚠ 『 Предупреждение Системы 』 ⚠\n") and text.count("\n") == 1
            assert ("менее часа" if minutes == 60 else f"менее {minutes} минут") in text
            assert ("заморожена" in text) == frozen and ("штраф" in text) == (not frozen)
            assert "Время идёт" not in text and "────" not in text


class _Chat:
    def __init__(self, username=None, invite=None):
        self.username, self.invite_link = username, invite


class _Link:
    invite_link = "https://t.me/+NEW"


class _Bot:
    def __init__(self, chat=None, get_err=False, create_err=False):
        self.chat, self.get_err, self.create_err = chat, get_err, create_err

    async def get_chat(self, chat_id):
        if self.get_err:
            raise RuntimeError("boom")
        return self.chat

    async def create_chat_invite_link(self, chat_id, name=None):
        if self.create_err:
            raise RuntimeError("no rights")
        return _Link()


def _resolve(bot, chat_id=-1001234567890):
    return asyncio.run(profile.resolve_group_link(bot, chat_id))


def test_resolve_group_link_chain():
    assert _resolve(_Bot(_Chat("mygroup"))) == "https://t.me/mygroup"
    assert _resolve(_Bot(_Chat(None, "https://t.me/+OLD"))) == "https://t.me/+OLD"
    assert _resolve(_Bot(_Chat())) == "https://t.me/+NEW"
    assert _resolve(_Bot(_Chat(), create_err=True)) == "https://t.me/c/1234567890/1"
    assert _resolve(_Bot(get_err=True, create_err=True)) == "https://t.me/c/1234567890/1"
    assert _resolve(_Bot(_Chat(), create_err=True), -12345) is None


def test_backup_snapshot_is_consistent_and_deterministic(tmp_path: Path):
    from bot import backup

    src = tmp_path / "src.db"
    conn = sqlite3.connect(src)
    conn.execute("create table t(x)")
    conn.execute("insert into t values (1)")
    conn.commit()
    conn.close()
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    assert asyncio.run(backup._stage_db_file(src, a)) and asyncio.run(backup._stage_db_file(src, b))
    assert a.read_bytes() == b.read_bytes()
    assert sqlite3.connect(a).execute("select x from t").fetchall() == [(1,)]
