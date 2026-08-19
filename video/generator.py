"""Генерация готового MP4 через FFmpeg.

Озвучка (WAV) + ASS-субтитры (заголовок и фразы) + фото новости (несколько,
с переходами) или фоновое видео/цвет → H.264/AAC вертикальный MP4 9:16.

Валидация результата через ffprobe: длительность, разрешение, аудио/видео.
"""

import logging
import random
import subprocess
from pathlib import Path

from config import Config
from video.templates import (
    build_background_input,
    build_photos_filter,
    build_single_video_filter,
)

logger = logging.getLogger("video")

# Приоритет автовыбора кодировщика H.264
_ENCODER_PRIORITY = ["libx264", "libopenh264",
                     "h264_nvenc", "h264_vaapi", "h264_qsv"]

_cache: dict = {}


class VideoError(RuntimeError):
    pass


def detect_encoder(force: str = "") -> tuple[str, list[str]]:
    """Возвращает (имя кодировщика, дополнительные аргументы)."""
    if force and force != "auto":
        return force, []
    if "encoders" in _cache:
        available = _cache["encoders"]
    else:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30,
        )
        available = proc.stdout.decode("utf-8", errors="replace")
        _cache["encoders"] = available
    for name in _ENCODER_PRIORITY:
        if name in available:
            return name, []
    raise VideoError(
        "Не найден кодировщик H.264 в FFmpeg (нужен libx264, libopenh264 "
        "или аппаратный h264_nvenc/vaapi/qsv). Установите их или укажите "
        "VIDEO_ENCODER в .env."
    )


def _probe(path: Path) -> dict:
    """Возвращает информацию о медиа-файле через ffprobe (duration/streams)."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-show_entries", "stream=codec_type,width,height",
         "-of", "json", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
    )
    if proc.returncode != 0:
        raise VideoError("ffprobe не смог прочитать файл: " + str(path))
    import json

    return json.loads(proc.stdout.decode() or "{}")


class VideoGenerator:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def _pick_background(self) -> Path:
        backgrounds = sorted(Path(self.cfg.backgrounds_dir).glob("*.mp4"))
        return random.choice(backgrounds) if backgrounds else None

    def _audio_duration(self, wav: Path) -> float:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(wav)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        if proc.returncode != 0:
            raise VideoError("ffprobe не смог прочитать WAV")
        return float(proc.stdout.decode().strip())

    def _encoder_args(self) -> list[str]:
        name, _ = detect_encoder(self.cfg.video_encoder)
        if name == "libx264":
            return ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]
        if name in ("libopenh264", "h264_nvenc", "h264_vaapi", "h264_qsv"):
            return ["-c:v", name, "-b:v", f"{self.cfg.video_bitrate_k}k"]
        return ["-c:v", name, "-b:v", f"{self.cfg.video_bitrate_k}k"]

    def _check_duration(self, duration: float) -> None:
        """Контроль качества: длительность в заданном диапазоне."""
        if duration < self.cfg.min_video_duration:
            log.info("[FFMPEG] Аудио %.1f с короче минимума %d с — добавлю тишину",
                     duration, self.cfg.min_video_duration)
        if duration > self.cfg.max_video_duration:
            log.info("[FFMPEG] Аудио %.1f с больше лимита %d с — обрезаю",
                     duration, self.cfg.max_video_duration)

    def _validate_output(self, out_mp4: Path, duration: float) -> dict:
        """Валидация готового MP4: существует, размер, длительность, видео+аудио."""
        if not out_mp4.exists() or out_mp4.stat().st_size == 0:
            raise VideoError("FFmpeg не создал MP4-файл")
        info = _probe(out_mp4)
        fmt = info.get("format", {})
        actual = float(fmt.get("duration", 0) or 0)
        codecs = {s.get("codec_type") for s in info.get("streams", [])}
        ok = True
        reasons: list[str] = []
        if "video" not in codecs:
            ok, reasons = False, reasons + ["нет видеодорожки"]
        if "audio" not in codecs:
            ok, reasons = False, reasons + ["нет аудиодорожки"]
        if abs(actual - duration) > max(2.0, duration * 0.15):
            ok, reasons = False, reasons + [
                f"длительность {actual:.1f} с != {duration:.1f} с"
            ]
        for s in info.get("streams", []):
            if s.get("codec_type") == "video":
                w, h = s.get("width", 0), s.get("height", 0)
                if (w, h) != (self.cfg.video_width, self.cfg.video_height):
                    ok, reasons = False, reasons + [f"разрешение {w}x{h}"]
        if not ok:
            raise VideoError("Валидация не пройдена: " + ", ".join(reasons))
        logger.info("[FFMPEG] Валидация OK (%.1f с, %d МБ)",
                    actual, out_mp4.stat().st_size // (1024 * 1024))
        return {"duration": actual, "size": out_mp4.stat().st_size}

    def generate(self, audio_wav: Path, subtitle_ass: Path,
                 out_mp4: Path, photo_paths: list[Path] | None = None) -> Path:
        """Собирает MP4 из озвучки, субтитров и (опционально) фото новости."""
        out_mp4 = Path(out_mp4)
        out_mp4.parent.mkdir(parents=True, exist_ok=True)
        duration = self._audio_duration(audio_wav)
        self._check_duration(duration)
        target = max(duration, float(self.cfg.min_video_duration))

        encoder, _ = detect_encoder(self.cfg.video_encoder)
        photo_paths = [Path(p) for p in (photo_paths or []) if Path(p).exists()]

        if photo_paths:
            filter_complex = build_photos_filter(
                self.cfg, len(photo_paths), str(subtitle_ass), duration
            )
            inputs: list[str] = []
            for p in photo_paths[: self.cfg.max_video_photos]:
                inputs += ["-loop", "1", "-t", f"{duration / len(photo_paths):.3f}",
                           "-i", str(p)]
            photo_inputs = len(photo_paths)
            audio_index = photo_inputs
            logger.info("[VIDEO] Фото: %d, кодировщик: %s", len(photo_paths), encoder)

            command = ["ffmpeg", "-y"] + inputs + ["-i", str(audio_wav)]
            command += [
                "-filter_complex", filter_complex,
                "-map", "[v]", "-map", f"{audio_index}:a",
                "-af", "apad",
                "-t", f"{target:.2f}",
            ]
        else:
            bg_input, _ = build_background_input(self.cfg)
            filter_complex = build_single_video_filter(self.cfg, str(subtitle_ass))
            background = self._pick_background()
            if background:
                logger.info("[VIDEO] Фон: %s", background.name)
            command = ["ffmpeg", "-y"] + bg_input + ["-i", str(audio_wav)]
            command += [
                "-filter_complex", filter_complex,
                "-map", "[v]", "-map", "1:a",
                "-af", "apad",
                "-t", f"{target:.2f}",
            ]

        command += self._encoder_args()
        command += [
            "-pix_fmt", "yuv420p",
            "-r", str(self.cfg.video_fps),
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
            "-movflags", "+faststart",
            str(out_mp4),
        ]

        proc = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.cfg.render_timeout_seconds,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace")[-3000:]
            raise VideoError(f"FFmpeg завершился с ошибкой:\n{stderr}")

        self._validate_output(out_mp4, target)
        logger.info("[FFMPEG] MP4 готов: %s (%.1f с)", out_mp4.name, target)
        return out_mp4


__all__ = ["VideoGenerator", "VideoError", "detect_encoder"]