"""Независимый конвейер генерации видео для опубликованных новостей.

Цепочка: новость (news_id) → Ollama (сценарий+заголовок) → TTS (WAV) →
Whisper (субтитры) → FFmpeg (MP4 из фото/фона) → QC (ffprobe) →
локальное хранение → опционально Google Drive.

Очередь независима от Telegram: сбой видео НЕ ломает публикацию поста
(статусы video_status/status). Повтор генерации — через /retry.
"""

import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from config import Config
from google_drive.uploader import DriveUploader
from llm_ollama import OllamaClient, OllamaError
from subtitles.whisper import WhisperTranscriber
from tts.generator import TTSEngine, get_wav_duration
from video.generator import VideoGenerator, VideoError

log = logging.getLogger("video")


class VideoPipelineError(RuntimeError):
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

        work_dir = (
            Path(self._cfg.videos_dir)
            / datetime.now().strftime("%Y-%m-%d")
            / f"news_{news_id}"
        )
        work_dir.mkdir(parents=True, exist_ok=True)
        name = "video"

        try:
            # 1. Сценарий
            generated = await self._generate_script(news, model)
            result["script"] = generated["script"]
            result["headline"] = generated["headline"]
            result["steps"]["Сценарий"] = "OK"

            # 2. Озвучка
            wav = self._tts.generate(generated["script"], work_dir, name)
            duration = get_wav_duration(wav)
            result["steps"]["TTS"] = "OK"

            # 3. Субтитры
            ass = self._whisper.make_subtitles(
                wav, generated["headline"], duration, work_dir, name
            )
            result["steps"]["Whisper"] = "OK"

            # 4. Видео (фото новости, при наличии)
            mp4 = work_dir / f"{name}.mp4"
            photo_paths = []
            if photos:
                photo_paths = self._write_photos(
                    {"photos": photos}, work_dir
                )
            self._video.generate(wav, ass, mp4, photo_paths)
            result["steps"]["FFmpeg"] = "OK"
            result["mp4"] = str(mp4)

            # 5. Google Drive
            if self._uploader is not None:
                try:
                    date_str = datetime.now().strftime("%Y-%m-%d")
                    file_id = self._uploader.upload_auto(mp4, date_str)
                    result["drive_url"] = self._uploader.make_shareable(file_id)
                    result["steps"]["Google Drive"] = "OK"
                except Exception as exc:
                    log.warning("[DRIVE] Ошибка загрузки: %s", exc)
                    result["steps"]["Google Drive"] = "ОШИБКА"
            else:
                result["steps"]["Google Drive"] = "ПРОПУЩЕН"

            result["ready"] = True
            log.info("[VIDEO] #%d «%s» — готово (%s)", news_id, title, mp4.name)
            return result
        except (VideoError, VideoPipelineError, Exception) as exc:
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
                storage.mark_video_ready(
                    news_id, res["mp4"], self._mp4_duration(res["mp4"]),
                    res.get("script", ""), res.get("headline", ""),
                )
                storage.mark_video_published(news_id, res.get("drive_url"))
                if news.get("status") == "telegram_published":
                    storage.set_news_status(news_id, "completed")
            else:
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

    async def close(self) -> None:
        await self._ollama.close()


__all__ = ["VideoPipeline", "VideoPipelineError"]