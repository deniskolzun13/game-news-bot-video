"""Система миграций схемы БД с версионированием.

Поддерживает:
- Последовательное применение SQL-миграций из каталога
- Таблицу schema_version для отслеживания применённых миграций
- Откат (down) миграций при необходимости
- Абстракцию Storage для возможной замены SQLite на Postgres
"""
import logging
import os
import re
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("migrations")


@dataclass
class Migration:
    """Представляет одну миграцию."""
    version: int
    name: str
    up_sql: str
    down_sql: str = ""


class MigrationManager:
    """Управляет применением миграций к БД."""

    def __init__(self, db_path: str, migrations_dir: str | Path):
        self._db_path = db_path
        self._migrations_dir = Path(migrations_dir)
        self._conn: Optional[sqlite3.Connection] = None

    def _get_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _ensure_schema_table(self) -> None:
        conn = self._get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()

    def get_current_version(self) -> int:
        """Возвращает текущую версию схемы (0 если таблицы нет)."""
        self._ensure_schema_table()
        conn = self._get_connection()
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return row[0] if row and row[0] is not None else 0

    def get_applied_migrations(self) -> list[int]:
        """Возвращает список применённых версий."""
        self._ensure_schema_table()
        conn = self._get_connection()
        rows = conn.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
        return [r[0] for r in rows]

    def load_migrations(self) -> list[Migration]:
        """Загружает миграции из SQL-файлов в каталоге.

        Ожидается структура:
        migrations/
          001_initial_schema.sql
          002_add_video_fields.sql
          ...

        Формат файла:
        -- UP
        CREATE TABLE ...
        -- DOWN
        DROP TABLE ...
        """
        migrations = []
        for file_path in sorted(self._migrations_dir.glob("*.sql")):
            match = re.match(r"(\d+)_([^.]+)\.sql", file_path.name)
            if not match:
                log.warning("Пропущен файл миграции (неверное имя): %s", file_path.name)
                continue

            version = int(match.group(1))
            name = match.group(2)
            content = file_path.read_text(encoding="utf-8")

            # Разбираем UP и DOWN секции
            up_sql = ""
            down_sql = ""
            current_section = None
            for line in content.splitlines():
                if line.strip().upper() == "-- UP":
                    current_section = "up"
                    continue
                elif line.strip().upper() == "-- DOWN":
                    current_section = "down"
                    continue
                if current_section == "up":
                    up_sql += line + "\n"
                elif current_section == "down":
                    down_sql += line + "\n"

            if not up_sql.strip():
                log.warning("Миграция %s: пустая UP секция", file_path.name)
                continue

            migrations.append(Migration(
                version=version,
                name=name,
                up_sql=up_sql.strip(),
                down_sql=down_sql.strip(),
            ))

        return sorted(migrations, key=lambda m: m.version)

    def migrate(self, target_version: Optional[int] = None) -> int:
        """Применяет миграции до целевой версии (или до последней).

        Возвращает количество применённых миграций.
        """
        self._ensure_schema_table()
        migrations = self.load_migrations()
        current = self.get_current_version()

        if target_version is None:
            target_version = max((m.version for m in migrations), default=current)

        if target_version < current:
            log.warning("Запрошен откат с %d на %d — down-миграции не реализованы", current, target_version)
            return 0

        applied = 0
        conn = self._get_connection()
        for migration in migrations:
            if migration.version <= current:
                continue
            if migration.version > target_version:
                break

            log.info("Применение миграции %d: %s", migration.version, migration.name)
            try:
                conn.executescript(migration.up_sql)
                conn.execute(
                    "INSERT INTO schema_version (version, name) VALUES (?, ?)",
                    (migration.version, migration.name),
                )
                conn.commit()
                applied += 1
            except Exception as exc:
                conn.rollback()
                log.error("Ошибка применения миграции %d: %s", migration.version, exc)
                raise

        if applied:
            log.info("Применено миграций: %d", applied)
        else:
            log.info("Миграции не требуются (текущая версия: %d)", current)

        return applied

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


# --- Абстракция Storage для поддержки разных БД ---

