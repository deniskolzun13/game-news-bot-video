"""Высокоуровневый генератор озвучки.

Движки: piper (основной) -> espeak-ng (запасной). Выбор через TTS_ENGINE:
  - piper:    только Piper (упадёт, если нет бинарника/модели)
  - espeak-ng: только espeak-ng
  - auto:     Piper, при недоступности — espeak-ng
"""

import logging
import shutil
import subprocess
from pathlib import Path

from config import Config
from tts.piper import PiperTTS, PiperError

logger = logging.getLogger("tts")


class TTSError(RuntimeError):
    pass


def get_wav_duration(wav_path: Path) -> float:
    """Длительность WAV в секундах через ffprobe."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(wav_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    if proc.returncode != 0:
        raise TTSError("ffprobe не смог определить длительность WAV")
    return float(proc.stdout.decode().strip())


def _split_sentences(text: str) -> list[str]:
    """Разбивает текст на предложения (для espeak-ng лимита аргументов)."""
    import re
    parts = re.split(r"(?<=[.!?…])\s+", (text or "").strip())
    return [p for p in parts if p.strip()]


def _synthesize_espeak(text: str, dest: Path, speed: float = 1.0) -> Path:
    """Синтез через espeak-ng в WAV (фолбэк, когда Piper недоступен)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "espeak-ng",
        "-v", "ru",
        "-s", str(max(120, int(160 * speed))),
        "-w", str(dest),
        text,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        raise TTSError(
            "espeak-ng не смог синтезировать речь: " + (result.stderr or "")[:200]
        )
    return dest


class TTSEngine:
    """Генератор озвучки с фолбэком piper -> espeak-ng."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.piper = PiperTTS(
            piper_bin=cfg.piper_bin,
            model_path=Path(cfg.piper_model_path),
            config_path=Path(cfg.piper_config_path),
            speed=cfg.tts_speed,
        )
        self._engine = self._resolve_engine()

    def _resolve_engine(self) -> str:
        engine = (self.cfg.tts_engine or "auto").strip().lower()
        piper_ok = self.piper.is_available() and self.piper.check_model()
        espeak_ok = shutil.which("espeak-ng") is not None
        if engine == "piper":
            if not piper_ok:
                raise TTSError(
                    "TTS_ENGINE=piper, но Piper/модель не найдены. "
                    "Установите piper и русскую модель (см. README) "
                    "или используйте TTS_ENGINE=auto/espeak-ng."
                )
            return "piper"
        if engine == "espeak-ng":
            if not espeak_ok:
                raise TTSError("TTS_ENGINE=espeak-ng, но espeak-ng не установлен.")
            return "espeak-ng"
        # auto
        if piper_ok:
            return "piper"
        if espeak_ok:
            logger.warning("Piper недоступен — использую espeak-ng (фолбэк)")
            return "espeak-ng"
        raise TTSError("Нет доступного TTS: ни Piper, ни espeak-ng.")

    def is_available(self) -> bool:
        try:
            self._resolve_engine()
            return True
        except TTSError:
            return False

    def generate(self, text: str, out_dir: Path, name: str) -> Path:
        """Создаёт WAV и возвращает путь."""
        text = (text or "").strip()
        if not text:
            raise TTSError("Пустой текст для озвучки")
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        wav_path = out_dir / f"{name}.wav"

        if self._engine == "piper":
            self.piper.synthesize(text, wav_path)
        else:
            _synthesize_espeak(text, wav_path, speed=self.cfg.tts_speed)

        duration = get_wav_duration(wav_path)
        logger.info("[TTS] %s: OK (%.1f с)", self._engine, duration)
        return wav_path


__all__ = ["TTSEngine", "TTSError", "get_wav_duration"]