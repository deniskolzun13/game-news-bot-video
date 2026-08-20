# Конфигурация (CONFIGURATION.md)

Полное описание всех переменных окружения для настройки бота.

---

## Telegram

| Переменная | Обязательна | По умолчанию | Описание |
|------------|-------------|--------------|----------|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Токен бота от @BotFather |
| `TELEGRAM_CHANNEL_ID` | ✅ | — | ID канала для публикации (@username или -100...) |
| `MAX_CAPTION_LENGTH` | | 1024 | Лимит длины подписи к медиа |
| `APPROVE_POSTS` | | false | Требует утверждения постов перед публикацией |
| `OWNER_CHAT_ID` | | — | Числовой ID владельца (если известен заранее) |
| `OWNER_SETUP_CODE` | | — | Секретный код для первичной настройки владельца через /start |

---

## Расписание

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `POLL_INTERVAL_MINUTES` | 30 | Интервал сбора новостей (мин) |
| `MIN_POST_DELAY_MINUTES` | 0 | Мин. задержка перед публикацией (мин) |
| `MAX_POST_DELAY_MINUTES` | 30 | Макс. задержка перед публикацией (мин) |
| `QUIET_START_HOUR` | 22 | Начало тихих часов (0-23) |
| `QUIET_END_HOUR` | 8 | Конец тихих часов (0-23) |
| `TIMEZONE` | Europe/Moscow | Часовой пояс (IANA tz) |
| `PEAK_HOURS` | 8-21 | Часы пик для публикации (через запятую) |
| `QUEUE_CAPACITY` | 15 | Макс. постов в очереди |
| `NEWS_FRESHNESS_HOURS` | 36 | Макс. возраст новости при публикации (ч) |
| `REVIEW_TIMES` | 8,15 | Часы ежедневного ревью (через запятую) |
| `REVIEW_WINDOW_HOURS` | 7 | Окно ревью вперёд (ч) |
| `REVIEW_ENABLED` | true | Включить ежедневное ревью |
| `GAME_REPEAT_HOURS` | 24 | Мин. интервал между упоминаниями одной игры (ч) |

---

## Источники

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `ENABLED_SOURCES` | stopgame,igromania,dtf,3dnews,vgtimes | Активные источники (через запятую) |
| `MAX_ITEMS_PER_SOURCE` | 2 | Макс. новостей из одного источника за прогон |
| `MAX_ITEM_AGE_HOURS` | 48 | Макс. возраст новости в RSS (ч) |
| `HTTP_TIMEOUT` | 20.0 | Таймаут HTTP запросов (сек) |

---

## Фото

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `MIN_IMAGE_WIDTH` | 1024 | Мин. ширина картинки (px) |
| `PIXABAY_API_KEY` | — | Ключ Pixabay для поиска фото |
| `STEAMGRIDDB_API_KEY` | — | Ключ SteamGridDB для официальных обложек |

---

## Обычные видео-посты (ENABLE_VIDEO_POSTS)

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `MAX_VIDEO_SIZE_MB` | 50 | Макс. размер видео из статьи (МБ) |
| `ENABLE_VIDEO_POSTS` | false | Включить публикацию видео из статей |
| `YTDLP_ENABLED` | true | Включить yt-dlp для YouTube |
| `YTDLP_MAX_MB` | 150 | Макс. размер YouTube видео (МБ) |
| `YTDLP_HEIGHT` | 720 | Макс. высота YouTube видео |
| `YTDLP_TIMEOUT` | 300 | Таймаут yt-dlp (сек) |

---

## AI-видео (VIDEO_ENABLED)

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `VIDEO_ENABLED` | true | Включить генерацию AI-видео |
| `VIDEO_WIDTH` | 1080 | Ширина видео (px) |
| `VIDEO_HEIGHT` | 1920 | Высота видео (px) |
| `VIDEO_FPS` | 30 | FPS видео |
| `MIN_VIDEO_DURATION` | 20 | Мин. длительность видео (сек) |
| `MAX_VIDEO_DURATION` | 45 | Макс. длительность видео (сек) |
| `VIDEO_ENCODER` | auto | Кодек: auto/libx264/libopenh264/h264_nvenc/h264_vaapi/h264_qsv |
| `VIDEO_PADDING` | blur | Заполнение фона: blur/crop |
| `VIDEO_TRANSITION_TYPE` | crossfade | Переход между фото: crossfade/fade |
| `VIDEO_TRANSITION_DURATION` | 0.5 | Длительность перехода (сек) |
| `VIDEO_BITRATE_K` | 4000 | Битрейт видео (кбит/с) |
| `MAX_VIDEO_PHOTOS` | 5 | Макс. фото в одном видео |
| `VIDEO_WORK_CONCURRENCY` | 1 | Параллельных рендеров |
| `VIDEO_TIMEOUT_SCRIPT` | 120 | Таймаут генерации сценария (сек) |
| `VIDEO_TIMEOUT_TTS` | 180 | Таймаут озвучки (сек) |
| `VIDEO_TIMEOUT_WHISPER` | 300 | Таймаут субтитров (сек) |
| `VIDEO_TIMEOUT_FFMPEG` | 600 | Таймаут рендера (сек) |
| `VIDEO_TIMEOUT_DRIVE` | 120 | Таймаут загрузки на Drive (сек) |