class StorageBackend(ABC):
    """Абстрактный интерфейс хранилища.

    Позволяет заменить SQLite на Postgres без переписывания бизнес-логики.
    """

    @abstractmethod
    def close(self) -> None:
        pass

    # --- published ---
    @abstractmethod
    def is_published(self, url: str) -> bool: ...

    @abstractmethod
    def mark_published(self, url: str, source: str, title: str) -> None: ...

    # --- post_queue ---
    @abstractmethod
    def enqueue(
        self, url: str, source: str, title: str, text: str,
        photo: bytes | None, publish_at: "datetime",
        video: bytes | None = None, extra_photos: str | None = None,
        status: str = "queued", created_at: "datetime | None" = None
    ) -> bool: ...

    @abstractmethod
    def due_items(self, now: "datetime") -> list[dict]: ...

    @abstractmethod
    def get_item(self, queue_id: int) -> dict | None: ...

    @abstractmethod
    def set_status(self, queue_id: int, status: str) -> None: ...

    @abstractmethod
    def update_text(self, queue_id: int, text: str) -> None: ...

    @abstractmethod
    def update_photos(self, queue_id: int, photos: list[bytes], video: bytes | None,
                      extra_photos: str | None) -> None: ...

    # --- news ---
    @abstractmethod
    def upsert_news(self, url: str, source: str, title: str,
                    description: str = "", published_at: str = "") -> int: ...

    @abstractmethod
    def set_news_status(self, news_id: int, status: str) -> None: ...

    @abstractmethod
    def set_video_status(self, news_id: int, video_status: str) -> None: ...

    @abstractmethod
    def get_news(self, news_id: int) -> dict | None: ...

    @abstractmethod
    def get_news_by_url(self, url: str) -> dict | None: ...

    @abstractmethod
    def video_pending(self, limit: int = 10) -> list[dict]: ...

    @abstractmethod
    def mark_video_ready(self, news_id: int, video_path: str,
                         duration: float, script: str, headline: str) -> None: ...

    @abstractmethod
    def mark_video_published(self, news_id: int, drive_url: str | None) -> None: ...

    @abstractmethod
    def mark_video_failed(self, news_id: int, error: str) -> None: ...

    @abstractmethod
    def retry_video(self, news_id: int) -> bool: ...

    @abstractmethod
    def list_videos(self, limit: int = 20) -> list[dict]: ...

    @abstractmethod
    def news_stats(self) -> dict: ...

    @abstractmethod
    def save_news_photos(self, news_id: int, photos: list[bytes]) -> None: ...

    @abstractmethod
    def load_news_photos(self, news: dict) -> list[bytes]: ...

    # --- stats ---
    @abstractmethod
    def stats(self) -> dict: ...

    @abstractmethod
    def activity_stats(self) -> dict: ...

    @abstractmethod
    def last_published(self) -> tuple[str, str] | None: ...

    @abstractmethod
    def last_messages(self) -> list[int]: ...

    @abstractmethod
    def record_messages(self, message_ids: list[int]) -> None: ...

    @abstractmethod
    def drop_messages(self, message_ids: list[int]) -> None: ...

    @abstractmethod
    def queue_preview(self, limit: int = 5) -> list[dict]: ...

    @abstractmethod
    def reschedule(self, queue_id: int, publish_at: "datetime") -> None: ...

    @abstractmethod
    def trim_queue(self, capacity: int, keep_url: str | None = None) -> None: ...

    @abstractmethod
    def exact_norm_title(self, norm_title: str) -> str | None: ...

    @abstractmethod
    def titles_containing_game(self, game_sig: str) -> list[str]: ...

    @abstractmethod
    def recent_titles(self, hours: int) -> list[str]: ...

    @abstractmethod
    def get_item_by_url(self, url: str) -> dict | None: ...

    @abstractmethod
    def backup(self) -> str | None: ...


