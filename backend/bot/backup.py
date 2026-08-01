"""
Автобэкап базы данных (db.sql — там же лежат и профили, и историческая
прогрессия, см. bot/database.py) в приватный GitHub-репозиторий.

Работает через обычный git CLI (должен быть установлен в образе — см.
Dockerfile), а не через API GitHub, поэтому не тянет лишних зависимостей.

Каждый запуск клонирует репозиторий "с нуля" во временную папку, копирует
туда актуальный файл БД, коммитит (если что-то реально изменилось) и пушит,
после чего временная папка удаляется целиком. Персистентной рабочей копии
внутри контейнера намеренно нет: репозиторий крохотный (один sqlite-файл),
так что полный клон дешевле, чем таскать .git между рестартами контейнера
и разбираться с возможным рассинхроном.
"""
import asyncio
import logging
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from aiogram import Bot

from bot.config import settings

logger = logging.getLogger(__name__)


def _authenticated_url() -> str:
    """Вшивает GitHub-токен в HTTPS-урл репозитория, чтобы push отработал
    без интерактивного логина. Если токен не задан - урл остаётся как есть
    (пригодится, если git настроен на аутентификацию как-то иначе, например
    через смонтированный ~/.netrc)."""
    url = settings.backup_repo_url
    if settings.backup_github_token and url.startswith("https://"):
        return url.replace(
            "https://", f"https://x-access-token:{settings.backup_github_token}@", 1
        )
    return url


async def _run(cmd: list[str], cwd: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def _notify_failure(bot: Bot | None, text: str):
    if not bot or not settings.backup_notify_chat_id:
        return
    try:
        await bot.send_message(int(settings.backup_notify_chat_id), f"⚠️ {text}")
    except Exception as e:
        logger.warning("Не удалось отправить уведомление о сбое бэкапа: %s", e)


async def run_backup(bot: Bot | None = None):
    if not settings.backup_repo_url:
        logger.info("BACKUP_REPO_URL не задан - автобэкап пропущен.")
        return

    db_source = Path(settings.db_path)
    if not db_source.exists():
        logger.warning("Файл БД %s не найден - бэкап пропущен.", db_source)
        return

    tmp_dir = tempfile.mkdtemp(prefix="db_backup_")
    try:
        code, _, err = await _run(
            ["git", "clone", "--depth", "1", _authenticated_url(), tmp_dir], cwd="/tmp"
        )
        if code != 0:
            # Свежесозданный на GitHub репозиторий без единого коммита клонируется
            # с ошибкой ("repository is empty") - это не сбой, просто такого удалённого
            # состояния ещё нет. В этом случае инициализируем локально и привяжем remote.
            logger.info("git clone не удался (репозиторий пуст?): %s", err.strip())
            code, _, err = await _run(["git", "init"], cwd=tmp_dir)
            if code != 0:
                logger.error("Не удалось инициализировать репозиторий бэкапа: %s", err)
                await _notify_failure(bot, "Бэкап БД: не удалось инициализировать git-репозиторий.")
                return
            await _run(["git", "checkout", "-b", "main"], cwd=tmp_dir)
            await _run(["git", "remote", "add", "origin", _authenticated_url()], cwd=tmp_dir)

        await _run(["git", "config", "user.name", settings.backup_git_user_name], cwd=tmp_dir)
        await _run(["git", "config", "user.email", settings.backup_git_user_email], cwd=tmp_dir)

        shutil.copy2(db_source, Path(tmp_dir) / db_source.name)

        await _run(["git", "add", "-A"], cwd=tmp_dir)
        _, status_out, _ = await _run(["git", "status", "--porcelain"], cwd=tmp_dir)
        if not status_out.strip():
            logger.info("Бэкап БД: изменений с прошлого раза нет, коммит пропущен.")
            return

        stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        code, _, err = await _run(["git", "commit", "-m", f"backup: {stamp}"], cwd=tmp_dir)
        if code != 0:
            logger.error("git commit не удался: %s", err)
            await _notify_failure(bot, f"Бэкап БД: git commit не удался ({err.strip()[:200]}).")
            return

        code, _, err = await _run(["git", "push", "origin", "HEAD:main"], cwd=tmp_dir)
        if code != 0:
            logger.error("git push не удался: %s", err)
            await _notify_failure(bot, f"Бэкап БД: git push не удался ({err.strip()[:200]}).")
            return

        logger.info("Бэкап БД успешно запушен (%s).", stamp)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
