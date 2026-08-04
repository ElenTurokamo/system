"""
Бизнес-логика "Отряда" (список друзей): статус-иконки, рендер списка и
брифа. Хендлеры команд/колбэков - в bot/handlers/friends.py; этот модуль -
единая точка правды за то, как это выглядит (по аналогии с profile.py и
challenge_render.py).
"""
from aiogram.types import InlineKeyboardMarkup

from bot import keyboards as kb
from bot.config import FOCUS_OPTIONS, level_from_total_xp
from bot.database import db
from bot.profile import display_name, ru_days

# Друзей на одну страницу /friendlist. Стрелочки листания появляются, только
# когда друзей больше, чем помещается на одну страницу - то есть больше 3.
PAGE_SIZE = 3

# Статус сегодняшнего испытания друга в списке - максимально компактно, одним
# смайликом. "Провал" фиксируется наравне с успехом (не прячется), а не только
# показывает "в процессе"/"выполнено" - соцдавление работает в обе стороны.
_STATUS_ICONS = {
    "awaiting_action": "⏳",
    "completed": "✅",
    "completed_with_photo": "✅",
    "gave_up": "❌",
    "expired": "❌",
}

# Та же карта, но текстом для брифа - без деталей штрафа/истории провалов
# (сколько раз всего проваливал и т.п.), только факт по сегодняшнему дню.
_STATUS_LABELS = {
    "awaiting_action": "⏳ в процессе",
    "completed": "✅ выполнено",
    "completed_with_photo": "✅ выполнено",
    "gave_up": "❌ не выполнено",
    "expired": "❌ не выполнено",
}


async def _latest_status(user_id: int) -> str | None:
    latest = await db.get_latest_challenge(user_id)
    return latest["status"] if latest else None


async def status_icon(user_id: int) -> str:
    status = await _latest_status(user_id)
    return _STATUS_ICONS.get(status, "▫️")


async def status_label(user_id: int) -> str:
    status = await _latest_status(user_id)
    return _STATUS_LABELS.get(status, "▫️ ещё не начато")


async def build_friend_rows(user_id: int) -> list[dict]:
    """
    Принятые друзья пользователя с ником и текущей иконкой статуса.
    Сортировка - как в мини-лидерборде: сначала по уровню, потом по стрику.
    """
    friend_ids = await db.get_friend_ids(user_id)
    rows = []
    for friend_id in friend_ids:
        friend = await db.get_user(friend_id)
        if not friend:
            continue
        rows.append(
            {
                "user_id": friend_id,
                "name": display_name(friend),
                "level": friend["level"],
                "streak": friend["streak"],
                "icon": await status_icon(friend_id),
            }
        )
    rows.sort(key=lambda r: (-r["level"], -r["streak"]))
    return rows


def render_list(friend_rows: list[dict], page: int) -> tuple[str, InlineKeyboardMarkup]:
    """
    Список друзей - это только заголовок + кликабельные кнопки (см.
    kb.friend_list_kb, где каждая кнопка уже несёт имя и иконку статуса
    друга). Само сообщение имена не дублирует текстом: один источник правды,
    и список не расползается, сколько бы друзей ни было.
    """
    header = "『 Отряд 』"

    if not friend_rows:
        text = f"{header}\n\nПока никого нет. Добавь друга командой /add_friend."
        return text, kb.friend_list_kb([], 0, 1)

    total_pages = max(1, (len(friend_rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    page_rows = friend_rows[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    return header, kb.friend_list_kb(page_rows, page, total_pages)


async def render_brief(friend: dict, viewer_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    level, xp_into_level, xp_needed = level_from_total_xp(friend["xp"])
    streak = friend["streak"]

    text = (
        f"『 {display_name(friend)} 』\n\n"
        f"🏆 Уровень: {level} ({xp_into_level}/{xp_needed} XP)\n"
        f"🔥 Серия: {streak} {ru_days(streak)}\n"
        f"📋 Испытание сегодня: {await status_label(friend['user_id'])}\n\n"
        f"{FOCUS_OPTIONS['pushups']['label']}: {friend['daily_pushups']}\n"
        f"{FOCUS_OPTIONS['squats']['label']}: {friend['daily_squats']}\n"
        f"{FOCUS_OPTIONS['abs']['label']}: {friend['daily_abs']}\n"
        f"{FOCUS_OPTIONS['pullups']['label']}: {friend['daily_pullups']}\n"
        f"{FOCUS_OPTIONS['running']['label']} (мин): {friend['daily_running']}\n"
        f"{FOCUS_OPTIONS['chess']['label']} (партий): {friend['daily_chess']}\n"
        f"{FOCUS_OPTIONS['reading']['label']} (страниц): {friend['daily_reading']}"
    )

    # Поддержать можно только активное (awaiting_action) испытание друга - если
    # сегодняшнее уже закрыто или ещё не выдано, поддерживать нечего, кнопка не
    # показывается вовсе (см. friends_list-концепцию, п.6.1).
    active = await db.get_active_challenge(friend["user_id"])
    show_cheer = bool(active)
    cheering = False
    if active:
        cheering = viewer_id in await db.get_cheer_supporter_ids(active["id"])

    markup = kb.friend_brief_kb(friend["user_id"], page, show_cheer, cheering)
    return text, markup
