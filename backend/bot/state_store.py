"""
Дополнительная локальная БД бота: постоянное хранилище "ключей" сообщений и мелких
переменных, которые иначе жили бы только в памяти процесса.

Зачем отдельный файл
--------------------
Основная БД (db.sql, bot/database.py) уже хранит id сообщений профиля. Но этот файл
можно потерять или откатить: восстановление из автобэкапа (он делается два раза в
неделю), пересоздание тома, ручная подмена db.sql. После отката в основной БД остаются
СТАРЫЕ id: профиль уже не отредактировать ("message to edit not found"), бот присылает
новый, а прежний остаётся в чате дублем (если ему больше 48 ч - его не удалить вовсе).

Эта БД - независимая копия самого важного. Она:
  * лежит в отдельном файле (settings.state_db_path; в Docker - на отдельном томе);
  * НЕ входит в автобэкап (bot/backup.py копирует только db.sql) и поэтому не откатывается
    вместе с ним;
  * пишется "насквозь" (write-through) из Database при каждой смене ключа сообщения;
  * при старте сверяется с основной БД (Database.reconcile_message_keys): более свежая
    запись побеждает, пустое хранилище заполняется из основной БД.

Что здесь хранится
------------------
  message_keys    - ключи сообщений, которые бот потом РЕДАКТИРУЕТ/УДАЛЯЕТ: профиль (kind
                    'profile') и отчёт о провале (kind 'failure'). Запись - это
                    (чат, id сообщения, когда создано, когда менялась). Пустой message_id -
                    "надгробие": ключ очищен, и более старая запись из откатившейся основной
                    БД не должна его воскресить.
  kv              - мелкие переменные: кэш file_id PDF-гайда (bot/guide.py), чтобы после
                    перезапуска не загружать файл в Telegram заново.
  pending_deletes - очередь автоудаления эфемерных сообщений (ответы на команды, /help с
                    гайдом, список друзей - bot/utils.py): после перезапуска бота они
                    удаляются по расписанию, а не остаются в чате навсегда.

Принципы
--------
  * Хранилище вспомогательное: любой сбой (диск, права, блокировка файла) только
    логируется и НИКОГДА не ломает основной сценарий. Пока оно не подключено
    (state.connect не вызывался) - все методы ничего не делают.
  * Одно соединение aiosqlite, журнал по умолчанию (не WAL: на смонтированных томах Docker
    Desktop WAL ненадёжен) и немедленный commit после каждой записи.
"""
import logging
import os
from datetime import datetime
from typing import Any, Optional

import aiosqlite

from bot.config import settings

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS message_keys (
    kind        TEXT NOT NULL,       -- 'profile' | 'failure'
    user_id     INTEGER NOT NULL,
    chat_id     INTEGER,
    message_id  INTEGER,             -- NULL - надгробие (ключ очищен)
    created_at  TEXT,                -- UTC, когда сообщение было отправлено
    updated_at  TEXT NOT NULL,       -- UTC, когда менялась ЭТА запись (для сверки с основной БД)
    PRIMARY KEY (kind, user_id)
);