class SQLiteStorage(StorageBackend):
    """Реализация StorageBackend для SQLite (текущая)."""

    def __init__(self, db_path: str):
        from storage import Storage
        self._storage = Storage(db_path)

    def close(self) -> None:
        self._storage.close()

    # Делегируем все методы к существующему Storage
    def is_published(self, url: str) -> bool:
        return self._storage.is_published(url)

    def mark_published(self, url: str, source: str, title: str) -> None:
        self._storage.mark_published(url, source, title)

    def enqueue(
        self, url: str, source: str, title: str, text: str,
        photo: bytes | None, publish_at: "datetime",
        video: bytes | None = None, extra_photos: str | None = None,
        status: str = "queued", created_at: "datetime | None" = None
    ) -> bool:
        return self._storage.enqueue(url, source, title, text, photo, publish_at,
                                     video, extra_photos, status, created_at)

    def due_items(self, now: "datetime") -> list[dict]:
        return self._storage.due_items(now)

    def get_item(self, queue_id: int) -> dict | None:
        return self._storage.get_item(queue_id)

    def set_status(self, queue_id: int, status: str) -> None:
        self._storage.set_status(queue_id, status)

    def update_text(self, queue_id: int, text: str) -> None:
        self._storage.update_text(queue_id, text)

    def update_photos(self, queue_id: int, photos: list[bytes], video: bytes | None,
                      extra_photos: str | None) -> None:
        self._storage.update_photos(queue_id, photos, video, extra_photos)

    def upsert_news(self, url: str, source: str, title: str,
                    description: str = "", published_at: str = "") -> int:
        return self._storage.upsert_news(url, source, title, description, published_at)

    def set_news_status(self, news_id: int, status: str) -> None:
        self._storage.set_news_status(news_id, status)

    def set_video_status(self, news_id: int, video_status: str) -> None:
        self._storage.set_video_status(news_id, video_status)

    def get_news(self, news_id: int) -> dict | None:
        return self._storage.get_news(news_id)

    def get_news_by_url(self, url: str) -> dict | None:
        return self._storage.get_news_by_url(url)

    def video_pending(self, limit: int = 10) -> list[dict]:
        return self._storage.video_pending(limit)

    def mark_video_ready(self, news_id: int, video_path: str,
                         duration: float, script: str, headline: str) -> None:
        self._storage.mark_video_ready(news_id, video_path, duration, script, headline)

    def mark_video_published(self, news_id: int, drive_url: str | None) -> None:
        self._storage.mark_video_published(news_id, drive_url)

    def mark_video_failed(self, news_id: int, error: str) -> None:
        self._storage.mark_video_failed(news_id, error)

    def retry_video(self, news_id: int) -> bool:
        return self._storage.retry_video(news_id)

    def list_videos(self, limit: int = 20) -> list[dict]:
        return self._storage.list_videos(limit)

    def news_stats(self) -> dict:
        return self._storage.news_stats()

    def save_news_photos(self, news_id: int, photos: list[bytes]) -> None:
        self._storage.save_news_photos(news_id, photos)

    def load_news_photos(self, news: dict) -> list[bytes]:
        return self._storage.load_news_photos(news)

    def stats(self) -> dict:
        return self._storage.stats()

    def activity_stats(self) -> dict:
        return self._storage.activity_stats()

    def last_published(self) -> tuple[str, str] | None:
        return self._storage.last_published()

    def last_messages(self) -> list[int]:
        return self._storage.last_messages()

    def record_messages(self, message_ids: list[int]) -> None:
        self._storage.record_messages(message_ids)

    def drop_messages(self, message_ids: list[int]) -> None:
        self._storage.drop_messages(message_ids)

    def queue_preview(self, limit: int = 5) -> list[dict]:
        return self._storage.queue_preview(limit)

    def reschedule(self, queue_id: int, publish_at: "datetime") -> None:
        self._storage.reschedule(queue_id, publish_at)

    def trim_queue(self, capacity: int, keep_url: str | None = None) -> None:
        self._storage.trim_queue(capacity, keep_url)

    def exact_norm_title(self, norm_title: str) -> str | None:
        return self._storage.exact_norm_title(norm_title)

    def titles_containing_game(self, game_sig: str) -> list[str]:
        return self._storage.titles_containing_game(game_sig)

    def recent_titles(self, hours: int) -> list[str]:
        return self._storage.recent_titles(hours)

    def get_item_by_url(self, url: str) -> dict | None:
        return self._storage.get_item_by_url(url)

    def backup(self) -> str | None:
        return self._storage.backup()

    def close(self) -> None:
        self._storage.close()


def create_storage(db_path: str, backend: str = "sqlite") -> StorageBackend:
    """Фабрика для создания хранилища.

    Args:
        db_path: путь к файлу БД (SQLite) или connection string (Postgres)
        backend: "sqlite" | "postgres" (пока только sqlite реализован)

    Returns:
        Экземпляр StorageBackend
    """
    if backend == "sqlite":
        return SQLiteStorage(db_path)
    elif backend == "postgres":
        raise NotImplementedError("PostgreSQL backend пока не реализован")
    else:
        raise ValueError(f"Неизвестный backend: {backend}")


__all__ = [
    "Migration",
    "MigrationManager",
    "StorageBackend",
    "SQLiteStorage",
    "create_storage",
]