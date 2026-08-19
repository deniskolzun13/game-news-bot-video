"""Шаблоны визуального оформления вертикального видео.

Поддерживаются три режима фона:
  1. несколько фото новости — карточки поверх размытого фона (blur) с
     переходами crossfade/fade между ними;
  2. background mp4 (assets/backgrounds/*.mp4) — перебор + scale/crop;
  3. синтетический тёмный фон (lavfi color) — если нет ни фото, ни фона.

Субтитры (заголовок + фразы) рисует FFmpeg-фильтр subtitles из ASS-файла.
"""

import logging
from pathlib import Path

from config import Config

logger = logging.getLogger("video")


def _escape_filter_path(path) -> str:
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def build_single_video_filter(cfg: Config, subtitle_path: str) -> str:
    """Фильтр для одного видеовхода (background mp4 или lavfi color).

    Используется, когда у новости нет фото.
    """
    w = cfg.video_width
    h = cfg.video_height
    escaped_subs = _escape_filter_path(subtitle_path)
    return (
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},setsar=1,"
        f"subtitles='{escaped_subs}'[v]"
    )


def build_background_input(cfg: Config) -> tuple[list[str], bool]:
    """Возвращает аргументы входного потока фона.

    Первый элемент — список аргументов ffmpeg,
    второй — True, если фон берётся из файла (а не генерируется).
    """
    backgrounds = sorted(Path(cfg.backgrounds_dir).glob("*.mp4"))
    if backgrounds:
        return (["-stream_loop", "-1", "-i", str(backgrounds[0])], True)
    return (
        [
            "-f", "lavfi",
            "-i", f"color=c=0x101322:s={cfg.video_width}x{cfg.video_height}:r={cfg.video_fps}",
        ],
        False,
    )


def build_photos_filter(
    cfg: Config,
    photo_count: int,
    subtitle_path: str,
    duration: float,
) -> str:
    """Фильтр для видео из нескольких фото с переходами.

    Ожидается photo_count входов-картинок (индексы 0..n-1); аудио — вход n.
    Возвращает filter_complex строку. Каждая картинка: размытый фон + сама
    по центру; между фото — переход crossfade/fade; поверх — ASS-субтитры.
    """
    if photo_count < 1:
        raise ValueError("build_photos_filter: нужно хотя бы одно фото")

    w, h = cfg.video_width, cfg.video_height
    n = photo_count
    dur = max(duration, 0.1)
    per_photo = dur / n
    trans = max(0.1, min(cfg.video_transition_duration, per_photo * 0.5))
    transition_type = cfg.video_transition_type
    padding = cfg.video_padding

    filters: list[str] = []
    labels: list[str] = []
    for i in range(n):
        if padding == "blur":
            chain = (
                f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h},boxblur=20:5,setsar=1[bg{i}];"
                f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,setsar=1[fg{i}];"
                f"[bg{i}][fg{i}]overlay=(W-w)/2:(H-h)/2,setsar=1[o{i}]"
            )
        else:
            chain = (
                f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h},setsar=1[o{i}]"
            )
        filters.append(chain)
        labels.append(f"[o{i}]")

    if n == 1:
        v_out = "[o0]"
    elif transition_type == "crossfade":
        offsets: list[str] = []
        cum = 0.0
        for k in range(n - 1):
            cum += per_photo
            offsets.append(str(round(cum - k * trans, 3)))
        prev = "[o0]"
        for i in range(1, n):
            filters.append(
                f"{prev}[o{i}]xfade=transition=fade:"
                f"duration={trans:.3f}:offset={offsets[i - 1]}[vx{i}]"
            )
            prev = f"[vx{i}]"
        v_out = prev
    else:
        for i in range(n):
            fi = min(trans, per_photo / 2)
            fo = min(trans, per_photo / 2)
            filters.append(
                f"[o{i}]fade=t=in:st=0:d={fi:.3f},"
                f"fade=t=out:st={per_photo - fo:.3f}:d={fo:.3f}[fv{i}]"
            )
        concat_in = "".join(f"[fv{i}]" for i in range(n))
        filters.append(f"{concat_in}concat=n={n}:v=1:a=0[vx]")
        v_out = "[vx]"

    filters.append(f"{v_out}subtitles='{_escape_filter_path(subtitle_path)}'[v]")
    return ";".join(filters)


__all__ = [
    "build_single_video_filter",
    "build_background_input",
    "build_photos_filter",
]