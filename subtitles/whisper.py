"""Локальные субтитры через whisper-timestamped.

Создаёт крупные субтитры, разбитые на короткие фразы, синхронные с озвучкой.
Заголовок новости — сверху, фразы — снизу (вертикальное видео 9:16).
"""

import logging
import re
import threading
from pathlib import Path

logger = logging.getLogger("whisper")

_SUBTITLE_FONT = "DejaVu Sans"

_SUBTITLE_STYLE = (
    "Style: Subtitle,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,"
    "&H96000000,-1,0,0,0,100,100,0,0,1,4,2,2,40,40,120,1"
)

_HEADLINE_STYLE = (
    "Style: Headline,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,"
    "&H96000000,-1,0,0,0,100,100,0,0,1,4,3,5,40,40,300,1"
)

_ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{subtitle_style}
{headline_style}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

_MAX_WORDS = 5
_MAX_LINE_DURATION = 2.6
_MIN_LINE_DURATION = 0.8

_WHISPER_MODEL = None
_WHISPER_TS = None
_MODEL_LOCK = threading.Lock()


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    centi = int(round(seconds * 100))
    h, rem = divmod(centi, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _word_text(word) -> str:
    if isinstance(word, dict):
        return re.sub(r"^\s+|\s+$", "", word.get("text", ""))
    return re.sub(r"^\s+|\s+$", "", getattr(word, "text", ""))


def _w_start(word) -> float:
    return word["start"] if isinstance(word, dict) else word.start


def _w_end(word) -> float:
    return word["end"] if isinstance(word, dict) else word.end


def _group_words(words, max_words=_MAX_WORDS,
                 max_duration=_MAX_LINE_DURATION,
                 min_duration=_MIN_LINE_DURATION):
    """Группирует слова в короткие строки субтитров."""
    lines = []
    current = []
    start = None
    for word in words:
        if not current:
            current = [word]
            start = _w_start(word)
            continue
        span = _w_end(word) - start
        if len(current) >= max_words or (
            span > max_duration and _w_end(word) - start >= min_duration
        ):
            lines.append((start, _w_end(current[-1]), current))
            current = [word]
            start = _w_start(word)
        else:
            current.append(word)
    if current:
        lines.append((start, _w_end(current[-1]), current))
    return lines


def _merge_short_lines(lines, min_gap=0.25, max_words=_MAX_WORDS * 2):
    """Объединяет слишком короткие/близкие строки."""
    if len(lines) < 2:
        return lines
    merged = [list(lines[0])]
    for line in lines[1:]:
        prev_start, prev_end, prev_words = merged[-1]
        start, end, words = line
        prev_words = list(prev_words)
        if (len(prev_words) < 2 and (start - prev_end) < min_gap) or (
            len(prev_words) + len(words) <= max_words and (start - prev_end) < 0.12
        ):
            prev_words.extend(words)
            merged[-1] = (prev_start, max(prev_end, end), prev_words)
        else:
            merged.append([start, end, list(words)])
    return merged


class WhisperTranscriber:
    """Транскрипция WAV через whisper-timestamped (слов с таймкодами)."""

    def __init__(self, model_size: str = "small", device: str = "cpu",
                 timeout: int = 300) -> None:
        self.model_size = model_size
        self.device = device
        self.timeout = timeout

    def _load_model(self):
        global _WHISPER_MODEL, _WHISPER_TS
        if _WHISPER_MODEL is not None:
            return _WHISPER_MODEL
        with _MODEL_LOCK:
            if _WHISPER_MODEL is None:
                import whisper_timestamped as whisper_ts

                _WHISPER_TS = whisper_ts
                logger.info("[WHISPER] Загрузка модели: %s (device=%s)",
                            self.model_size, self.device)
                _WHISPER_MODEL = whisper_ts.load_model(
                    self.model_size, device=self.device
                )
        return _WHISPER_MODEL

    def get_words(self, audio_path: Path):
        """Возвращает список слов с таймкодами из WAV."""
        import whisper_timestamped as whisper_ts

        model = self._load_model()
        audio = whisper_ts.load_audio(str(audio_path))
        result = whisper_ts.transcribe(
            model, audio, language="ru", verbose=False
        )
        words = []
        for segment in result.get("segments", []):
            for word in segment.get("words", []):
                text = re.sub(r"^\s+|\s+$", "", word.get("text", ""))
                if not text:
                    continue
                words.append(
                    {
                        "text": text,
                        "start": float(word["start"]),
                        "end": float(word["end"]),
                    }
                )
        return words

    def make_subtitles(self, audio_path: Path, headline: str,
                       total_duration: float, out_dir: Path,
                       name: str) -> Path:
        """Создаёт ASS-субтитры (заголовок сверху + фразы снизу).

        Возвращает путь к ASS-файлу.
        """
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        ass_path = out_dir / f"{name}.ass"
        srt_path = out_dir / f"{name}.srt"

        try:
            words = self.get_words(audio_path)
        except Exception as exc:
            logger.warning("[WHISPER] Ошибка транскрипции (%s) — субтитры пустые", exc)
            words = []

        if not words:
            logger.warning("[WHISPER] Whisper не распознал речь — субтитры будут пустыми")
            lines = []
        else:
            grouped = _group_words(words)
            lines = _merge_short_lines(grouped)

        body = self._ass_body(lines, headline, total_duration)
        header = _ASS_HEADER.format(
            subtitle_style=_SUBTITLE_STYLE.format(
                font=_SUBTITLE_FONT, size=72
            ),
            headline_style=_HEADLINE_STYLE.format(
                font=_SUBTITLE_FONT, size=96
            ),
        )
        ass_path.write_text(header + body, encoding="utf-8")
        srt_path.write_text(self._srt_body(lines), encoding="utf-8")
        logger.info("[WHISPER] Субтитры: OK (%d фраз)", len(lines))
        return ass_path

    def _ass_body(self, lines, headline: str, total_duration: float) -> str:
        events = []
        # Заголовок в начале видео
        hl_start = 0.0
        hl_end = min(5.0, total_duration * 0.4)
        if headline:
            hl_text = self._wrap_headline(headline)
            events.append(
                f"Dialogue: 0,{_ass_time(hl_start)},{_ass_time(hl_end)},"
                f"Headline,,0,0,0,,{hl_text}\n"
            )
        for start, end, words in lines:
            text = " ".join(_word_text(w) for w in words)
            if not text:
                continue
            events.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},"
                f"Subtitle,,0,0,0,,{self._escape(text)}\n"
            )
        return "".join(events)

    def _srt_body(self, lines) -> str:
        def srt_time(seconds: float) -> str:
            seconds = max(0.0, seconds)
            ms = int(round((seconds - int(seconds)) * 1000))
            h, rem = divmod(int(seconds), 3600)
            m, s = divmod(rem, 60)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        blocks = []
        for index, (start, end, words) in enumerate(lines, start=1):
            text = " ".join(_word_text(w) for w in words)
            if not text:
                continue
            blocks.append(
                f"{index}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n"
            )
        return "\n".join(blocks)

    @staticmethod
    def _escape(text: str) -> str:
        return text.replace("\n", r"\N")

    @staticmethod
    def _wrap_headline(headline: str) -> str:
        """Разбивает заголовок на строки ~16 символов для крупного шрифта."""
        headline = headline.strip().rstrip(".!")
        chars = list(headline)
        lines = []
        current = ""
        for ch in chars:
            current += ch
            if len(current) >= 16 and ch == " ":
                lines.append(current.strip())
                current = ""
        if current.strip():
            lines.append(current.strip())
        if not lines:
            lines = [headline]
        return r"\N".join(lines)[:200]


__all__ = ["WhisperTranscriber"]