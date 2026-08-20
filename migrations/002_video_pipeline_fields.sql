-- UP
-- Добавление полей для видео-пайплайна и метрик

-- Дополнительные поля в news для video pipeline
ALTER TABLE news ADD COLUMN video_timeout_script INTEGER DEFAULT 120;
ALTER TABLE news ADD COLUMN video_timeout_tts INTEGER DEFAULT 180;
ALTER TABLE news ADD COLUMN video_timeout_whisper INTEGER DEFAULT 300;
ALTER TABLE news ADD COLUMN video_timeout_ffmpeg INTEGER DEFAULT 600;
ALTER TABLE news ADD COLUMN video_timeout_drive INTEGER DEFAULT 120;

-- Таблица метрик видео-пайплайна
CREATE TABLE IF NOT EXISTS video_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    news_id INTEGER NOT NULL,
    stage TEXT NOT NULL,           -- script, tts, whisper, ffmpeg, drive
    duration REAL NOT NULL,        -- секунды
    success INTEGER NOT NULL,      -- 1 = успех, 0 = ошибка
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS video_metrics_news_id_idx ON video_metrics(news_id);
CREATE INDEX IF NOT EXISTS video_metrics_stage_idx ON video_metrics(stage);

-- DOWN
DROP INDEX IF EXISTS video_metrics_stage_idx;
DROP INDEX IF EXISTS video_metrics_news_id_idx;
DROP TABLE IF EXISTS video_metrics;