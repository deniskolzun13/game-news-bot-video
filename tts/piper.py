"""Низкоуровневая работа с Piper TTS через командную строку."""

import logging
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("tts")


class PiperError(RuntimeError):
    pass


class PiperTTS:
    def __init__(self, piper_bin: str, model_path: Path, config_path: Path,
                 speed: float = 1.0) -> None:
        self.piper_bin = piper_bin
        self.model_path = Path(model_path)
        self.config_path = Path(config_path)
        self.speed = speed
        if speed <= 0:
            raise PiperError("TTS_SPEED должен быть больше нуля")

    @property
    def length_scale(self) -> float:
        # speed=1.0 => length_scale=1.0; speed=1.2 => быстрее
        return 1.0 / self.speed

    def _resolve_binary(self) -> str | None:
        """Ищет бинарник piper: на PATH и рядом с текущим интерпретатором."""
        found = shutil.which(self.piper_bin)
        if found:
            return found
        candidates = [
            Path(sys.executable).parent / self.piper_bin,
            Path(sys.executable).parent / "piper",
        ]
        for path in candidates:
            if path.exists() and path.stat().st_mode & 0o111:
                return str(path)
        return None

    def is_available(self) -> bool:
        return self._resolve_binary() is not None

    def check_model(self) -> bool:
        return self.model_path.exists()

    def synthesize(self, text: str, output_wav: Path) -> Path:
        """Озвучивает текст в WAV (моно 22050 Гц) и возвращает путь."""
        if not self.is_available():
            raise PiperError(
                "Piper не найден. Установите Piper (pip install piper-tts) "
                "или бинарник из https://github.com/rhasspy/piper "
                "и повторите запуск."
            )
        binary = self._resolve_binary()
        if not self.model_path.exists():
            raise PiperError(
                f"Модель Piper не найдена: {self.model_path}\n"
                f"Скачайте русскую модель в assets/piper/ (см. README)."
            )

        output_wav = Path(output_wav)
        output_wav.parent.mkdir(parents=True, exist_ok=True)

        command = [
            binary,
            "--model", str(self.model_path),
            "--output_file", str(output_wav),
            "--length_scale", f"{self.length_scale:.2f}",
            "--sentence_silence", "0.25",
        ]
        if self.config_path.exists():
            command += ["--config", str(self.config_path)]

        try:
            proc = subprocess.run(
                command,
                input=text.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=600,
            )
        except subprocess.TimeoutExpired as exc:
            raise PiperError("Piper не завершился за 10 минут") from exc

        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace")[-2000:]
            raise PiperError(f"Piper завершился с ошибкой:\n{stderr}")

        if not output_wav.exists() or output_wav.stat().st_size == 0:
            raise PiperError("Piper не создал WAV-файл")
        return output_wav


__all__ = ["PiperTTS", "PiperError"]