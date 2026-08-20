import logging
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

import httpx
from bs4 import BeautifulSoup

from parsers.rss_base import BROWSER_HEADERS

log = logging.getLogger("article")

MAX_TEXT_CHARS = 6000

# Селектор контейнера с текстом статьи для каждого сайта.
# Проверено по фактической вёрстке сайтов (авг. 2026).
ARTICLE_SELECTORS = {
    "stopgame": "#material_content",
    "igromania": ".MaterialItemPage_materialSection__Z82Fd",
    "dtf": ".content__body",
    "3dnews": '[itemprop="articleBody"]',
}


class ParseFailureReason(Enum):
    """Причины неудачного парсинга для health-check."""
    NO_SELECTOR = "no_selector"
    HTTP_ERROR = "http_error"
    SELECTOR_NOT_FOUND = "selector_not_found"
    EMPTY_TEXT = "empty_text"
    TEXT_TOO_SHORT = "text_too_short"
    ONLY_TITLE = "only_title"
    NETWORK_ERROR = "network_error"


@dataclass
class ParseResult:
    """Результат парсинга статьи с деталями для health-check."""
    content: "ArticleContent | None"
    source: str
    url: str
    success: bool
    reason: ParseFailureReason | None = None
    details: str = ""
    text_length: int = 0
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc)


@dataclass
class ArticleContent:
    text: str
    og_image: str | None
    content_images: list[str]
    video_urls: list[str]
    youtube_links: list[str]


_YOUTUBE_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|embed/|shorts/|live/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)


def _youtube_id(url: str) -> str | None:
    m = _YOUTUBE_RE.search(url)
    return m.group(1) if m else None


def _find_youtube(soup) -> list[str]:
    """Ищет YouTube-видео по всем тегам: iframe, ссылки, lite-youtube (StopGame)."""
    links: list[str] = []
    seen: set[str] = set()
    for tag in soup.find_all(True):
        for attr in ("src", "href", "videoid", "data-videoid", "data-src"):
            value = urllib.parse.unquote(tag.get(attr) or "")
            vid = _youtube_id(value)
            if vid and vid not in seen:
                seen.add(vid)
                links.append(f"https://youtu.be/{vid}")
    return links


def _find_video_urls(soup) -> list[str]:
    urls: list[str] = []
    for meta in soup.find_all("meta"):
        prop = (meta.get("property") or meta.get("name") or "").lower()
        if prop in ("og:video", "og:video:url", "og:video:secure_url"):
            content = (meta.get("content") or "").strip()
            if content and content not in urls:
                urls.append(content)
    for source in soup.find_all("video"):
        src = source.get("src") or source.get("data-src") or ""
        if src and src not in urls:
            urls.append(src)
    return urls


def _clean_text(container) -> str:
    paragraphs = []
    for p in container.find_all("p"):
        text = p.get_text(" ", strip=True)
        if len(text) > 1:
            paragraphs.append(text)
    # Убираем подряд идущие дубликаты (типично для DTF-блоков).
    cleaned = [p for i, p in enumerate(paragraphs) if i == 0 or p != paragraphs[i - 1]]
    text = "\n".join(cleaned)
    return text[:MAX_TEXT_CHARS]


def _absolute_url(url: str, page_url: str) -> str:
    return urllib.parse.urljoin(page_url, url)


def _content_images(container, page_url: str) -> list[str]:
    images = []
    for img in container.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src or src.startswith("data:"):
            continue
        if src.endswith(".svg") or "/svg" in src:
            continue
        if "c120x100" in src or "scale_crop/72x72" in src:
            continue  # миниатюры аватарок
        src = _absolute_url(src, page_url)
        if src not in images:
            images.append(src)
    return images[:5]


def _fallback_extract_text(soup: BeautifulSoup, min_chars: int = 200) -> str | None:
    """Readability-подобный фолбэк: ищем самый большой текстовый блок в body.

    Убираем nav, header, footer, aside, script, style, form, ad-блоки.
    Возвращаем текст, если он достаточно длинный.
    """
    body = soup.find("body")
    if not body:
        return None

    # Удаляем шумовые элементы
    for tag in body.find_all(
        ["nav", "header", "footer", "aside", "script", "style", "form",
         "noscript", "iframe", "svg", "button", "input", "select"]
    ):
        tag.decompose()

    # Удаляем элементы с подозрительными классами (реклама, виджеты)
    suspicious = [
        "ad", "ads", "banner", "widget", "sidebar", "footer", "header",
        "navigation", "menu", "social", "share", "comment", "popup",
        "modal", "overlay", "cookie", "consent", "newsletter", "related",
        "recommended", "sponsored", "promo", "teaser"
    ]
    for tag in body.find_all(class_=True):
        classes = " ".join(tag.get("class", [])).lower()
        if any(s in classes for s in suspicious):
            tag.decompose()

    # Собираем текст из оставшихся p, div, article, section, main
    candidates = []
    for tag in body.find_all(["p", "div", "article", "section", "main"]):
        text = tag.get_text(" ", strip=True)
        if len(text) >= min_chars:
            candidates.append((len(text), text))

    if not candidates:
        # Фолбэк: весь текст body
        full_text = body.get_text(" ", strip=True)
        if len(full_text) >= min_chars:
            return full_text[:MAX_TEXT_CHARS]
        return None

    # Берём самый длинный кандидат
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1][:MAX_TEXT_CHARS]


