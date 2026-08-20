# Архитектура (ARCHITECTURE.md)

Общая схема системы, потоки данных и взаимодействие модулей.

---

## Общая схема

```
┌─────────────────────────────────────────────────────────────────┐
│                        TELEGRAM BOT                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │  COMMANDS   │  │  SCHEDULER  │  │      VIDEO WORKER       │  │
│  │  (/start,    │  │  (poll,     │  │  (process_pending,      │  │
│  │  /publish,   │  │   publish,  │  │   generate_one)         │  │
│  │  /stats...)  │  │   review)   │  │                         │  │
│  └──────┬──────┘  └──────┬──────┘  └───────────┬─────────────┘  │
└─────────┼────────────────┼─────────────────────┼────────────────┘
          │                │                     │
          ▼                ▼                     ▼
┌─────────────────────────────────────────────────────────────────┐
│                      NEWS PIPELINE                               │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │   PARSERS   │  │   ARTICLE   │  │        OLLAMA LLM       │  │
│  │ (RSS fetch, │──▶│  FETCHING   │──▶│ (rewrite, proofread,    │  │
│  │  filters)   │   │ (text,img)  │   │  extract_game, script)  │  │
│  └─────────────┘  └──────┬──────┘  └───────────┬──────────────┘  │
│                          │                     │                 │
│                          ▼                     │                 │
│                   ┌──────────────┐             │                 │
│                   │   IMAGES     │             │                 │
│                   │ (Steam,      │             │                 │
│                   │  Pixabay,    │             │                 │
│                   │  Wiki, SGDB) │             │                 │
│                   └──────┬───────┘             │                 │
│                          │                     │                 │
│                          ▼                     ▼                 │
│                   ┌──────────────┐    ┌──────────────────┐      │
│                   │  STORAGE     │◀─▶│  POST QUEUE      │      │
│                   │  (SQLite)    │   │ (schedule,       │      │
│                   └──────────────┘   │  approve,       │      │
│                                      │  publish)        │      │
│                                      └────────┬─────────┘      │
└───────────────────────────────────────────────┼────────────────┘
                                                │
                                                ▼
                         ┌─────────────────────────────────────────┐
                         │           VIDEO PIPELINE                 │
                         │  ┌─────────┐  ┌─────┐ ┌────────┐       │
                         │  │ OLLAMA  │─▶│ TTS │▶│WHISPER │       │
                         │  │ (script)│  │(Piper)│(subs)  │       │
                         │  └─────────┘  └──┬──┘ └────┬──┘       │
                         │                  ▼        ▼         │
                         │           ┌────────────────┐        │
                         │           │  FFMPEG        │        │
                         │           │ (photos+subs)  │        │
                         │           └──────┬─────────┘        │
                         │                  ▼                │
                         │         ┌──────────────┐         │
                         │         │  QC (ffprobe)│         │
                         │         └──────┬───────┘         │
                         │                ▼                 │
                         │    ┌───────────────────┐        │
                         │    │ Google Drive (opt)│        │
                         │    └───────────────────┘        │
                         └─────────────────────────────────┘
```

---

## Основные модули

### 1. Parsers (`parsers/`)
- **RSSBaseParser**: базовый класс для RSS
- **StopGame, Igromania, DTF, 3DNews, VGTimes**: специфические парсеры
- `fetch_items()`: возвращает список `NewsItem` (url, title, description, published_at)

### 2. Article (`article.py`)
- `fetch_article()`: скачивает HTML, извлекает текст, og:image, контент-картинки, YouTube/видео ссылки
- **Селекторы** на сайт: `ARTICLE_SELECTORS`
- **Фолбэк**: readability-подобный алгоритм (`_fallback_extract_text`) если селектор не сработал
- Возвращает `ParseResult` с `success`, `reason`, `details`, `text_length`