---

## TTS (озвучка)

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `TTS_ENGINE` | piper | Движок: piper/espeak-ng/auto |
| `TTS_VOICE` | ru_RU-irina-medium | Голос Piper (файл в assets/piper/) |
| `TTS_SPEED` | 1.0 | Скорость речи (0.5-2.0) |
| `PIPER_BIN` | piper | Путь к бинарнику Piper |

---

## Whisper (субтитры)

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `WHISPER_MODEL` | small | Модель: tiny/base/small/medium/large |
| `WHISPER_DEVICE` | cpu | Устройство: cpu/cuda |
| `WHISPER_TIMEOUT_SECONDS` | 300 | Таймаут транскрипции (сек) |

---

## Google Drive

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `UPLOAD_TO_DRIVE` | false | Загружать видео на Drive |
| `GOOGLE_DRIVE_FOLDER_ID` | — | ID папки на Drive (опционально) |
| `GOOGLE_CREDENTIALS_FILE` | credentials.json | Путь к credentials.json |
| `GOOGLE_TOKEN_FILE` | token.json | Путь к token.json |

---

## Пути

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `DB_PATH` | data/published.db | Путь к SQLite БД |
| `LOG_DIR` | logs | Директория логов |
| `OUTPUT_DIR` | output | Директория временных файлов |
| `VIDEOS_DIR` | output/videos | Директория готовых видео |
| `BACKGROUNDS_DIR` | assets/backgrounds | Фоновые видео для рендера |
| `PIPER_DIR` | assets/piper | Модели Piper TTS |
| `WORK_DIR` | work | Рабочая директория |

---

## LLM (Ollama)

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `OLLAMA_BASE_URL` | http://localhost:11434 | Адрес Ollama API |
| `OLLAMA_MODEL` | llama3.1:8b | Основная модель |
| `OLLAMA_FALLBACK_MODEL` | mistral | Запасная модель |
| `OLLAMA_CONCURRENCY` | 2 | Параллельных запросов к Ollama |
| `OLLAMA_TIMEOUT` | 120.0 | Таймаут запроса (сек) |
| `MIN_NEWS_SCORE` | 7 | Мин. балл новости для видео (1-10) |

---

## LLM Fallback (OpenAI-совместимый)

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `LLM_FALLBACK_ENABLED` | false | Включить fallback провайдер |
| `LLM_FALLBACK_BASE_URL` | — | Базовый URL (OpenAI, OpenRouter, YandexGPT...) |
| `LLM_FALLBACK_API_KEY` | — | API ключ |
| `LLM_FALLBACK_MODEL` | — | Имя модели |

---

## Ресурсные лимиты видео

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `VIDEO_MAX_CPU_PERCENT` | 85 | Макс. CPU% перед рендером |
| `VIDEO_MAX_MEMORY_PERCENT` | 85 | Макс. RAM% перед рендером |
| `VIDEO_MIN_FREE_MEMORY_MB` | 1024 | Мин. свободной RAM (МБ) |

---

## Circuit Breaker (LLM)

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `LLM_CB_THRESHOLD` | 3 | Неудач перед открытием цепи |
| `LLM_CB_RECOVERY` | 300.0 | Секунд до HALF_OPEN |

---

## Пример .env

```bash
# Telegram
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHANNEL_ID=@mychannel
OWNER_CHAT_ID=123456789

# Ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.1:8b
OLLAMA_FALLBACK_MODEL=mistral

# Видео
VIDEO_ENABLED=true
TTS_ENGINE=piper
TTS_VOICE=ru_RU-irina-medium

# Опционально
PIXABAY_API_KEY=your_key
STEAMGRIDDB_API_KEY=your_key
UPLOAD_TO_DRIVE=false
```

---

## Приоритет загрузки

1. Переменные окружения (`.env` или systemd Environment)
2. Значения по умолчанию в `config.py`

Переменные в `.env` имеют приоритет над системными переменными окружения.