import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

DEFAULT_SOURCES = ("stopgame", "igromania", "dtf", "3dnews", "vgtimes")


@dataclass
class Config:
    telegram_token: str
    channel_id: str
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
    ollama_fallback_model: str = "mistral"
    poll_interval_minutes: int = 30
    min_post_delay_minutes: int = 0
    max_post_delay_minutes: int = 30
    enabled_sources: list[str] = field(default_factory=lambda: list(DEFAULT_SOURCES))
    max_items_per_source: int = 2
    max_item_age_hours: int = 48
    min_image_width: int = 1024
    max_caption_length: int = 1024
    max_video_size_mb: int = 50
    ytdlp_enabled: bool = True
    ytdlp_max_mb: int = 150
    ytdlp_height: int = 720
    ytdlp_timeout: int = 300
    quiet_start_hour: int = 22
    quiet_end_hour: int = 8
    timezone: str = "Europe/Moscow"
    peak_hours: list[int] = field(default_factory=lambda: list(range(8, 22)))
    queue_capacity: int = 15
    news_freshness_hours: int = 36
    approve_posts: bool = False
    review_times: list[int] = field(default_factory=lambda: [8, 15])
    review_window_hours: int = 7
    review_enabled: bool = True
    owner_setup_code: str | None = None
    game_repeat_hours: int = 24
    pixabay_api_key: str | None = None
    steamgriddb_api_key: str | None = None
    db_path: str = "data/published.db"
    log_dir: str = "logs"
    http_timeout: float = 20.0
    dry_run: bool = False


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


def _env_hours(name: str, default: list[int]) -> list[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return [int(hour.strip()) for hour in raw.split(",")]
    except ValueError:
        raise SystemExit(f"Ошибка конфигурации: {name} должен быть списком целых часов через запятую.")


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

    pixabay = os.getenv("PIXABAY_API_KEY", "").strip() or None
    steamgriddb = os.getenv("STEAMGRIDDB_API_KEY", "").strip() or None

    cfg = Config(
        telegram_token=token,
        channel_id=channel_id,
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip(),
        ollama_model=os.getenv("OLLAMA_MODEL", "llama3.1:8b").strip(),
        ollama_fallback_model=os.getenv("OLLAMA_FALLBACK_MODEL", "mistral").strip(),
        poll_interval_minutes=_env_int("POLL_INTERVAL_MINUTES", 30),
        min_post_delay_minutes=_env_int("MIN_POST_DELAY_MINUTES", 0),
        max_post_delay_minutes=_env_int("MAX_POST_DELAY_MINUTES", 30),
        enabled_sources=sources,
        max_items_per_source=_env_int("MAX_ITEMS_PER_SOURCE", 2),
        max_item_age_hours=_env_int("MAX_ITEM_AGE_HOURS", 48),
        min_image_width=_env_int("MIN_IMAGE_WIDTH", 1024),
        max_caption_length=_env_int("MAX_CAPTION_LENGTH", 1024),
        max_video_size_mb=_env_int("MAX_VIDEO_SIZE_MB", 50),
        ytdlp_enabled=_env_bool("YTDLP_ENABLED", True),
        ytdlp_max_mb=_env_int("YTDLP_MAX_MB", 150),
        ytdlp_height=_env_int("YTDLP_HEIGHT", 720),
        ytdlp_timeout=_env_int("YTDLP_TIMEOUT", 300),
        quiet_start_hour=_env_int("QUIET_START_HOUR", 22),
        quiet_end_hour=_env_int("QUIET_END_HOUR", 8),
        timezone=os.getenv("TIMEZONE", "Europe/Moscow").strip(),
        peak_hours=_env_hours("PEAK_HOURS", list(range(8, 22))),
        queue_capacity=_env_int("QUEUE_CAPACITY", 15),
        news_freshness_hours=_env_int("NEWS_FRESHNESS_HOURS", 36),
        approve_posts=_env_bool("APPROVE_POSTS", False),
        review_times=_env_hours("REVIEW_TIMES", [8, 15]),
        review_window_hours=_env_int("REVIEW_WINDOW_HOURS", 7),
        review_enabled=_env_bool("REVIEW_ENABLED", True),
        owner_setup_code=os.getenv("OWNER_SETUP_CODE", "").strip() or None,
        game_repeat_hours=_env_int("GAME_REPEAT_HOURS", 24),
        pixabay_api_key=pixabay,
        steamgriddb_api_key=steamgriddb,
        db_path=os.getenv("DB_PATH", "data/published.db").strip(),
        log_dir=os.getenv("LOG_DIR", "logs").strip(),
        http_timeout=float(os.getenv("HTTP_TIMEOUT", "20")),
        dry_run=dry_run,
    )
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
    return cfg
