from dataclasses import dataclass
from datetime import datetime


@dataclass
class NewsItem:
    """Одна новость из RSS-ленты источника."""

    title: str
    url: str
    description: str
    image_url: str | None
    published_at: datetime | None
    source: str