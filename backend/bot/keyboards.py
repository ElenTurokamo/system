from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import FOCUS_OPTIONS, TIME_OF_DAY_LABELS, time_of_day_label

KIND_DIVIDER_LABELS = {
    "physical": "── 💪 Физические ──",
    "mental": "── 🧠 Интеллектуальные ──",
}


def _grouped_focus_rows(focus_keys: list[str], row_builder) -> list[list[InlineKeyboardButton]]:
    """
    Строит кнопки, сгруппированные по типу дисциплины (физическая/интеллектуальная),
    с разделителем-подписью между группами. Разделитель не нужен, если среди
    переданных фокусов только один тип.
    """
    physical = [k for k in focus_keys if FOCUS_OPTIONS[k]["kind"] == "physical"]
    mental = [k for k in focus_keys if FOCUS_OPTIONS[k]["kind"] == "mental"]
    show_dividers = bool(physical) and bool(mental)

    rows: list[list[InlineKeyboardButton]] = []
    for kind, keys in (("physical", physical), ("mental", mental)):
        if not keys:
            continue
        if show_dividers:
            rows.append([InlineKeyboardButton(text=KIND_DIVIDER_LABELS[kind], callback_data="noop")])
        for key in keys:
            rows.append([row_builder(key)])
    return rows


def time_of_day_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=time_of_day_label(key), callback_data=f"time:{key}")]
        for key in TIME_OF_DAY_LABELS
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def focus_select_kb(selected: list[str]) -> InlineKeyboardMarkup:
    def build(key: str) -> InlineKeyboardButton:
        opt = FOCUS_OPTIONS[key]
        mark = "✅ " if key in selected else "▫️ "
        return InlineKeyboardButton(text=f"{mark}{opt['label']}", callback_data=f"focus_toggle:{key}")

    rows = _grouped_focus_rows(list(FOCUS_OPTIONS.keys()), build)
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
    def build(focus_key: str) -> InlineKeyboardButton:
        p = next(r for r in progress_rows if r["focus"] == focus_key)
        opt = FOCUS_OPTIONS[focus_key]

        markers = []
        if p["completed"]:
            markers.append("✅")
        if p["focus"] == active_focus:
            markers.append("🎯")

        prefix = f"{' '.join(markers)} " if markers else ""
        text = f"{prefix}{opt['label']} {p['amount']}/{p['target']}"
        return InlineKeyboardButton(text=text, callback_data=f"focus:{challenge_id}:{focus_key}")

    focus_keys = [p["focus"] for p in progress_rows]
    rows = _grouped_focus_rows(focus_keys, build)

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
