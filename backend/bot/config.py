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
    # Сколько XP снимается с игрока за каждый пропущенный (просроченный) день.
    missed_day_xp_penalty: int = field(
        default_factory=lambda: int(_env("MISSED_DAY_XP_PENALTY", "300"))
    )

    # ---------- Автобэкап БД в приватный GitHub-репозиторий ----------
    # Если BACKUP_REPO_URL пустой - автобэкап просто не регистрируется в
    # планировщике (см. scheduler.setup_scheduler), никакой ошибки не будет.
    backup_repo_url: str = field(default_factory=lambda: _env("BACKUP_REPO_URL", ""))
    # Personal Access Token с правом push в этот репозиторий (repo scope).
    # Вшивается в HTTPS-урл на лету, в открытом виде нигде не логируется.
    backup_github_token: str = field(default_factory=lambda: _env("BACKUP_GITHUB_TOKEN", ""))
    backup_git_user_name: str = field(
        default_factory=lambda: _env("BACKUP_GIT_USER_NAME", "solo-leveling-bot")
    )
    backup_git_user_email: str = field(
        default_factory=lambda: _env("BACKUP_GIT_USER_EMAIL", "backup@bot.local")
    )
    # Два дня в неделю (cron day_of_week: mon/tue/wed/thu/fri/sat/sun), максимально
    # равноудалённых друг от друга на 7-дневной неделе (3 и 4 дня между ними).
    backup_day_1: str = field(default_factory=lambda: _env("BACKUP_DAY_1", "mon"))
    backup_day_2: str = field(default_factory=lambda: _env("BACKUP_DAY_2", "thu"))
    backup_time: str = field(default_factory=lambda: _env("BACKUP_TIME", "04:00"))
    # Если задан - в этот чат бот пришлёт сообщение, когда пуш бэкапа не удался
    # (например, просрочен токен). Необязательно.
    backup_notify_chat_id: str = field(default_factory=lambda: _env("BACKUP_NOTIFY_CHAT_ID", ""))

    @property
    def time_slots(self) -> dict:
        return {
            "morning": self.time_morning,
            "day": self.time_day,
            "evening": self.time_evening,
            "night": self.time_night,
        }


settings = Settings()

# ---------- Прогрессия опыта ----------
#
# Раньше стоимость уровня и награда за испытание были фиксированными (1000 XP на
# уровень, 100 XP за испытание всегда). Теперь и то, и другое растёт вместе с
# уровнем игрока: на старте прокачка быстрая и легко ощутимая, на высоких уровнях
# требует больше, но и награда за прохождение испытания выше.

# Стоимость перехода с уровня 1 на уровень 2.
XP_PER_LEVEL_BASE = 1000
# На сколько процентов дорожает КАЖДЫЙ следующий уровень относительно предыдущего.
XP_LEVEL_GROWTH = 0.12

# Базовая награда XP за одно завершённое испытание (на 1 уровне).
XP_PER_CHALLENGE_BASE = 100
# На сколько процентов растёт награда за испытание с каждым уровнем игрока.
XP_REWARD_GROWTH = 0.08

# Секретный бонус: если ВСЕ выбранные дисциплины довести до target * BONUS_MULTIPLIER,
# начисляется XP, равный BONUS_XP_MULTIPLIER наградам за обычное завершённое испытание
# (то есть x3 от xp_reward_for_challenge на текущем уровне игрока) - весомо, но
# без резких скачков уровня, которые сбивали прогрессию.
BONUS_MULTIPLIER = 2
BONUS_XP_MULTIPLIER = 3


def xp_required_for_level(level: int) -> int:
    """Сколько XP нужно набрать, чтобы перейти С этого уровня на следующий."""
    raw = XP_PER_LEVEL_BASE * (1 + XP_LEVEL_GROWTH) ** (level - 1)
    return max(10, int(round(raw / 10) * 10))  # округляем до десятков


def level_from_total_xp(total_xp: int) -> tuple[int, int, int]:
    """
    Переводит суммарный (пожизненный) XP игрока в (уровень, XP внутри уровня,
    XP нужно для следующего уровня). Уровень 1 стартует с 0 XP.
    """
    level = 1
    remaining = max(0, total_xp)
    while True:
        needed = xp_required_for_level(level)
        if remaining < needed:
            return level, remaining, needed
        remaining -= needed
        level += 1


