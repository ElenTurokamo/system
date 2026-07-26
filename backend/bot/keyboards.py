from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import FOCUS_OPTIONS, TIME_OF_DAY_LABELS


def time_of_day_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"time:{key}")]
        for key, label in TIME_OF_DAY_LABELS.items()
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


def challenge_kb(challenge_id: int, focuses: list[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=FOCUS_OPTIONS[f]["label"], callback_data=f"focus:{challenge_id}:{f}")]
        for f in focuses
    ]
    rows.append([InlineKeyboardButton(text="🏳 Сдаться", callback_data=f"giveup:{challenge_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def photo_prompt_kb(challenge_id: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="Пропустить фото", callback_data=f"skipphoto:{challenge_id}")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)