async def fetch_article(
    client: httpx.AsyncClient, url: str, source: str, timeout: float
) -> ParseResult:
    """Скачивает страницу статьи и извлекает текст и ссылки на изображения.

    Возвращает ParseResult с деталями для health-check.
    При неудаче основного селектора пробует readability-фолбэк.
    """
    selector = ARTICLE_SELECTORS.get(source)
    if not selector:
        return ParseResult(
            content=None, source=source, url=url, success=False,
            reason=ParseFailureReason.NO_SELECTOR,
            details="У источника не задан селектор ARTICLE_SELECTORS"
        )

    resp = None
    for attempt in (1, 2):
        try:
            resp = await client.get(
                url, headers=BROWSER_HEADERS, timeout=timeout, follow_redirects=True
            )
            resp.raise_for_status()
            break
        except httpx.HTTPError as exc:
            log.warning("Не удалось скачать статью %s (попытка %d): %s", url, attempt, exc)
            if attempt == 2:
                return ParseResult(
                    content=None, source=source, url=url, success=False,
                    reason=ParseFailureReason.NETWORK_ERROR,
                    details=f"HTTP ошибка после 2 попыток: {exc}"
                )

    soup = BeautifulSoup(resp.text, "html.parser")
    container = soup.select_one(selector)

    if container is None:
        log.warning("Не найден контейнер текста (%s) на странице %s", selector, url)
        # Пробуем фолбэк
        fallback_text = _fallback_extract_text(soup)
        if fallback_text:
            log.info("Фолбэк сработал для %s: извлечено %d символов", source, len(fallback_text))
            og = soup.select_one('meta[property="og:image"]')
            og_image = _absolute_url(og.get("content"), url) if og and og.get("content") else None
            video_urls = []
            youtube_links = _find_youtube(soup)
            for video_url in _find_video_urls(soup):
                video_url = _absolute_url(video_url, url)
                if _youtube_id(video_url):
                    if video_url not in youtube_links:
                        youtube_links.append(video_url)
                elif video_url.endswith((".mp4", ".webm", ".mov")) and video_url not in video_urls:
                    video_urls.append(video_url)
            return ParseResult(
                content=ArticleContent(
                    text=fallback_text,
                    og_image=og_image,
                    content_images=[],
                    video_urls=video_urls,
                    youtube_links=youtube_links,
                ),
                source=source, url=url, success=True,
                reason=None,
                details="Основной селектор не найден, использован фолбэк",
                text_length=len(fallback_text)
            )
        return ParseResult(
            content=None, source=source, url=url, success=False,
            reason=ParseFailureReason.SELECTOR_NOT_FOUND,
            details=f"Селектор '{selector}' не найден, фолбэк не дал текста"
        )

    text = _clean_text(container)
    text_len = len(text)

    if text_len < 50:
        # Текст слишком короткий — пробуем фолбэк
        fallback_text = _fallback_extract_text(soup)
        if fallback_text and len(fallback_text) > text_len:
            log.info("Фолбэк дал больше текста для %s (%d vs %d)", source, len(fallback_text), text_len)
            text = fallback_text
            text_len = len(text)
            used_fallback = True
        else:
            return ParseResult(
                content=None, source=source, url=url, success=False,
                reason=ParseFailureReason.TEXT_TOO_SHORT,
                details=f"Текст слишком короткий: {text_len} символов",
                text_length=text_len
            )
    else:
        used_fallback = False

    og = soup.select_one('meta[property="og:image"]')
    og_image = _absolute_url(og.get("content"), url) if og and og.get("content") else None

    video_urls = []
    youtube_links = _find_youtube(soup)
    for video_url in _find_video_urls(soup):
        video_url = _absolute_url(video_url, url)
        if _youtube_id(video_url):
            if video_url not in youtube_links:
                youtube_links.append(video_url)
        elif video_url.endswith((".mp4", ".webm", ".mov")) and video_url not in video_urls:
            video_urls.append(video_url)

    content = ArticleContent(
        text=text,
        og_image=og_image,
        content_images=_content_images(container, url) if not used_fallback else [],
        video_urls=video_urls,
        youtube_links=youtube_links,
    )

    return ParseResult(
        content=content,
        source=source,
        url=url,
        success=True,
        reason=None,
        details="Фолбэк использован" if used_fallback else "Основной селектор",
        text_length=text_len
    )
