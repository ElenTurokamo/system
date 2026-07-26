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


def challenge_start(streak: int, level: int, xp: int, timeout: int) -> str:
    return _compose(
        "challenge_start", streak=streak, level=level, xp=xp, timeout=timeout
    )


def focus_selected(focus_label: str, unit: str) -> str:
    return _compose("focus_selected", focus_label=focus_label, unit=unit)


def amount_logged(amount: int, unit: str, focus_label: str) -> str:
    return _compose("amount_logged", amount=amount, unit=unit, focus_label=focus_label)


def success(streak: int, xp_gained: int, xp: int, level: int) -> str:
    return _compose("success", streak=streak, xp_gained=xp_gained, xp=xp, level=level)


def photo_caption(streak: int, user_id: int) -> str:
    template = random.choice(_BANK["photo_posted"]["templates"])
    return template.format(streak=streak, user_id=user_id)


def give_up(penalty_hours: int) -> str:
    return _compose("give_up", penalty_hours=penalty_hours)


def expired() -> str:
    return _compose("expired")


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
