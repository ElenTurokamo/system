"""
Автобэкап баз данных в приватный GitHub-репозиторий. В репозиторий уходят ДВА файла:

  * db.sql        - основная БД: профили, статистика и историческая прогрессия игроков
                    (bot/database.py);
  * bot_state.db  - БД состояния с временными переменными бота: ключи сообщений профиля и
                    отчёта о провале, кэш file_id PDF-гайда, очередь автоудаления
                    (bot/state_store.py).

Работает через обычный git CLI (должен быть установлен в образе — см.
Dockerfile), а не через API GitHub, поэтому не тянет лишних зависимостей.

Каждый запуск клонирует репозиторий "с нуля" во временную папку, кладёт туда
согласованные снимки обеих БД, коммитит (если что-то реально изменилось) и пушит,
после чего временная папка удаляется целиком. Персистентной рабочей копии
внутри контейнера намеренно нет: репозиторий крохотный (два sqlite-файла),
так что полный клон дешевле, чем таскать .git между рестартами контейнера
и разбираться с возможным рассинхроном.

Снимок БД делается через онлайн-бэкап SQLite (sqlite3.Connection.backup), а не простым
копированием файла: бот пишет в БД во время работы, и "живая" копия файла могла бы
оказаться в середине записи. Если онлайн-бэкап не удался - запасной вариант: обычная копия.
"""
import asyncio
import logging
import shutil
import sqlite3
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


def _sqlite_snapshot(src: Path, dst: Path) -> None:
    """Согласованный снимок живой SQLite-БД через онлайн-бэкап (читает src, пишет новый файл dst)."""
    src_conn = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


async def _stage_db_file(src: Path, dst: Path) -> bool:
    """
    Кладёт снимок БД src в dst. Сначала онлайн-бэкап SQLite (в отдельном потоке, чтобы не
    блокировать event loop), при сбое - обычное копирование файла. True - файл подготовлен.
    """
    # Файл из клона удаляется заранее: снимок в "чистый" файл детерминирован, и если данные не
    # менялись, git не увидит изменений (бэкап в уже существующий файл меняет служебный счётчик).
    try:
        if dst.exists():
            dst.unlink()
        await asyncio.to_thread(_sqlite_snapshot, src, dst)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("Онлайн-снимок %s не удался (%s) - копирую файл как есть.", src.name, e)
    try:
        if dst.exists():
            dst.unlink()
        await asyncio.to_thread(shutil.copy2, src, dst)
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("Не удалось подготовить %s к бэкапу: %s", src.name, e)
        return False


def _backup_names() -> tuple[str, str]:
    """Имена файлов в репозитории: (основная БД, БД состояния). Не должны совпадать."""
    main_name = Path(settings.db_path).name
    state_name = Path(settings.state_db_path).name
    if state_name == main_name:
        state_name = "bot_state.db"
    return main_name, state_name


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

        main_name, state_name = _backup_names()
        if not await _stage_db_file(db_source, Path(tmp_dir) / main_name):
            await _notify_failure(bot, f"Бэкап БД: не удалось подготовить {main_name}.")
            return

        # БД состояния - вспомогательная: её отсутствие или сбой снимка не должны мешать
        # бэкапу основной БД (бот и сам работает без неё - см. bot/state_store.py).
        state_source = Path(settings.state_db_path)
        if state_source.exists():
            if not await _stage_db_file(state_source, Path(tmp_dir) / state_name):
                await _notify_failure(
                    bot, f"Бэкап БД: не удалось подготовить {state_name} (основная БД сохранена)."
                )
        else:
            logger.info("Файл БД состояния %s не найден - в бэкап не попадёт.", state_source)

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
