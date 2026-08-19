import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_SOURCES = ("stopgame", "igromania", "dtf", "3dnews", "vgtimes")

BASE_DIR = Path(__file__).resolve().parent


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        raise SystemExit(f"Ошибка конфигурации: {name} должен быть целым числом, получено: {raw!r}")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        raise SystemExit(f"Ошибка конфигурации: {name} должен быть числом, получено: {raw!r}")


def _env_hours(name: str, default: list[int]) -> list[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return [int(hour.strip()) for hour in raw.split(",")]
    except ValueError:
        raise SystemExit(f"Ошибка конфигурации: {name} должен быть списком целых часов через запятую.")


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


def find_font() -> str:
    """Возвращает путь к шрифту с кириллицей для заголовка видео."""
    custom = os.getenv("FONT_PATH", "").strip()
    if custom and Path(custom).exists():
        return custom
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    return ""


@dataclass
class Config:
    # --- Telegram ---
    telegram_token: str
    channel_id: str
    max_caption_length: int = 1024
    approve_posts: bool = False
    owner_setup_code: str | None = None

    # --- Расписание ---
    poll_interval_minutes: int = 30
    min_post_delay_minutes: int = 0
    max_post_delay_minutes: int = 30
    quiet_start_hour: int = 22
    quiet_end_hour: int = 8
    timezone: str = "Europe/Moscow"
    peak_hours: list[int] = field(default_factory=lambda: list(range(8, 22)))
    queue_capacity: int = 15
    news_freshness_hours: int = 36
    review_times: list[int] = field(default_factory=lambda: [8, 15])
    review_window_hours: int = 7
    review_enabled: bool = True
    game_repeat_hours: int = 24

    # --- Источники ---
    enabled_sources: list[str] = field(default_factory=lambda: list(DEFAULT_SOURCES))
    max_items_per_source: int = 2
    max_item_age_hours: int = 48
    http_timeout: float = 20.0

    # --- Ollama ---
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
    ollama_fallback_model: str = "mistral"
    ollama_concurrency: int = 2
    ollama_timeout: float = 120.0
    min_news_score: int = 7

    # --- Фото ---
    min_image_width: int = 1024
    pixabay_api_key: str | None = None
    steamgriddb_api_key: str | None = None

    # --- Посты с видео из статей (старое поведение) ---
    max_video_size_mb: int = 50
    enable_video_posts: bool = False
    ytdlp_enabled: bool = True
    ytdlp_max_mb: int = 150
    ytdlp_height: int = 720
    ytdlp_timeout: int = 300

    # --- Генерация видео (ai-news-video + workspace) ---
    video_enabled: bool = True
    video_width: int = 1080
    video_height: int = 1920
    video_fps: int = 30
    min_video_duration: int = 20
    max_video_duration: int = 45
    video_encoder: str = "auto"  # auto | libx264 | libopenh264 | h264_nvenc | h264_vaapi | h264_qsv
    video_padding: str = "blur"  # blur | crop
    video_transition_type: str = "crossfade"
    video_transition_duration: float = 0.5
    video_bitrate_k: int = 4000
    max_video_photos: int = 5
    video_work_concurrency: int = 1
    render_timeout_seconds: int = 600

    # --- TTS ---
    tts_engine: str = "piper"  # piper | espeak-ng | auto
    tts_voice: str = "ru_RU-irina-medium"
    tts_speed: float = 1.0
    piper_bin: str = "piper"

    # --- Whisper ---
    whisper_model: str = "small"
    whisper_device: str = "cpu"  # auto | cpu | cuda
    whisper_timeout_seconds: int = 300

    # --- Google Drive ---
    upload_to_drive: bool = False
    drive_folder_id: str = ""
    drive_credentials_file: str = "credentials.json"
    drive_token_file: str = "token.json"

    # --- Пути ---
    db_path: str = "data/published.db"
    log_dir: str = "logs"
    output_dir: str = "output"
    videos_dir: str = "output/videos"
    backgrounds_dir: str = "assets/backgrounds"
    piper_dir: str = "assets/piper"
    work_dir: str = "work"

    dry_run: bool = False

    @property
    def piper_model_path(self) -> str:
        return str(BASE_DIR / self.piper_dir / f"{self.tts_voice}.onnx")

    @property
    def piper_config_path(self) -> str:
        return str(BASE_DIR / self.piper_dir / f"{self.tts_voice}.onnx.json")


def load_config(dry_run: bool = False) -> Config:
    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    channel_id = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()

    if not dry_run:
        if not token:
            raise SystemExit(
                "TELEGRAM_BOT_TOKEN не задан. "
                "Получите токен у @BotFather и укажите его в файле .env "
                "(см. .env.example)."
            )
        if not channel_id:
            raise SystemExit(
                "TELEGRAM_CHANNEL_ID не задан. Укажите @имя_канала или числовой id "
                "(-100...) в файле .env."
            )

    sources_raw = os.getenv("ENABLED_SOURCES", "").strip()
    if sources_raw:
        sources = [s.strip().lower() for s in sources_raw.split(",") if s.strip()]
    else:
        sources = list(DEFAULT_SOURCES)

    cfg = Config(
        telegram_token=token,
        channel_id=channel_id,
        max_caption_length=_env_int("MAX_CAPTION_LENGTH", 1024),
        approve_posts=_env_bool("APPROVE_POSTS", False),
        owner_setup_code=os.getenv("OWNER_SETUP_CODE", "").strip() or None,
        poll_interval_minutes=_env_int("POLL_INTERVAL_MINUTES", 30),
        min_post_delay_minutes=_env_int("MIN_POST_DELAY_MINUTES", 0),
        max_post_delay_minutes=_env_int("MAX_POST_DELAY_MINUTES", 30),
        quiet_start_hour=_env_int("QUIET_START_HOUR", 22),
        quiet_end_hour=_env_int("QUIET_END_HOUR", 8),
        timezone=os.getenv("TIMEZONE", "Europe/Moscow").strip(),
        peak_hours=_env_hours("PEAK_HOURS", list(range(8, 22))),
        queue_capacity=_env_int("QUEUE_CAPACITY", 15),
        news_freshness_hours=_env_int("NEWS_FRESHNESS_HOURS", 36),
        review_times=_env_hours("REVIEW_TIMES", [8, 15]),
        review_window_hours=_env_int("REVIEW_WINDOW_HOURS", 7),
        review_enabled=_env_bool("REVIEW_ENABLED", True),
        game_repeat_hours=_env_int("GAME_REPEAT_HOURS", 24),
        enabled_sources=sources,
        max_items_per_source=_env_int("MAX_ITEMS_PER_SOURCE", 2),
        max_item_age_hours=_env_int("MAX_ITEM_AGE_HOURS", 48),
        http_timeout=_env_float("HTTP_TIMEOUT", 20.0),
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip(),
        ollama_model=os.getenv("OLLAMA_MODEL", "llama3.1:8b").strip(),
        ollama_fallback_model=os.getenv("OLLAMA_FALLBACK_MODEL", "mistral").strip(),
        ollama_concurrency=_env_int("OLLAMA_CONCURRENCY", 2),
        ollama_timeout=_env_float("OLLAMA_TIMEOUT", 120.0),
        min_news_score=_env_int("MIN_NEWS_SCORE", 7),
        min_image_width=_env_int("MIN_IMAGE_WIDTH", 1024),
        pixabay_api_key=os.getenv("PIXABAY_API_KEY", "").strip() or None,
        steamgriddb_api_key=os.getenv("STEAMGRIDDB_API_KEY", "").strip() or None,
        max_video_size_mb=_env_int("MAX_VIDEO_SIZE_MB", 50),
        enable_video_posts=_env_bool("ENABLE_VIDEO_POSTS", False),
        ytdlp_enabled=_env_bool("YTDLP_ENABLED", True),
        ytdlp_max_mb=_env_int("YTDLP_MAX_MB", 150),
        ytdlp_height=_env_int("YTDLP_HEIGHT", 720),
        ytdlp_timeout=_env_int("YTDLP_TIMEOUT", 300),
        video_enabled=_env_bool("VIDEO_ENABLED", True),
        video_width=_env_int("VIDEO_WIDTH", 1080),
        video_height=_env_int("VIDEO_HEIGHT", 1920),
        video_fps=_env_int("VIDEO_FPS", 30),
        min_video_duration=_env_int("MIN_VIDEO_DURATION", 20),
        max_video_duration=_env_int("MAX_VIDEO_DURATION", 45),
        video_encoder=os.getenv("VIDEO_ENCODER", "auto").strip(),
        video_padding=os.getenv("VIDEO_PADDING", "blur").strip().lower(),
        video_transition_type=os.getenv("VIDEO_TRANSITION_TYPE", "crossfade").strip().lower(),
        video_transition_duration=_env_float("VIDEO_TRANSITION_DURATION", 0.5),
        video_bitrate_k=_env_int("VIDEO_BITRATE_K", 4000),
        max_video_photos=_env_int("MAX_VIDEO_PHOTOS", 5),
        video_work_concurrency=_env_int("VIDEO_WORK_CONCURRENCY", 1),
        render_timeout_seconds=_env_int("RENDER_TIMEOUT_SECONDS", 600),
        tts_engine=os.getenv("TTS_ENGINE", "piper").strip().lower(),
        tts_voice=os.getenv("TTS_VOICE", "ru_RU-irina-medium").strip(),
        tts_speed=_env_float("TTS_SPEED", 1.0),
        piper_bin=os.getenv("PIPER_BIN", "piper").strip(),
        whisper_model=os.getenv("WHISPER_MODEL", "small").strip(),
        whisper_device=os.getenv("WHISPER_DEVICE", "cpu").strip().lower(),
        whisper_timeout_seconds=_env_int("WHISPER_TIMEOUT_SECONDS", 300),
        upload_to_drive=_env_bool("UPLOAD_TO_DRIVE", False),
        drive_folder_id=os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip(),
        drive_credentials_file=os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json").strip(),
        drive_token_file=os.getenv("GOOGLE_TOKEN_FILE", "token.json").strip(),
        db_path=os.getenv("DB_PATH", "data/published.db").strip(),
        log_dir=os.getenv("LOG_DIR", "logs").strip(),
        output_dir=os.getenv("OUTPUT_DIR", "output").strip(),
        videos_dir=os.getenv("VIDEOS_DIR", "output/videos").strip(),
        backgrounds_dir=os.getenv("BACKGROUNDS_DIR", "assets/backgrounds").strip(),
        piper_dir=os.getenv("PIPER_DIR", "assets/piper").strip(),
        work_dir=os.getenv("WORK_DIR", "work").strip(),
        dry_run=dry_run,
    )
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    if cfg.max_post_delay_minutes < cfg.min_post_delay_minutes:
        raise SystemExit(
            "Ошибка конфигурации: MAX_POST_DELAY_MINUTES не может быть "
            f"меньше MIN_POST_DELAY_MINUTES ({cfg.max_post_delay_minutes} < "
            f"{cfg.min_post_delay_minutes})."
        )
    if cfg.min_post_delay_minutes < 0:
        raise SystemExit("Ошибка конфигурации: MIN_POST_DELAY_MINUTES не может быть отрицательным.")
    if cfg.max_items_per_source < 1:
        raise SystemExit("Ошибка конфигурации: MAX_ITEMS_PER_SOURCE должен быть не меньше 1.")
    if cfg.max_item_age_hours < 0:
        raise SystemExit("Ошибка конфигурации: MAX_ITEM_AGE_HOURS не может быть отрицательным.")
    if cfg.news_freshness_hours <= 0:
        raise SystemExit("Ошибка конфигурации: NEWS_FRESHNESS_HOURS должен быть больше нуля.")
    if cfg.queue_capacity < 1:
        raise SystemExit("Ошибка конфигурации: QUEUE_CAPACITY должен быть не меньше 1.")
    if not 0 < cfg.http_timeout:
        raise SystemExit("Ошибка конфигурации: HTTP_TIMEOUT должен быть больше нуля.")
    if cfg.tts_speed <= 0:
        raise SystemExit("Ошибка конфигурации: TTS_SPEED должен быть больше нуля.")
    if cfg.video_width < 100 or cfg.video_height < 100:
        raise SystemExit("Ошибка конфигурации: VIDEO_WIDTH/VIDEO_HEIGHT слишком малы.")
    if cfg.video_fps <= 0 or cfg.video_fps > 60:
        raise SystemExit("Ошибка конфигурации: VIDEO_FPS должен быть от 1 до 60.")
    if cfg.video_transition_duration < 0:
        raise SystemExit("Ошибка конфигурации: VIDEO_TRANSITION_DURATION не может быть отрицательным.")
    if not all(0 <= hour <= 23 for hour in (cfg.quiet_start_hour, cfg.quiet_end_hour)):
        raise SystemExit("Ошибка конфигурации: QUIET_START_HOUR и QUIET_END_HOUR должны быть от 0 до 23.")
    if not all(0 <= hour <= 23 for hour in cfg.peak_hours):
        raise SystemExit("Ошибка конфигурации: часы в PEAK_HOURS должны быть от 0 до 23.")
    if cfg.review_enabled:
        if not cfg.review_times:
            raise SystemExit("Ошибка конфигурации: REVIEW_TIMES не может быть пустым при REVIEW_ENABLED=true.")
        if not all(0 <= hour <= 23 for hour in cfg.review_times):
            raise SystemExit("Ошибка конфигурации: часы в REVIEW_TIMES должны быть от 0 до 23.")
        if cfg.review_window_hours < 1:
            raise SystemExit("Ошибка конфигурации: REVIEW_WINDOW_HOURS должен быть не меньше 1.")