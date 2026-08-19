import base64
import glob
import json
import logging
import os
import re
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

log = logging.getLogger("storage")


def normalize_title(title: str) -> str:
    """Нормализованный заголовок для поиска дубликатов (только буквы/цифры)."""
    return re.sub(r"[^a-zа-яё0-9]+", "", title.lower())


class Storage:
    """Хранилище уже опубликованных ссылок (дедупликация новостей)."""

    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS published (
                url         TEXT PRIMARY KEY,
                source      TEXT NOT NULL,
                title       TEXT NOT NULL,
                published_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS post_queue (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                url         TEXT NOT NULL,
                source      TEXT NOT NULL,
                title       TEXT NOT NULL,
                text        TEXT NOT NULL,
                photo       BLOB,
                video       BLOB,
                publish_at  TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'queued',
                extra_photos TEXT
            )
            """
        )
        # Миграция для БД, созданной до появления видео.
        try:
            self._conn.execute("ALTER TABLE post_queue ADD COLUMN video BLOB")
        except sqlite3.OperationalError:
            pass
        try:
            self._conn.execute("ALTER TABLE post_queue ADD COLUMN status TEXT NOT NULL DEFAULT 'queued'")
        except sqlite3.OperationalError:
            pass
        try:
            self._conn.execute("ALTER TABLE post_queue ADD COLUMN extra_photos TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            self._conn.execute("ALTER TABLE post_queue ADD COLUMN created_at TEXT")
        except sqlite3.OperationalError:
            pass
        # Нормализованный заголовок для быстрого поиска дубликатов (индекс вместо
        # линейного перебора all_titles() в Python). Обратная миграция: заполняем
        # norm_title для строк, созданных до появления колонки.
        for table in ("published", "post_queue"):
            try:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN norm_title TEXT")
            except sqlite3.OperationalError:
                pass
        for url, title in self._conn.execute(
            "SELECT url, title FROM published WHERE norm_title IS NULL"
        ).fetchall():
            self._conn.execute(
                "UPDATE published SET norm_title = ? WHERE url = ?",
                (normalize_title(title), url),
            )
        for qid, title in self._conn.execute(
            "SELECT id, title FROM post_queue WHERE norm_title IS NULL"
        ).fetchall():
            self._conn.execute(
                "UPDATE post_queue SET norm_title = ? WHERE id = ?",
                (normalize_title(title), qid),
            )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS published_norm_title_idx ON published(norm_title)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS post_queue_norm_title_idx ON post_queue(norm_title)"
        )
        # URL должен быть уникален и в очереди: несколько одновременно
        # обрабатываемых RSS-элементов иначе могут пройти предварительную проверку.
        self._conn.execute(
            "DELETE FROM post_queue WHERE id NOT IN "
            "(SELECT MIN(id) FROM post_queue GROUP BY url)"
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS post_queue_url_unique ON post_queue(url)"
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS post_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id  INTEGER NOT NULL,
                published_at TEXT NOT NULL
            )
            """
        )
        # Единая таблица жизненного цикла новости: Telegram-пост + AI-видео.
        # status — статус Telegram-поста, video_status — статус видео.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS news (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                url           TEXT NOT NULL,
                source        TEXT NOT NULL,
                title         TEXT NOT NULL,
                description   TEXT,
                published_at  TEXT,
                created_at    TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'new',
                video_status  TEXT NOT NULL DEFAULT 'none',
                score         INTEGER,
                category      TEXT,
                reason        TEXT,
                video_script  TEXT,
                video_headline TEXT,
                video_path    TEXT,
                video_duration REAL,
                video_created_at TEXT,
                video_published_at TEXT,
                video_error   TEXT,
                google_drive_url TEXT,
                photos        TEXT
            )
            """
        )
        # Миграции колонок news (безопасно, если БД уже создана).
        _news_columns = {
            "description": "TEXT",
            "published_at": "TEXT",
            "created_at": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'new'",
            "video_status": "TEXT NOT NULL DEFAULT 'none'",
            "score": "INTEGER",
            "category": "TEXT",
            "reason": "TEXT",
            "video_script": "TEXT",
            "video_headline": "TEXT",
            "video_path": "TEXT",
            "video_duration": "REAL",
            "video_created_at": "TEXT",
            "video_published_at": "TEXT",
            "video_error": "TEXT",
            "google_drive_url": "TEXT",
            "photos": "TEXT",
        }
        existing_news_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(news)").fetchall()
        }
        for col, ddl in _news_columns.items():
            if col not in existing_news_columns:
                self._conn.execute(f"ALTER TABLE news ADD COLUMN {col} {ddl}")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS news_status_idx ON news(status)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS news_video_status_idx ON news(video_status)"
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS news_url_idx ON news(url)"
        )
        self._conn.commit()

    def is_published(self, url: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM published WHERE url = ?", (url,)
        ).fetchone()
        return row is not None

    def is_queued(self, url: str) -> bool:
        """URL уже стоит в очереди (включая ожидающие утверждения)."""
        row = self._conn.execute(
            "SELECT 1 FROM post_queue WHERE url = ?", (url,)
        ).fetchone()
        return row is not None

    def queue_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM post_queue").fetchone()[0]

    def drop_oldest(self) -> None:
        """Удаляет самый ранний пост из очереди (вытеснение устаревших)."""
        self._conn.execute(
            "DELETE FROM post_queue WHERE id = "
            "(SELECT id FROM post_queue ORDER BY "
            "CASE WHEN status = 'queued' THEN 0 ELSE 1 END, publish_at, id LIMIT 1)"
        )
        self._conn.commit()

    def trim_queue(self, capacity: int, keep_url: str | None = None) -> None:
        """Удаляет дальние записи, сохраняя ближайшие и новый URL."""
        while self.queue_count() > capacity:
            params: tuple[str, ...] = () if keep_url is None else (keep_url,)
            condition = "" if keep_url is None else "WHERE url <> ?"
            row = self._conn.execute(
                "SELECT id FROM post_queue "
                f"{condition} ORDER BY publish_at DESC, id DESC LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                self.drop_oldest()
                continue
            self._conn.execute("DELETE FROM post_queue WHERE id = ?", (row[0],))
            self._conn.commit()

    def mark_published(self, url: str, source: str, title: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO published (url, source, title, norm_title, published_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (url, source, title, normalize_title(title), datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def enqueue(self, url: str, source: str, title: str, text: str,
                photo: bytes | None, publish_at: datetime,
                video: bytes | None = None, extra_photos: str | None = None,
                status: str = "queued", created_at: datetime | None = None) -> bool:
        """Ставит готовый пост в очередь публикации."""
        self._conn.execute(
            "INSERT OR IGNORE INTO post_queue "
            "(url, source, title, text, photo, video, publish_at, status, extra_photos, created_at, norm_title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (url, source, title, text, photo, video, publish_at.isoformat(), status, extra_photos,
             (created_at or datetime.now(timezone.utc)).isoformat(),
             normalize_title(title)),
        )
        inserted = self._conn.execute("SELECT changes()").fetchone()[0] == 1
        self._conn.commit()
        return inserted

    @staticmethod
    def _decode_photos(photo: bytes | None, extra: str | None) -> list[bytes]:
        photos = [photo] if photo else []
        if extra:
            try:
                for b64 in json.loads(extra):
                    photos.append(base64.b64decode(b64))
            except (ValueError, TypeError):
                log.warning("Не удалось разобрать extra_photos: %r", extra)
        return photos

    def _photos(self, row: tuple) -> list[bytes]:
        return self._decode_photos(row[4], row[7])

    def get_item(self, queue_id: int) -> dict | None:
        """Пост из очереди по id (для кнопок утверждения)."""
        row = self._conn.execute(
            "SELECT id, url, source, title, text, photo, video, extra_photos, publish_at, status, created_at "
            "FROM post_queue WHERE id = ?",
            (queue_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0], "url": row[1], "source": row[2], "title": row[3],
            "text": row[4], "photos": self._decode_photos(row[5], row[7]),
            "video": row[6], "publish_at": row[8], "status": row[9],
            "created_at": row[10],
        }

    def set_status(self, queue_id: int, status: str) -> None:
        self._conn.execute(
            "UPDATE post_queue SET status = ? WHERE id = ?", (status, queue_id)
        )
        self._conn.commit()

    def update_text(self, queue_id: int, text: str) -> None:
        self._conn.execute(
            "UPDATE post_queue SET text = ? WHERE id = ?", (text, queue_id)
        )
        self._conn.commit()

    def update_photos(self, queue_id: int, photos: list[bytes], video: bytes | None,
                      extra_photos: str | None) -> None:
        photo = photos[0] if photos else None
        self._conn.execute(
            "UPDATE post_queue SET photo = ?, video = ?, extra_photos = ? WHERE id = ?",
            (photo, video, extra_photos, queue_id),
        )
        self._conn.commit()

    def get_items_by_status(self, status: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, url, source, title, text, photo, video, extra_photos, publish_at, status, created_at "
            "FROM post_queue WHERE status = ? ORDER BY publish_at",
            (status,),
        ).fetchall()
        return [
            {
                "id": r[0], "url": r[1], "source": r[2], "title": r[3],
                "text": r[4], "photos": self._decode_photos(r[5], r[7]),
                "video": r[6], "publish_at": r[8], "status": r[9],
                "created_at": r[10],
            }
            for r in rows
        ]

    def get_item_by_url(self, url: str) -> dict | None:
        """Пост из очереди по url (для фото к видео-фону)."""
        row = self._conn.execute(
            "SELECT id, url, source, title, text, photo, video, extra_photos, publish_at, status, created_at "
            "FROM post_queue WHERE url = ? ORDER BY publish_at DESC LIMIT 1",
            (url,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0], "url": row[1], "source": row[2], "title": row[3],
            "text": row[4], "photos": self._decode_photos(row[5], row[7]),
            "video": row[6], "publish_at": row[8], "status": row[9],
            "created_at": row[10],
        }

    def due_items(self, now: datetime) -> list[dict]:
        """Возвращает посты, которые пора публиковать (publish_at <= now)."""
        rows = self._conn.execute(
            "SELECT id, url, source, title, text, photo, video, extra_photos, publish_at, status, created_at "
            "FROM post_queue WHERE publish_at <= ? ORDER BY publish_at",
            (now.isoformat(),),
        ).fetchall()
        return [
            {
                "id": r[0], "url": r[1], "source": r[2], "title": r[3],
                "text": r[4], "photos": self._decode_photos(r[5], r[7]),
                "video": r[6], "publish_at": r[8], "status": r[9],
                "created_at": r[10],
            }
            for r in rows
        ]

    def dequeue(self, queue_id: int) -> None:
        self._conn.execute("DELETE FROM post_queue WHERE id = ?", (queue_id,))
        self._conn.commit()

    def reschedule(self, queue_id: int, publish_at: datetime) -> None:
        """Переносит пост на новое время публикации (тихие часы)."""
        self._conn.execute(
            "UPDATE post_queue SET publish_at = ? WHERE id = ?",
            (publish_at.isoformat(), queue_id),
        )
        self._conn.commit()

    def latest_publish_time(self, max_ahead_hours: float | None = None) -> datetime | None:
        """Самое позднее время публикации в заданном горизонте."""
        latest: datetime | None = None
        rows = self._conn.execute(
            "SELECT publish_at FROM post_queue "
            "UNION SELECT published_at FROM published"
        ).fetchall()
        horizon = None
        if max_ahead_hours is not None:
            horizon = datetime.now(timezone.utc) + timedelta(hours=max_ahead_hours)
        for (iso,) in rows:
            try:
                parsed = datetime.fromisoformat(iso)
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if horizon is not None and parsed > horizon:
                continue
            if latest is None or parsed > latest:
                latest = parsed
        return latest

    def all_titles(self) -> list[str]:
        """Все заголовки из опубликованного и очереди — для поиска дубликатов."""
        rows = self._conn.execute(
            "SELECT title FROM published UNION SELECT title FROM post_queue"
        ).fetchall()
        return [r[0] for r in rows]

    def exact_norm_title(self, norm_title: str) -> str | None:
        """Возвращает оригинальный заголовок, если такой же нормализованный
        уже есть в опубликованном или в очереди (поиск по индексу norm_title)."""
        row = self._conn.execute(
            "SELECT title FROM published WHERE norm_title = ? "
            "UNION SELECT title FROM post_queue WHERE norm_title = ? LIMIT 1",
            (norm_title, norm_title),
        ).fetchone()
        return row[0] if row else None

    def titles_containing_game(self, game_sig: str) -> list[str]:
        """Заголовки, содержащие нормализованное имя игры (для LLM-проверки
        дубликатов по одному событию)."""
        like = f"%{game_sig}%"
        rows = self._conn.execute(
            "SELECT title FROM published WHERE norm_title LIKE ? "
            "UNION SELECT title FROM post_queue WHERE norm_title LIKE ?",
            (like, like),
        ).fetchall()
        return [r[0] for r in rows]

    def recent_titles(self, hours: float) -> list[str]:
        """Заголовки за последние N часов (опубликованные + вся очередь)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self._conn.execute(
            "SELECT title FROM published WHERE published_at >= ?", (cutoff,)
        ).fetchall()
        qrows = self._conn.execute(
            "SELECT title, created_at FROM post_queue"
        ).fetchall()
        recent_queue = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        for title, created_at in qrows:
            if not created_at:
                recent_queue.append(title)
                continue
            try:
                created = datetime.fromisoformat(created_at)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if created >= cutoff:
                recent_queue.append(title)
        return [r[0] for r in rows] + recent_queue

    def record_messages(self, message_ids: list[int]) -> None:
        """Сохраняет message_id опубликованных сообщений (для удаления)."""
        now = datetime.now(timezone.utc).isoformat()
        for mid in message_ids:
            self._conn.execute(
                "INSERT INTO post_messages (message_id, published_at) VALUES (?, ?)",
                (mid, now),
            )
        self._conn.commit()

    def last_messages(self) -> list[int]:
        """message_id последней публикации (группы последнего поста)."""
        rows = self._conn.execute(
            "SELECT message_id FROM post_messages "
            "WHERE published_at = (SELECT MAX(published_at) FROM post_messages)"
        ).fetchall()
        return [r[0] for r in rows]

    def drop_messages(self, message_ids: list[int]) -> None:
        if not message_ids:
            return
        self._conn.execute(
            f"DELETE FROM post_messages WHERE message_id IN ({','.join('?' * len(message_ids))})",
            message_ids,
        )
        self._conn.commit()

    def last_published(self) -> tuple[str, str] | None:
        """(заголовок, время) последнего опубликованного поста."""
        row = self._conn.execute(
            "SELECT title, published_at FROM published ORDER BY published_at DESC LIMIT 1"
        ).fetchone()
        return (row[0], row[1]) if row else None

    def stats(self) -> dict:
        """Краткая статистика для команды /stats: сколько опубликовано,
        что в очереди и когда следующий пост."""
        published = self._conn.execute(
            "SELECT COUNT(*), MAX(published_at) FROM published"
        ).fetchone()
        queued = self._conn.execute(
            "SELECT COUNT(*), MIN(publish_at) FROM post_queue"
        ).fetchone()
        return {
            "published": published[0] or 0,
            "last_published_at": published[1],
            "queued": queued[0] or 0,
            "next_publish_at": queued[1],
        }

    def activity_stats(self) -> dict:
        now = datetime.now(timezone.utc)
        day = (now - timedelta(days=1)).isoformat()
        week = (now - timedelta(days=7)).isoformat()
        day_count = self._conn.execute("SELECT COUNT(*) FROM published WHERE published_at >= ?", (day,)).fetchone()[0]
        week_count = self._conn.execute("SELECT COUNT(*) FROM published WHERE published_at >= ?", (week,)).fetchone()[0]
        sources = self._conn.execute(
            "SELECT source, COUNT(*) FROM published WHERE published_at >= ? GROUP BY source ORDER BY COUNT(*) DESC LIMIT 3",
            (week,),
        ).fetchall()
        return {"day": day_count, "week": week_count, "sources": sources}

    def queue_preview(self, limit: int = 5) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, title, publish_at, status FROM post_queue ORDER BY publish_at LIMIT ?",
            (limit,),
        ).fetchall()
        return [{"id": r[0], "title": r[1], "publish_at": r[2], "status": r[3]} for r in rows]

    # ------------------------------------------------------------------
    # Жизненный цикл новости (Telegram-пост + видео)
    # status: new/processing/telegram_ready/telegram_published/
    #         completed/rejected/failed
    # video_status: none/pending/video_processing/video_ready/
    #               video_published/failed
    # ------------------------------------------------------------------

    def upsert_news(self, url: str, source: str, title: str,
                    description: str = "", published_at: str = "") -> int:
        """Создаёт запись новости или возвращает id существующей."""
        self._conn.execute(
            """INSERT OR IGNORE INTO news
               (url, source, title, description, published_at, created_at, status)
               VALUES (?, ?, ?, ?, ?, ?, 'new')""",
            (url, source, title, description, published_at,
             datetime.now(timezone.utc).isoformat()),
        )
        row = self._conn.execute(
            "SELECT id FROM news WHERE url = ? LIMIT 1", (url,)
        ).fetchone()
        self._conn.commit()
        return row[0] if row else 0

    def news_exists(self, url: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM news WHERE url = ? LIMIT 1", (url,)
        ).fetchone()
        return row is not None

    def save_news_photos(self, news_id: int, photos: list[bytes]) -> None:
        """Сохраняет фото новости (base64 JSON) для видео-фона."""
        if not photos:
            return
        import base64 as _b64

        payload = json.dumps([_b64.b64encode(p).decode() for p in photos])
        self._conn.execute(
            "UPDATE news SET photos = ? WHERE id = ?", (payload, news_id)
        )
        self._conn.commit()

    def load_news_photos(self, news: dict) -> list[bytes]:
        """Возвращает фото новости из колонки photos (base64 JSON)."""
        raw = news.get("photos") or ""
        if not raw:
            return []
        import base64 as _b64

        photos: list[bytes] = []
        try:
            for b64 in json.loads(raw):
                try:
                    photos.append(_b64.b64decode(b64))
                except Exception:
                    continue
        except (ValueError, TypeError):
            return []
        return photos

    def set_news_status(self, news_id: int, status: str) -> None:
        self._conn.execute(
            "UPDATE news SET status = ? WHERE id = ?", (status, news_id)
        )
        self._conn.commit()

    def get_news(self, news_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM news WHERE id = ?", (news_id,)).fetchone()
        if row is None:
            return None
        return dict(row)

    def get_news_by_url(self, url: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM news WHERE url = ? LIMIT 1", (url,)).fetchone()
        return dict(row) if row else None

    def news_by_status(self, status: str, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM news WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_news(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM news ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def news_stats(self) -> dict:
        """Сводка по статусам (для /status)."""
        counts = {
            row[0]: row[1]
            for row in self._conn.execute(
                "SELECT status, COUNT(*) FROM news GROUP BY status"
            ).fetchall()
        }
        video_counts = {
            row[0]: row[1]
            for row in self._conn.execute(
                "SELECT video_status, COUNT(*) FROM news GROUP BY video_status"
            ).fetchall()
        }
        return {"status": counts, "video_status": video_counts}

    # --- Видео-очередь ---

    def video_pending(self, limit: int = 10) -> list[dict]:
        """Новости, которым пора генерировать видео (независимая очередь).

        Берём новости со status telegram_published (или completed, если видео
        ещё не обработано) и video_status в none/pending/failed — по одному
        pipeline на новость.
        """
        rows = self._conn.execute(
            """SELECT * FROM news
               WHERE video_status IN ('none', 'pending', 'failed')
                 AND status IN ('telegram_published', 'completed')
               ORDER BY created_at ASC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_video_status(self, news_id: int, video_status: str) -> None:
        self._conn.execute(
            "UPDATE news SET video_status = ? WHERE id = ?",
            (video_status, news_id),
        )
        self._conn.commit()

    def mark_video_ready(self, news_id: int, video_path: str,
                         duration: float, script: str, headline: str) -> None:
        self._conn.execute(
            """UPDATE news
               SET video_status = 'video_ready', video_path = ?,
                   video_duration = ?, video_script = ?, video_headline = ?,
                   video_created_at = ?, video_error = NULL
               WHERE id = ?""",
            (video_path, duration, script, headline,
             datetime.now(timezone.utc).isoformat(), news_id),
        )
        self._conn.commit()

    def mark_video_published(self, news_id: int,
                             google_drive_url: str | None = None) -> None:
        self._conn.execute(
            """UPDATE news
               SET video_status = 'video_published',
                   video_published_at = ?, google_drive_url = ?
               WHERE id = ?""",
            (datetime.now(timezone.utc).isoformat(), google_drive_url, news_id),
        )
        self._conn.commit()

    def mark_video_failed(self, news_id: int, error: str) -> None:
        self._conn.execute(
            """UPDATE news SET video_status = 'failed', video_error = ?
               WHERE id = ?""",
            (str(error)[:1000], news_id),
        )
        self._conn.commit()

    def retry_video(self, news_id: int) -> bool:
        """Возвращает новость в видео-очередь (повторная генерация видео)."""
        row = self.get_news(news_id)
        if row is None:
            return False
        self._conn.execute(
            """UPDATE news
               SET video_status = 'pending', video_error = NULL,
                   video_path = NULL, google_drive_url = NULL
               WHERE id = ?""",
            (news_id,),
        )
        self._conn.commit()
        return True

    def list_videos(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM news WHERE video_status != 'none' "
            "ORDER BY video_created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def backup(self) -> str | None:
        """Копия БД в data/backups/, храним 7 последних копий."""
        self._conn.execute("PRAGMA wal_checkpoint(FULL)")
        backup_dir = os.path.join(
            os.path.dirname(os.path.abspath(self._db_path)), "backups"
        )
        os.makedirs(backup_dir, exist_ok=True)
        dest = os.path.join(
            backup_dir, f"published-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
        )
        try:
            shutil.copy2(self._db_path, dest)
        except OSError as exc:
            log.warning("Не удалось создать бэкап БД: %s", exc)
            return None
        for old in sorted(glob.glob(os.path.join(backup_dir, "published-*.db")))[:-7]:
            try:
                os.unlink(old)
            except OSError:
                pass
        return dest

    def close(self) -> None:
        self._conn.close()