### 3. Images (`images.py`)
Цепочка фолбэков для чистых фото:
1. **Pixabay** (API key) — фото по названию игры
2. **Steam** (store API + CDN) — официальные обложки/герои
3. **Playground.ru** — og:image со страницы игры
4. **Wikipedia** — pageimages API
5. **SteamGridDB** — официальные гриды (600x900)
- Валидация: мин. ширина, не SVG, не плейсхолдеры (<30KB), соотношение сторон

### 4. LLM (`llm_ollama.py`)
**Архитектура провайдеров:**
- `LLMProvider` (ABC) → `OllamaProvider`, `OpenAICompatibleProvider`
- `LLMClient`: высокоуровневый клиент с circuit breaker и фолбэком

**Circuit Breaker (Ollama):**
- CLOSED → (3 failures) → OPEN → (5 min) → HALF_OPEN → (1 success) → CLOSED
- При OPEN запросы идут сразу к fallback

**Fallback Provider:**
- Любой OpenAI-совместимый API (OpenAI, OpenRouter, YandexGPT, vLLM)
- Автоматическое переключение с уведомлением владельца

**Методы:**
- `rewrite()`: пост для Telegram
- `extract_game()`: название игры из заголовка
- `is_same_news()`: дедуп по заголовкам
- `proofread()`: вычитка орфографии
- `generate_json()` / `video_script()`: JSON-режим

---

### 5. Pipeline (`pipeline.py`)
**`NewsPipeline`** — основной оркестратор:
- `process_all()`: сбор со всех источников → очередь
- `_build_post()`: единая цепочка (article → game → dup → LLM → QC → photos)
- `publish_due()`: публикация готовых постов (с quiet hours, approve)
- `run_review()`: ежедневное ревью по расписанию
- `_record_parse_result()`: health-check парсеров
- `_check_parser_health()`: алерт если success rate < 50%

---

### 6. Video Pipeline (`video_pipeline.py`)
**`VideoPipeline`** — независимый конвейер:
- Независим от Telegram: сбой видео не ломает пост
- Очередь: `video_status` = none/pending/video_processing/video_ready/video_published/failed
- Команды: `/generate <id>`, `/retry <id>`, `/videos`, `/status`

**Этапы (`process_news`):**
1. **Script** (Ollama JSON) — 120с timeout
2. **TTS** (Piper → espeak-ng fallback) — 180с
3. **Whisper** (whisper-timestamped) — 300с
4. **FFmpeg** (photos + transitions + subs) — 600с
5. **Google Drive** (opt) — 120с

**Ресурсный контроль:**
- `_check_resources()`: CPU < 85%, RAM < 85%, free > 1GB
- Адаптивные таймауты на этап
- Метрики: `get_metrics()` → avg/max duration, success_rate

---

### 7. Video Engine (`video/`)
- **`templates.py`**: FFmpeg filter_complex
  - Фото: blur background + center crop + crossfade/fade transitions
  - Фон: background.mp4 или lavfi color
  - Субтитры: ASS (заголовок сверху + фразы снизу)
- **`generator.py`**: `VideoGenerator`
  - Автодетект энкодера: libx264 → libopenh264 → hwaccel
  - QC: ffprobe (duration, streams, resolution, codecs)

### 8. TTS (`tts/`)
- **`piper.py`**: CLI wrapper для Piper
- **`generator.py`**: `TTSEngine` с автофолбэком piper → espeak-ng

### 9. Subtitles (`subtitles/`)
- `whisper.py`: `WhisperTranscriber` на whisper-timestamped
- Word-level timestamps → группировка в фразы (≤5 слов, ≤2.6с)
- Output: ASS (заголовок + фразы) + SRT

### 10. Storage (`storage.py`, `migrations.py`)
- **SQLite** с WAL + индексы
- Таблицы: `published`, `post_queue`, `news`, `post_messages`, `schema_version`
- **MigrationManager**: загрузка SQL из `migrations/`, таблица `schema_version`
- `StorageBackend` (ABC) + `SQLiteStorage` — абстракция для Postgres

