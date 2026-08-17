import calendar
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

import feedparser
import httpx

from parsers.base import NewsItem

log = logging.getLogger("parser")

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
}


def normalize_url(url: str) -> str:
    """Убирает из ссылки query-параметры (?from=rss и т.п.) для дедупликации."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _to_datetime(struct_time) -> datetime | None:
    try:
        return datetime.fromtimestamp(calendar.timegm(struct_time), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


class RssParser:
    """Базовый парсер новостей через RSS-ленту."""

    name: str = ""
    feed_url: str = ""
    timeout: float = 20.0

    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    def _is_relevant(self, item) -> bool:
        return True

    async def fetch_items(self, max_age_hours: int = 0) -> list[NewsItem]:
        resp = None
        for attempt in (1, 2):
            try:
                resp = await self._client.get(self.feed_url, headers=BROWSER_HEADERS)
                resp.raise_for_status()
                break
            except httpx.HTTPError as exc:
                log.warning("%s: ошибка загрузки RSS (попытка %d): %s", self.name, attempt, exc)
                if attempt == 2:
                    raise

        feed = feedparser.parse(resp.text)
        if feed.get("bozo") and not feed.entries:
            raise ValueError(f"Не удалось разобрать RSS {self.feed_url}: {feed.get('bozo_exception')}")

        now = datetime.now(timezone.utc)
        items: list[NewsItem] = []
        for entry in feed.entries:
            if not self._is_relevant(entry):
                continue
            url = normalize_url(entry.get("link", ""))
            if not url:
                continue
            published = _to_datetime(entry.get("published_parsed") or entry.get("updated_parsed"))
            if max_age_hours > 0 and published is not None:
                age = now - published
                if age > timedelta(hours=max_age_hours):
                    continue

            image_url = None
            for enc in entry.get("enclosures", []):
                if enc.get("type", "").startswith("image"):
                    image_url = enc.get("url")
                    break
            if not image_url and entry.get("media_content"):
                image_url = entry["media_content"][0].get("url")

            items.append(
                NewsItem(
                    title=(entry.get("title") or "").strip(),
                    url=url,
                    description=((entry.get("summary") or "") or (entry.get("description") or "")).strip(),
                    image_url=image_url,
                    published_at=published,
                    source=self.name,
                )
            )
        log.info("%s: получено %d новостей из RSS", self.name, len(items))
        return items