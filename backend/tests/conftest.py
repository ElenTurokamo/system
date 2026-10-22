"""
Окружение для симуляционных тестов. Важно: переменные задаются ДО импорта bot.*,
иначе config.py подхватит настоящий .env (токен, репозиторий бэкапов и т.п.).
"""
import os
import sys
import tempfile

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_BACKEND)
sys.path.insert(0, _BACKEND)

os.environ["BOT_TOKEN"] = "123456:TEST"
os.environ["TZ"] = "Asia/Almaty"
os.environ["TIME_MORNING"] = "05:00"
os.environ["TIME_DAY"] = "13:00"
os.environ["TIME_EVENING"] = "19:00"
os.environ["TIME_NIGHT"] = "23:00"
os.environ["CHALLENGE_TIMEOUT_HOURS"] = "18"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "boot.sql")
os.environ["BACKUP_REPO_URL"] = ""
os.environ["BACKUP_GITHUB_TOKEN"] = ""
os.environ["PROFILE_RETRY_DELAY_SECONDS"] = "0"
os.environ["PROFILE_EDIT_RETRY_INTERVAL_SECONDS"] = "0"