### 11. Commands (`commands.py`)
- Панель кнопок + текстовые команды
- `/publish_now`, `/stats`, `/next`, `/videos`, `/generate`, `/retry`, `/status`, `/settings`, `/health`, `/transfer_ownership`
- Callback-кнопки: publish, skip, delay, delete, review actions

### 11. Owner (`owner.py`)
- TTL setup код (10 мин, одноразовый, JSON файл)
- `verify_setup_code()`, `generate_setup_code()`, `transfer_ownership()`
- `log_auth_attempt()` — аудит всех попыток

### 12. Notifier (`notifier.py`)
- Уведомления владельцу: ошибки, застрявшие посты, ревью, LLM fallback, missed slots
- Троттлинг: не чаще раза в 10 мин по категории

### 13. Config (`config.py`)
- `load_config(dry_run)` → `Config` dataclass
- Валидация + `_validate_credentials()` при старте
- `.env.example` со всеми переменными

### 14. Migrations (`migrations.py`)
- `MigrationManager`: загрузка `.sql` из `migrations/`, таблица `schema_version`
- Файлы: `001_initial_schema.sql`, `002_video_pipeline_fields.sql`
- `StorageBackend` (ABC) + `SQLiteStorage` — для будущего Postgres

---

## Потоки данных

### 1. Сбор и публикация новости
```
RSS → Parser → Article → Images → Ollama(rewrite) → QC → Queue → Publish
                                    ↓
                              Images → Ollama(video_script)
                                    ↓
                              Video Pipeline (TTS→Whisper→FFmpeg) → QC → Drive
```

### 2. Видео-очередь (независимая)
```
News.published → video_pending() → process_news() → mark_video_ready/published
                                    ↓
                              /generate <id> (ручной запуск)
                              /retry <id> (после ошибки)
```

### 3. Ревью постов
```
Queue.awaiting → /review → Owner approves/rejects/edits → publish_due()
```

### 4. LLM Fallback
```
Ollama (CLOSED) → 3 failures → OPEN (5 min) → HALF_OPEN → Fallback
                                    ↓
                              Notify owner
                              Auto-switch back when recovered
```

---

## База данных (SQLite)

### Таблицы
| Таблица | Назначение |
|---------|------------|
| `published` | Опубликованные URL (дедуп) |
| `post_queue` | Очередь постов на публикацию |
| `news` | Жизненный цикл новости (post + video) |
| `post_messages` | message_id опубликованных постов |
| `schema_version` | Версии миграций |
| `video_metrics` | Метрики видео-пайплайна |

### Статусы `news.status`
- `new` → `processing` → `telegram_ready` → `telegram_published` → `completed`
- `rejected` (нет фото/дубль) / `failed` (ошибка)

### Статусы `news.video_status`
- `none` → `pending` → `video_processing` → `video_ready` → `video_published`
- `failed` (ошибка генерации)

---

## Запуск и мониторинг

### Systemd
```ini
[Unit]
Description=Game news Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/path/to/bot
ExecStart=/path/to/.venv/bin/python -u bot.py
Environment=AIOHTTP_CLIENT_FORCE_IPV4=1
Restart=always
RestartSec=10
WatchdogSec=60  # systemd watchdog

[Install]
WantedBy=default.target
```

### Watchdog
- Бот обновляет `watchdog.tmp` каждые 30 сек (`_update_watchdog()`)
- systemd `WatchdogSec=60` или внешний скрипт проверяет mtime файла

### Мониторинг
- `/health` — Ollama, БД, ошибки за сутки
- `/status` — статусы новостей и видео
- `/metrics` (видео) — avg/max duration, success_rate
- Логи: `logs/bot.log` (rotating 2MB × 5) + stdout

---

## Безопасность

- **Owner**: TTL setup код (10 мин, одноразовый), `/transfer_ownership`
- **Auth**: только владелец имеет доступ к командам
- **Secrets**: только в `.env` (gitignored), credentials.json для Drive
- **Network**: `AIOHTTP_CLIENT_FORCE_IPV4=1` (избегаем IPv6 зависаний)
- **Resources**: CPU/RAM лимиты перед рендером видео