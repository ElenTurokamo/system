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

# Доступные "фокусы" испытаний. Можно расширять сколько угодно.
FOCUS_OPTIONS = {
    "pushups": {
        "label": "🥊 Отжимания",
        "unit": "раз",
        "counter_field": "daily_pushups",
        "kind": "physical",
    },
    "squats": {
        "label": "🦵 Приседания",
        "unit": "раз",
        "counter_field": "daily_squats",
        "kind": "physical",
    },
    "abs": {
        "label": "🔥 Пресс",
        "unit": "раз",
        "counter_field": "daily_abs",
        "kind": "physical",
    },
    "chess": {
        "label": "♟ Шахматы",
        "unit": "партий",
        "counter_field": "daily_chess",
        "kind": "mental",
    },
    "reading": {
        "label": "📖 Чтение",
        "unit": "страниц",
        "counter_field": "daily_reading",
        "kind": "mental",
    },
}

TIME_OF_DAY_LABELS = {
    "morning": "🌅 Утром",
    "day": "🌞 Днём",
    "evening": "🌇 Вечером",
    "night": "🌙 Ночью",
}
