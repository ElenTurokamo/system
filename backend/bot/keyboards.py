from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import FOCUS_OPTIONS, TIME_OF_DAY_LABELS, time_of_day_label


def time_of_day_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=time_of_day_label(key), callback_data=f"time:{key}")]
        for key in TIME_OF_DAY_LABELS
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def focus_select_kb(selected: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for key, opt in FOCUS_OPTIONS.items():
        mark = "✅ " if key in selected else "▫️ "
        rows.append(
            [InlineKeyboardButton(text=f"{mark}{opt['label']}", callback_data=f"focus_toggle:{key}")]
        )
    rows.append([InlineKeyboardButton(text="Готово ➜", callback_data="focus_done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_binding_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📖 Как привязать группу", callback_data="group_instructions")],
        [InlineKeyboardButton(text="Пропустить ➜", callback_data="group_skip")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def challenge_kb(
    challenge_id: int,
    progress_rows: list[dict],
    active_focus: str | None,
    show_finish: bool = False,
    show_skip_physical_photo: bool = False,
) -> InlineKeyboardMarkup:
    rows = []
    for p in progress_rows:
        opt = FOCUS_OPTIONS[p["focus"]]

        # Статусный смайлик (максимум один): показывает, в каком состоянии
        # прогресс по дисциплине. 🔥 огонёк (личный x2) считается "сильнее"
        # ✅ выполнено и подменяет его - огонёк математически всегда означает,
        # что цель тоже выполнена (x2 target >= target), поэтому дублировать
        # оба смысла не нужно.
        if p["bonus_claimed"]:
            marker = "🔥"
        elif p["completed"]:
            marker = "✅"
        else:
            marker = ""

        prefix = f"{marker} " if marker else ""
        label = f"{prefix}{opt['label']} {p['amount']}/{p['target']}"

        # То, что дисциплина сейчас ВЫБРАНА (активный фокус для ввода подходов) -
        # это отдельная, независимая от статуса вещь: игрок может сфокусироваться
        # и на уже выполненной, и на дисциплине с огоньком, чтобы продолжить
        # копить подходы сверху. Поэтому вместо ещё одного конкурирующего
        # смайлика (который перебивал бы 🔥/✅ и терялся бы под ними) фокус
        # обозначается обрамлением всей подписи кнопки - не эмодзи, а простыми
        # символами, так что итоговый смайлик на кнопке всегда ровно один,
        # а какая дисциплина выбрана - видно независимо от него.
        text = f"» {label} «" if p["focus"] == active_focus else label
        rows.append(
            [InlineKeyboardButton(text=text, callback_data=f"focus:{challenge_id}:{p['focus']}")]
        )

    if show_skip_physical_photo:
        rows.append(
            [InlineKeyboardButton(text="Пропустить фото", callback_data=f"skipphysicalphoto:{challenge_id}")]
        )

    if show_finish:
        rows.append(
            [InlineKeyboardButton(text="🏁 Завершить испытание", callback_data=f"finish:{challenge_id}")]
        )

    rows.append([InlineKeyboardButton(text="🏳 Сдаться", callback_data=f"giveup:{challenge_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def friend_request_kb(friendship_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="✅", callback_data=f"freq:{friendship_id}:accept"),
            InlineKeyboardButton(text="❌", callback_data=f"freq:{friendship_id}:decline"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def friend_list_kb(friend_rows: list[dict], page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows = []
    for f in friend_rows:
        text = f"{f['name']} {f['icon']}"
        rows.append(
            [InlineKeyboardButton(text=text, callback_data=f"flist:open:{f['user_id']}:{page}")]
        )

    # Стрелочки листания - только если друзей больше, чем помещается на одну
    # страницу (см. friends.PAGE_SIZE), иначе строка навигации просто не нужна.
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"flist:page:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="flist:noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"flist:page:{page + 1}"))
        rows.append(nav)

    return InlineKeyboardMarkup(inline_keyboard=rows)


def friend_brief_kb(
    friend_user_id: int, page: int, show_cheer: bool, cheering: bool
) -> InlineKeyboardMarkup:
    rows = []
    if show_cheer:
        label = "💛 Поддержка отправлена" if cheering else "🤝 Поддержать"
        rows.append(
            [InlineKeyboardButton(text=label, callback_data=f"flist:cheer:{friend_user_id}:{page}")]
        )
    rows.append([InlineKeyboardButton(text="⬅ Назад", callback_data=f"flist:page:{page}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
