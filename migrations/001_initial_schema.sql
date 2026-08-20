-- UP
-- Инициальная схема БД (соответствует текущему storage.py)

-- Таблица опубликованных URL (дедупликация)
CREATE TABLE IF NOT EXISTS published (
    url         TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    title       TEXT NOT NULL,
    published_at TEXT NOT NULL,
    norm_title   TEXT
);

CREATE INDEX IF NOT EXISTS published_norm_title_idx ON published(norm_title);

-- Очередь постов на публикацию
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
    extra_photos TEXT,
    created_at  TEXT,
    norm_title   TEXT
);

CREATE INDEX IF NOT EXISTS post_queue_norm_title_idx ON post_queue(norm_title);
CREATE UNIQUE INDEX IF NOT EXISTS post_queue_url_unique ON post_queue(url);

-- Сообщения в канале (для удаления)
CREATE TABLE IF NOT EXISTS post_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  INTEGER NOT NULL,
    published_at TEXT NOT NULL
);

-- Жизненный цикл новости: Telegram-пост + AI-видео
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
);

-- Миграции колонок news (безопасно, если БД уже создана)
-- description, published_at, created_at, status, video_status, score, category, reason
-- video_script, video_headline, video_path, video_duration, video_created_at, video_published_at, video_error, google_drive_url, photos

CREATE INDEX IF NOT EXISTS news_status_idx ON news(status);
CREATE INDEX IF NOT EXISTS news_video_status_idx ON news(video_status);
CREATE UNIQUE INDEX IF NOT EXISTS news_url_idx ON news(url);

-- Таблица версий схемы (управляется MigrationManager)
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- DOWN
DROP TABLE IF EXISTS schema_version;
DROP INDEX IF EXISTS news_url_idx;
DROP INDEX IF EXISTS news_video_status_idx;
DROP INDEX IF EXISTS news_status_idx;
DROP TABLE IF EXISTS news;
DROP TABLE IF EXISTS post_messages;
DROP INDEX IF EXISTS post_queue_url_unique;
DROP INDEX IF EXISTS post_queue_norm_title_idx;
DROP TABLE IF EXISTS post_queue;
DROP INDEX IF EXISTS published_norm_title_idx;
DROP TABLE IF EXISTS published;