def xp_for_n_levels(start_level: int, n: int) -> int:
    """Сколько всего XP нужно, чтобы подняться на n уровней начиная со start_level."""
    return sum(xp_required_for_level(start_level + i) for i in range(n))


def xp_reward_for_challenge(level: int) -> int:
    """Награда XP за завершённое дневное испытание - растёт вместе с уровнем игрока."""
    raw = XP_PER_CHALLENGE_BASE * (1 + XP_REWARD_GROWTH) ** (level - 1)
    return max(10, int(round(raw / 5) * 5))


# ---------- Прогрессия сложности испытаний ----------
#
# Дневная цель по каждой дисциплине растёт вместе с уровнем игрока: от разумного
# для новичка минимума (target_min) до потолка (target_max), который достигается
# к TARGET_GROWTH_CAP_LEVEL и дальше не растёт. target_step — во что округляется
# цель, чтобы цифры выглядели аккуратно (кратно 5, а не "63 отжимания").

TARGET_GROWTH_CAP_LEVEL = 50  # с этого уровня цель уже максимальная


def target_for_level(focus_key: str, level: int) -> int:
    opt = FOCUS_OPTIONS[focus_key]
    lo, hi, step = opt["target_min"], opt["target_max"], opt["target_step"]

    progress = min(max(level - 1, 0), TARGET_GROWTH_CAP_LEVEL - 1) / (TARGET_GROWTH_CAP_LEVEL - 1)
    raw = lo + (hi - lo) * progress
    value = int(round(raw / step) * step)
    return max(lo, min(hi, value))


# Доступные "фокусы" испытаний. Можно расширять сколько угодно.
# target_min — цель на 1 уровне (разумный старт для новичка)
# target_max — цель на TARGET_GROWTH_CAP_LEVEL и выше (потолок сложности)
FOCUS_OPTIONS = {
    "pushups": {
        "label": "💪 Отжимания",
        "unit": "раз",
        "counter_field": "daily_pushups",
        "kind": "physical",
        "target_min": 10,
        "target_max": 100,
        "target_step": 5,
    },
    "squats": {
        "label": "🍑 Приседания",
        "unit": "раз",
        "counter_field": "daily_squats",
        "kind": "physical",
        "target_min": 15,
        "target_max": 100,
        "target_step": 5,
    },
    "abs": {
        "label": "🍫 Пресс",
        "unit": "раз",
        "counter_field": "daily_abs",
        "kind": "physical",
        "target_min": 15,
        "target_max": 100,
        "target_step": 5,
    },
    "pullups": {
        "label": "🧗 Подтягивания",
        "unit": "раз",
        "counter_field": "daily_pullups",
        "kind": "physical",
        # Тяжелее отжиманий/приседаний только на старте, пока тело не привыкло
        # к нагрузке на хват и спину. С ростом уровня это выравнивается: можно
        # чередовать хваты (широкий/узкий/обратный) и тем самым разные группы
        # мышц, распределяя нагрузку и наращивая суммарный объём почти так же
        # свободно, как в других дисциплинах - поэтому потолок сложности не
        # должен оставаться сильно ниже остальных. 5 на старте - разумный
        # минимум для новичка, 90 у потолка сложности - серьёзный объём, но
        # уже сопоставимый с потолком отжиманий/приседаний/пресса (100), а не
        # искусственно урезанный вдвое-втрое.
        "target_min": 5,
        "target_max": 90,
        "target_step": 5,
    },
    "running": {
        "label": "🏃 Бег",
        "unit": "мин",
        "counter_field": "daily_running",
        "kind": "physical",
        # Измеряется в минутах, а не в километрах/раундах - так честнее для
        # разного уровня подготовки и не требует от игрока считать дистанцию.
        # 10 минут - лёгкая пробежка для новичка, 45 у потолка сложности -
        # уже полноценная кардио-тренировка, сопоставимая по нагрузке с
        # потолком остальных физических дисциплин.
        "target_min": 10,
        "target_max": 45,
        "target_step": 5,
    },
    "chess": {
        "label": "♟ Шахматы",
        "unit": "партий",
        "counter_field": "daily_chess",
        "kind": "mental",
        "target_min": 1,
        "target_max": 6,
        "target_step": 1,
    },
    "reading": {
        "label": "📖 Чтение",
        "unit": "страниц",
        "counter_field": "daily_reading",
        "kind": "mental",
        "target_min": 10,
        "target_max": 60,
        "target_step": 5,
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
