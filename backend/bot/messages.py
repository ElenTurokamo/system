"""
Генератор текстов в стиле Solo Leveling.

Вместо файла с 10 000 захардкоженных строк (которые неизбежно были бы
однообразными и в основном "водой"), тексты собираются на лету из banка
фрагментов (data/messages.json): system_prefix + opener + body + closer.

Даже с текущим (сравнительно небольшим) банком фраз число уникальных
комбинаций для одной только категории challenge_start:
20 (prefixes) * 30 (openers) * 8 (bodies) * 8 (closers) = 38 400 вариантов.
Банк можно расширять — комбинаторика растёт мультипликативно, а не линейно,
так что реального потолка в "тысячи разных диалогов" достичь легко без
раздувания репозитория до мегабайтов текста.
"""
import json
import random
from pathlib import Path
from typing import Optional

_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "messages.json"
with open(_DATA_PATH, "r", encoding="utf-8") as f:
    _BANK = json.load(f)


def _prefix() -> str:
    return random.choice(_BANK["system_prefixes"])


def _pick(category: str, part: str) -> Optional[str]:
    node = _BANK.get(category, {})
    options = node.get(part)
    return random.choice(options) if options else None


def _compose(category: str, **fmt) -> str:
    """Собирает opener/body/closer (что есть в банке) в одно сообщение."""
    parts = [_prefix()]
    for part in ("openers", "body_templates", "closers"):
        text = _pick(category, part)
        if text:
            parts.append(text.format(**fmt))
    return "\n\n".join(parts)


_QUEST_HEADER = "[ Информация о квесте ]"

# Короткие варианты второй строки карточки испытания. Единственное, что в этом
# сообщении допускается варьировать - сама карточка всегда состоит только из
# заголовка + этой строки (остальное - разделитель и таймер, см. challenge_render.py).
_QUEST_TAGLINES = [
    "Тренируйся, чтобы стать сильнее и повысить свой уровень.",
    "Выполни упражнения ниже — Система зафиксирует прогресс.",
    "Заверши испытание, чтобы получить опыт и вырасти.",
    "Каждый подход приближает тебя к следующему уровню.",
    "Дисциплина сегодня — сила завтра.",
    "Каждое движение фиксируется. Ты под наблюдением Системы.",
    "Отказ от испытания не предусмотрен протоколом.",
    "Слабость — временное состояние. Действие меняет это.",
    "Тени не спрашивают разрешения расти. Не спрашивай и ты.",
    "Испытание сгенерировано. Закрой все пункты — получи награду.",
    "Игрок, время не бесконечно. Начинай.",
    "Рост — единственный путь избежать штрафа.",
    "Слабый охотник не доживает до следующего ранга.",
    "Каждый повтор — вклад в будущий уровень.",
    "Система не прощает бездействия.",
    "Подтверди готовность действием, а не словами.",
    "Врата испытания закроются независимо от твоей готовности.",
    "Твой ранг определяется тем, что ты делаешь сейчас.",
    "Дисциплина сегодня отменяет штраф завтра.",
    "Система ждёт результата, а не намерений.",
    "Промедление засчитывается как отказ.",
    "Ты либо усиливаешься, либо теряешь позиции.",
    "Каждое испытание — проверка, а не формальность.",
    "Слабость не защищена от последствий.",
    "Начни. Отчёт Системе формируется в реальном времени.",
    "Тело — инструмент. Заточи его сегодня.",
    "Игнорировать испытание — значит выбрать штраф.",
    "Полное выполнение — единственный принятый результат.",
    "Система регистрирует прогресс, а не оправдания.",
    "Слабые звенья ломаются первыми. Укрепись сейчас.",
]


def challenge_start(streak: int = 0, level: int = 0, xp: int = 0, timeout: int = 0) -> str:
    """
    Заголовок карточки испытания. Формат жёстко фиксирован (заголовок + одна
    короткая строка) - без "воды" из прежнего банка фраз. Параметры streak/level/
    xp/timeout сохранены в сигнатуре только для совместимости вызова из
    scheduler.py, в тексте больше не используются (эта информация и так видна
    в профиле и в таймере карточки).
    """
    return f"{_QUEST_HEADER}\n\n{random.choice(_QUEST_TAGLINES)}"


def focus_selected(focus_label: str, unit: str) -> str:
    return _compose("focus_selected", focus_label=focus_label, unit=unit)


def amount_logged(amount: int, unit: str, focus_label: str) -> str:
    return _compose("amount_logged", amount=amount, unit=unit, focus_label=focus_label)


def success(streak: int, xp_gained: int, xp: int, level: int) -> str:
    return _compose("success", streak=streak, xp_gained=xp_gained, xp=xp, level=level)


def photo_caption(streak: int, user_id: int, physical_bonus_claimed: bool = False) -> str:
    """
    Ежедневный отчёт в группу (подпись к фото выполнения физических дисциплин).
    Формат жёстко фиксирован - без "воды" из банка случайных фраз (как и у
    challenge_start): один и тот же текст день за днём, меняются только
    streak/user_id.

    physical_bonus_claimed - личный x2 достигнут по ВСЕМ выбранным физическим
    дисциплинам (см. Database.physical_bonus_achieved). Это НЕ то же самое, что
    полный секретный бонус challenges.bonus_claimed (x2 XP), который требует x2
    сразу по всем дисциплинам, включая интеллектуальные, и даётся независимо от
    этого отчёта при закрытии всего испытания - используем более узкий,
    "физический" критерий специально для этой строки, чтобы не заставлять
    задерживать фото-отчёт ради шахмат/чтения, пока не прошёл памп.
    """
    text = f"Отчёт дня {streak}. Игрок {user_id} завершил испытание."
    if physical_bonus_claimed:
        text += f"\n\n🌟 В день {streak} также пройдено секретное испытание."
    return text


def give_up(xp_loss: int = 0, penalty_hours: int = 0) -> str:
    return _compose("give_up", xp_loss=xp_loss, penalty_hours=penalty_hours)


def expired(xp_loss: int = 0, penalty_hours: int = 0) -> str:
    return _compose("expired", xp_loss=xp_loss, penalty_hours=penalty_hours)


def level_up(level: int) -> str:
    template = random.choice(_BANK["level_up"]["templates"])
    return template.format(level=level)


def streak_milestone(streak: int) -> str:
    template = random.choice(_BANK["streak_milestone"]["templates"])
    return template.format(streak=streak)


def registration_welcome() -> str:
    return f"{_prefix()}\n\n{random.choice(_BANK['registration_welcome']['templates'])}"


def registration_done() -> str:
    return f"{_prefix()}\n\n{random.choice(_BANK['registration_done']['templates'])}"


def secret_bonus(focus_label: str, levels: int) -> str:
    return _compose("secret_bonus", focus_label=focus_label, levels=levels)
