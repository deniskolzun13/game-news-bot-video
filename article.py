import logging
import re
import urllib.parse
from dataclasses import dataclass

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


async def fetch_article(
    client: httpx.AsyncClient, url: str, source: str, timeout: float
) -> ArticleContent | None:
    """Скачивает страницу статьи и извлекает текст и ссылки на изображения."""
    selector = ARTICLE_SELECTORS.get(source)
    if not selector:
        log.warning("Для источника %s не задан селектор текста статьи", source)
        return None

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
                return None

    soup = BeautifulSoup(resp.text, "html.parser")
    container = soup.select_one(selector)
    if container is None:
        log.warning("Не найден контейнер текста (%s) на странице %s", selector, url)
        return None

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

    return ArticleContent(
        text=_clean_text(container),
        og_image=og_image,
        content_images=_content_images(container, url),
        video_urls=video_urls,
        youtube_links=youtube_links,
    )