CREATE TABLE IF NOT EXISTS kv (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS pending_deletes (
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    delete_at   REAL NOT NULL,       -- unix-время (time.time()), когда сообщение пора удалить
    command_key TEXT,                -- ключ команды для правила "одно сообщение на команду"
    PRIMARY KEY (chat_id, message_id)
);
"""


class StateStore:
    def __init__(self, path: Optional[str] = None):
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None

    # ---------- жизненный цикл ----------

    @property
    def connected(self) -> bool:
        return self._conn is not None

    async def connect(self, path: Optional[str] = None) -> bool:
        """
        Открывает (и при необходимости создаёт) файл хранилища. Возвращает True, если
        хранилище доступно. Бот должен работать и без него, поэтому сбой открытия не
        пробрасывается - только логируется (остальные методы тогда ничего не делают).
        """
        if self._conn is not None:
            await self.close()
        self.path = path or self.path or settings.state_db_path
        try:
            directory = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(directory, exist_ok=True)
            conn = await aiosqlite.connect(self.path)
            conn.row_factory = aiosqlite.Row
            await conn.executescript(SCHEMA)
            await conn.commit()
            self._conn = conn
            return True
        except Exception as e:  # noqa: BLE001 - хранилище вспомогательное
            logger.error(
                "Не удалось открыть локальное хранилище состояния %s: %s. "
                "Бот продолжит работу без него.", self.path, e,
            )
            self._conn = None
            return False

    async def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await conn.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("Ошибка при закрытии хранилища состояния: %s", e)

    async def _write(self, sql: str, params: tuple = (), what: str = "запись") -> bool:
        if self._conn is None:
            return False
        try:
            await self._conn.execute(sql, params)
            await self._conn.commit()
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("Хранилище состояния: не удалась %s: %s", what, e)
            return False

    async def _read(self, sql: str, params: tuple = (), what: str = "чтение") -> list[dict]:
        if self._conn is None:
            return []
        try:
            cur = await self._conn.execute(sql, params)
            return [dict(r) for r in await cur.fetchall()]
        except Exception as e:  # noqa: BLE001
            logger.warning("Хранилище состояния: не удалось %s: %s", what, e)
            return []

    # ---------- ключи сообщений ----------

    async def put_message_key(
        self,
        kind: str,
        user_id: int,
        chat_id: Optional[int],
        message_id: Optional[int],
        created_at: Optional[str] = None,
    ) -> bool:
        """
        Сохраняет ключ сообщения игрока. message_id=None - надгробие (сообщения больше нет).
        Для живого ключа updated_at совпадает с created_at (если он известен): тот же момент,
        что записан в основной БД, - по этим отметкам при сверке определяется, чья запись свежее.
        """
        now = datetime.utcnow().isoformat()
        if message_id is None:
            chat_id, created_at, updated_at = None, None, now
        else:
            # created_at может быть неизвестен (профиль, созданный до появления отметки) - тогда
            # он так и хранится пустым: выдумывать "сейчас" нельзя, по нему считается возраст
            # сообщения (граница редактирования Telegram - 48 ч).
            updated_at = created_at or now
        return await self._write(
            "INSERT INTO message_keys (kind, user_id, chat_id, message_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(kind, user_id) DO UPDATE SET chat_id = excluded.chat_id, "
            "message_id = excluded.message_id, created_at = excluded.created_at, "
            "updated_at = excluded.updated_at",
            (kind, user_id, chat_id, message_id, created_at, updated_at),
            what=f"запись ключа {kind}/{user_id}",
        )

    async def get_message_key(self, kind: str, user_id: int) -> Optional[dict]:
        rows = await self._read(
            "SELECT * FROM message_keys WHERE kind = ? AND user_id = ?",
            (kind, user_id),
            what=f"чтение ключа {kind}/{user_id}",
        )
        return rows[0] if rows else None

    async def list_message_keys(self, kind: Optional[str] = None) -> list[dict]:
        if kind is None:
            return await self._read("SELECT * FROM message_keys", what="чтение ключей сообщений")
        return await self._read(
            "SELECT * FROM message_keys WHERE kind = ?", (kind,), what="чтение ключей сообщений"
        )

    # ---------- мелкие переменные ----------

    async def kv_get(self, key: str) -> Optional[str]:
        rows = await self._read("SELECT value FROM kv WHERE key = ?", (key,), what=f"чтение {key}")
        return rows[0]["value"] if rows else None

    async def kv_set(self, key: str, value: str) -> bool:
        return await self._write(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, datetime.utcnow().isoformat()),
            what=f"запись {key}",
        )

    async def kv_delete(self, key: str) -> bool:
        return await self._write("DELETE FROM kv WHERE key = ?", (key,), what=f"удаление {key}")

    # ---------- очередь автоудаления ----------

    async def put_pending_delete(
        self, chat_id: int, message_id: int, delete_at: float, command_key: Optional[str] = None
    ) -> bool:
        return await self._write(
            "INSERT INTO pending_deletes (chat_id, message_id, delete_at, command_key) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id, message_id) DO UPDATE SET delete_at = excluded.delete_at, "
            "command_key = COALESCE(excluded.command_key, pending_deletes.command_key)",
            (chat_id, message_id, float(delete_at), command_key),
            what=f"запись автоудаления {chat_id}/{message_id}",
        )

    async def drop_pending_delete(self, chat_id: int, message_id: int) -> bool:
        return await self._write(
            "DELETE FROM pending_deletes WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
            what=f"снятие автоудаления {chat_id}/{message_id}",
        )

    async def list_pending_deletes(self) -> list[dict[str, Any]]:
        return await self._read(
            "SELECT * FROM pending_deletes ORDER BY delete_at ASC", what="чтение очереди автоудаления"
        )


state = StateStore()
