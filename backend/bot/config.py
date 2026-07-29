import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


@dataclass
class Settings:
    bot_token: str = field(default_factory=lambda: os.environ["BOT_TOKEN"])
    tz: str = field(default_factory=lambda: _env("TZ", "Asia/Almaty"))
    db_path: str = field(default_factory=lambda: _env("DB_PATH", "./data/db.sql"))

    time_morning: str = field(default_factory=lambda: _env("TIME_MORNING", "05:00"))
    time_day: str = field(default_factory=lambda: _env("TIME_DAY", "13:00"))
    time_evening: str = field(default_factory=lambda: _env("TIME_EVENING", "19:00"))
    time_night: str = field(default_factory=lambda: _env("TIME_NIGHT", "23:00"))

    challenge_timeout_hours: int = field(
        default_factory=lambda: int(_env("CHALLENGE_TIMEOUT_HOURS", "18"))
    )
    penalty_hours: int = field(default_factory=lambda: int(_env("PENALTY_HOURS", "48")))

    @property
    def time_slots(self) -> dict:
        return {
            "morning": self.time_morning,
            "day": self.time_day,
            "evening": self.time_evening,
            "night": self.time_night,
        }


settings = Settings()

# XP, необходимый для получения одного уровня
XP_PER_CHALLENGE = 100
XP_PER_LEVEL = 1000

# Секретный бонус: если ВСЕ выбранные дисциплины довести до target * BONUS_MULTIPLIER,
# начисляется BONUS_LEVELS уровней разом (один раз за испытание).
BONUS_MULTIPLIER = 2
BONUS_LEVELS = 5

# Доступные "фокусы" испытаний. Можно расширять сколько угодно.
# target — дневная цель по этому фокусу
FOCUS_OPTIONS = {
    "pushups": {
        "label": "🥊 Отжимания",
        "unit": "раз",
        "counter_field": "daily_pushups",
        "kind": "physical",
        "target": 50,
    },
    "squats": {
        "label": "🦵 Приседания",
        "unit": "раз",
        "counter_field": "daily_squats",
        "kind": "physical",
        "target": 50,
    },
    "abs": {
        "label": "🔥 Пресс",
        "unit": "раз",
        "counter_field": "daily_abs",
        "kind": "physical",
        "target": 50,
    },
    "chess": {
        "label": "♟ Шахматы",
        "unit": "партий",
        "counter_field": "daily_chess",
        "kind": "mental",
        "target": 5,
    },
    "reading": {
        "label": "📖 Чтение",
        "unit": "страниц",
        "counter_field": "daily_reading",
        "kind": "mental",
        "target": 20,
    },
}

TIME_OF_DAY_LABELS = {
    "morning": "🌅 Утром",
    "day": "🌞 Днём",
    "evening": "🌇 Вечером",
    "night": "🌙 Ночью",
}


def time_of_day_label(key: str) -> str:
    """Метка времени суток вместе с точным временем отправки, например '🌅 Утром (05:00)'."""
    return f"{TIME_OF_DAY_LABELS[key]} ({settings.time_slots[key]})"
