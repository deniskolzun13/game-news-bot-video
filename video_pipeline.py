"""Независимый конвейер генерации видео для опубликованных новостей.

Цепочка: новость (news_id) → Ollama (сценарий+заголовок) → TTS (WAV) →
Whisper (субтитры) → FFmpeg (MP4 из фото/фона) → QC (ffprobe) →
локальное хранение → опционально Google Drive.

Очередь независима от Telegram: сбой видео НЕ ломает публикацию поста
(статусы video_status/status). Повтор генерации — через /retry.
"""

import asyncio
import logging
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

from config import Config
from google_drive.uploader import DriveUploader
from llm_ollama import OllamaClient, OllamaError
from subtitles.whisper import WhisperTranscriber
from tts.generator import TTSEngine, get_wav_duration
from video.generator import VideoGenerator, VideoError

log = logging.getLogger("video")


class VideoPipelineError(RuntimeError):
    pass


class ResourceLimitExceeded(VideoPipelineError):
    """Превышены лимиты ресурсов (CPU/RAM) для безопасного рендера."""
    pass


class VideoPipeline:
    def __init__(self, cfg: Config, notifier=None) -> None:
        self._cfg = cfg
        self._notifier = notifier
        self._ollama = OllamaClient(
            cfg.ollama_base_url, cfg.ollama_model, cfg.ollama_fallback_model,
            concurrency=cfg.ollama_concurrency,
        )
        self._tts = TTSEngine(cfg)
        self._whisper = WhisperTranscriber(
            cfg.whisper_model, cfg.whisper_device, cfg.whisper_timeout_seconds
        )
        self._video = VideoGenerator(cfg)
        self._uploader: DriveUploader | None = None
        if cfg.upload_to_drive:
            self._uploader = DriveUploader(
                Path(cfg.drive_credentials_file),
                Path(cfg.drive_token_file),
                cfg.drive_folder_id,
            )
            if not self._uploader.is_configured():
                log.warning("[DRIVE] credentials.json не найден — загрузка на Drive пропущена")
                self._uploader = None

        # Ресурсные лимиты
        self._max_cpu_percent = getattr(cfg, "video_max_cpu_percent", 85)
        self._max_memory_percent = getattr(cfg, "video_max_memory_percent", 85)
        self._min_free_memory_mb = getattr(cfg, "video_min_free_memory_mb", 1024)

        # Адаптивные таймауты (секунды)
        self._timeouts = {
            "script": getattr(cfg, "video_timeout_script", 120),
            "tts": getattr(cfg, "video_timeout_tts", 180),
            "whisper": getattr(cfg, "video_timeout_whisper", 300),
            "ffmpeg": getattr(cfg, "video_timeout_ffmpeg", 600),
            "drive": getattr(cfg, "video_timeout_drive", 120),
        }

        # Метрики производительности
        self._metrics = {
            "total_runs": 0,
            "successful_runs": 0,
            "failed_runs": 0,
            "total_duration": 0.0,
            "stage_durations": {
                "script": [],
                "tts": [],
                "whisper": [],
                "ffmpeg": [],
                "drive": [],
            },
        }

    def _check_resources(self) -> None:
        """Проверяет доступные ресурсы перед запуском тяжёлого рендера."""
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        free_mb = mem.available / (1024 * 1024)

        if cpu > self._max_cpu_percent:
            raise ResourceLimitExceeded(
                f"CPU {cpu:.1f}% > лимита {self._max_cpu_percent}%"
            )
        if mem.percent > self._max_memory_percent:
            raise ResourceLimitExceeded(
                f"RAM {mem.percent:.1f}% > лимита {self._max_memory_percent}%"
            )
        if free_mb < self._min_free_memory_mb:
            raise ResourceLimitExceeded(
                f"Свободно RAM {free_mb:.0f} МБ < минимума {self._min_free_memory_mb} МБ"
            )

        log.debug("[VIDEO] Ресурсы OK: CPU %.1f%%, RAM %.1f%%, свободно %.0f МБ",
                  cpu, mem.percent, free_mb)

    async def _run_with_timeout(self, coro, timeout: float, stage: str):
        """Запускает корутину с таймаутом и замером времени."""
        start = time.monotonic()
        try:
            result = await asyncio.wait_for(coro, timeout=timeout)
            elapsed = time.monotonic() - start
            self._metrics["stage_durations"][stage].append(elapsed)
            log.debug("[VIDEO] Этап %s завершён за %.1fс", stage, elapsed)
            return result
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            self._metrics["stage_durations"][stage].append(elapsed)
            raise VideoPipelineError(f"Таймаут этапа {stage} ({timeout}с)") from None

    async def _generate_script(self, news: dict, model: str) -> dict:
        """Сценарий и заголовок через LLM (JSON)."""
        try:
            return await self._ollama.video_script(
                model,
                news.get("title") or "",
                news.get("description") or "",
                news.get("source") or "",
                news.get("category") or "",
            )
        except OllamaError as exc:
            raise VideoPipelineError(f"Генерация сценария не удалась: {exc}") from exc

    def _write_photos(self, news: dict, work_dir: Path) -> list[Path]:
        """Сохраняет фото новости (bytes) во временные файлы для FFmpeg."""
        photo_bytes = news.get("photos") or []
        paths: list[Path] = []
        for i, data in enumerate(photo_bytes[: self._cfg.max_video_photos]):
            if not data:
                continue
            p = work_dir / f"photo_{i}.jpg"
            p.write_bytes(data)
            paths.append(p)
        return paths

    async def process_news(self, news: dict, model: str,
                           photos: list[bytes] | None = None) -> dict:
        """Полный цикл генерации одного видео. Возвращает dict с результатом."""
        news_id = news.get("id")
        title = (news.get("title") or "Без названия")[:70]
        result = {"news_id": news_id, "title": title, "ready": False,
                  "steps": {}, "error": None, "mp4": None, "drive_url": None}

        # Проверка ресурсов перед стартом
        try:
            self._check_resources()
        except ResourceLimitExceeded as exc:
            result["error"] = f"Ресурсы: {exc}"
            log.warning("[VIDEO] #%d пропущено: %s", news_id, exc)
            return result

        work_dir = (
            Path(self._cfg.videos_dir)
            / datetime.now().strftime("%Y-%m-%d")
            / f"news_{news_id}"
        )
        work_dir.mkdir(parents=True, exist_ok=True)
        name = "video"

        try:
            # 1. Сценарий
            generated = await self._run_with_timeout(
                self._generate_script(news, model),
                self._timeouts["script"],
                "script",
            )
            result["script"] = generated["script"]
            result["headline"] = generated["headline"]
            result["steps"]["Сценарий"] = "OK"

            # 2. Озвучка
            wav = await self._run_with_timeout(
                asyncio.to_thread(self._tts.generate, generated["script"], work_dir, "video"),
                self._timeouts["tts"],
                "tts",
            )
            duration = get_wav_duration(wav)
            result["steps"]["TTS"] = "OK"

            # 3. Субтитры
            ass = await self._run_with_timeout(
                asyncio.to_thread(
                    self._whisper.make_subtitles, wav, generated["headline"],
                    duration, work_dir, "video"
                ),
                self._timeouts["whisper"],
                "whisper",
            )
            result["steps"]["Whisper"] = "OK"

            # 4. Видео (фото новости, при наличии)
            mp4 = work_dir / f"video.mp4"
            photo_paths = []
            if photos:
                photo_paths = self._write_photos({"photos": photos}, work_dir)
            await self._run_with_timeout(
                asyncio.to_thread(self._video.generate, wav, ass, mp4, photo_paths),
                self._timeouts["ffmpeg"],
                "ffmpeg",
            )
            result["steps"]["FFmpeg"] = "OK"
            result["mp4"] = str(mp4)

            # 5. Google Drive
            if self._uploader is not None:
                try:
                    date_str = datetime.now().strftime("%Y-%m-%d")
                    file_id = await self._run_with_timeout(
                        asyncio.to_thread(self._uploader.upload_auto, mp4, date_str),
                        self._timeouts["drive"],
                        "drive",
                    )
                    result["drive_url"] = self._uploader.make_shareable(file_id)
                    result["steps"]["Google Drive"] = "OK"
                except Exception as exc:
                    log.warning("[DRIVE] Ошибка загрузки: %s", exc)
                    result["steps"]["Google Drive"] = "ОШИБКА"
            else:
                result["steps"]["Google Drive"] = "ПРОПУЩЕН"

            result["ready"] = True
            log.info("[VIDEO] #%d «%s» — готово", news_id, title)
            return result
        except (VideoError, VideoPipelineError, ResourceLimitExceeded, Exception) as exc:
            result["error"] = str(exc)
            log.error("[VIDEO] #%d «%s» — ошибка: %s", news_id, title, exc)
            return result

    async def process_pending(self, limit: int = 3) -> list[dict]:
        """Обрабатывает видео-очередь: новости с video_status none/pending/failed."""
        if not self._cfg.video_enabled:
            log.info("[VIDEO] VIDEO_ENABLED=false — видео-очередь не обрабатывается")
            return []
        from storage import Storage

        storage = Storage(self._cfg.db_path)
        model = await self._ollama.check()
        results = []
        try:
            for row in storage.video_pending(limit):
                news = dict(row)
                news_id = news["id"]
                storage.set_video_status(news_id, "video_processing")
                news["photos"] = self._load_photos(storage, news)
                res = await self.process_news(news, model)
                if res["ready"]:
                    self._metrics["total_runs"] += 1
                    self._metrics["successful_runs"] += 1
                    storage.mark_video_ready(
                        news_id, res["mp4"], self._mp4_duration(res["mp4"]),
                        res.get("script", ""), res.get("headline", ""),
                    )
                    storage.mark_video_published(news_id, res.get("drive_url"))
                    if news.get("status") == "telegram_published":
                        storage.set_news_status(news_id, "completed")
                    if self._notifier:
                        await self._notifier.notify(
                            "video",
                            f"✅ Видео готово: «{news.get('title', '')[:60]}»",
                        )
                else:
                    self._metrics["total_runs"] += 1
                    self._metrics["failed_runs"] += 1
                    storage.mark_video_failed(news_id, res["error"] or "неизвестная ошибка")
                results.append(res)
        finally:
            storage.close()
        return results

    async def generate_one(self, news_id: int) -> dict:
        """Ручная генерация видео для конкретной новости (/generate, --generate-video)."""
        from storage import Storage

        storage = Storage(self._cfg.db_path)
        try:
            news = storage.get_news(news_id)
            if news is None:
                return {"error": f"Новость #{news_id} не найдена", "ready": False}
            model = await self._ollama.check()
            storage.set_video_status(news_id, "video_processing")
            news["photos"] = self._load_photos(storage, news)
            res = await self.process_news(news, model)
            if res["ready"]:
                self._metrics["total_runs"] += 1
                self._metrics["successful_runs"] += 1
                storage.mark_video_ready(
                    news_id, res["mp4"], self._mp4_duration(res["mp4"]),
                    res.get("script", ""), res.get("headline", ""),
                )
                storage.mark_video_published(news_id, res.get("drive_url"))
                if news.get("status") == "telegram_published":
                    storage.set_news_status(news_id, "completed")
            else:
                self._metrics["total_runs"] += 1
                self._metrics["failed_runs"] += 1
                storage.mark_video_failed(news_id, res["error"] or "неизвестная ошибка")
            return res
        finally:
            storage.close()

    @staticmethod
    def _load_photos(storage, news: dict) -> list[bytes]:
        """Фото новости для видео-фона: из news.photos, затем из post_queue."""
        photos = storage.load_news_photos(news)
        if photos:
            return photos
        row = storage.get_item_by_url(news.get("url") or "")
        if row is None:
            return []
        return row.get("photos") or []

    @staticmethod
    def _mp4_duration(mp4: str) -> float:
        import subprocess

        try:
            proc = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", mp4],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
            )
            return float(proc.stdout.decode().strip() or 0)
        except Exception:
            return 0.0

    def get_metrics(self) -> dict:
        """Возвращает метрики производительности видео-пайплайна."""
        m = self._metrics.copy()
        for stage, durations in m["stage_durations"].items():
            if durations:
                m[f"avg_{stage}_duration"] = sum(durations) / len(durations)
                m[f"max_{stage}_duration"] = max(durations)
        if m["total_runs"] > 0:
            m["success_rate"] = m["successful_runs"] / m["total_runs"]
        else:
            m["success_rate"] = 0.0
        return m

    async def close(self) -> None:
        await self._ollama.close()


__all__ = ["VideoPipeline", "VideoPipelineError", "ResourceLimitExceeded"]