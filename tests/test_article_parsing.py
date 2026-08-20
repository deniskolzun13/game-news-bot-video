import asyncio
import os
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from article import ArticleContent, fetch_article


class MockResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=MagicMock(), response=self
            )


class MockClient:
    def __init__(self, fixture_name: str = None, status_code: int = 200):
        self.fixture_name = fixture_name
        self.status_code = status_code

    async def get(self, url, headers=None, timeout=None, follow_redirects=True):
        if self.fixture_name:
            fixture_path = os.path.join(
                os.path.dirname(__file__), "fixtures", self.fixture_name
            )
            with open(fixture_path, "r", encoding="utf-8") as f:
                text = f.read()
            return MockResponse(text, self.status_code)
        return MockResponse("<html><body>No fixture</body></html>", self.status_code)


@pytest.mark.asyncio
async def test_fetch_article_stopgame():
    """Парсинг статьи StopGame: текст, og:image, картинки, YouTube."""
    client = MockClient("stopgame_article.html")
    result = await fetch_article(
        client, "https://stopgame.ru/news/123", "stopgame", timeout=10.0
    )
    assert result is not None, "Должен вернуть ArticleContent"
    assert isinstance(result, ArticleContent)
    assert "RetroSpace" in result.text
    assert "1 октября" in result.text
    assert result.og_image == "https://stopgame.ru/uploads/og_image.jpg"
    assert len(result.content_images) >= 1
    assert any("youtu.be/dQw4w9WgXcQ" in link for link in result.youtube_links)


@pytest.mark.asyncio
async def test_fetch_article_igromania():
    """Парсинг статьи Igromania: текст, og:image, видео."""
    client = MockClient("igromania_article.html")
    result = await fetch_article(
        client, "https://www.igromania.ru/news/456", "igromania", timeout=10.0
    )
    assert result is not None
    assert "Крысиный Король" in result.text
    assert "DLC со Слэшем" in result.text
    assert result.og_image == "https://www.igromania.ru/uploads/og_igromania.jpg"
    assert len(result.video_urls) >= 1
    assert result.video_urls[0].endswith(".mp4")


@pytest.mark.asyncio
async def test_fetch_article_dtf():
    """Парсинг статьи DTF: текст, og:image, YouTube ссылка."""
    client = MockClient("dtf_article.html")
    result = await fetch_article(
        client, "https://dtf.ru/games/789", "dtf", timeout=10.0
    )
    assert result is not None
    assert "гротескного RPG" in result.text
    assert "15 ноября" in result.text
    assert result.og_image == "https://dtf.ru/og/dtf_og.jpg"
    assert any("youtu.be/dQw4w9WgXcQ" in link for link in result.youtube_links)


@pytest.mark.asyncio
async def test_fetch_article_3dnews():
    """Парсинг статьи 3DNews: itemprop=articleBody, og:video."""
    client = MockClient("3dnews_article.html")
    result = await fetch_article(
        client, "https://3dnews.ru/games/999", "3dnews", timeout=10.0
    )
    assert result is not None
    assert "Nightdive Studios" in result.text
    assert "24 сентября" in result.text
    assert result.og_image == "https://3dnews.ru/og/3dnews_og.jpg"
    assert len(result.video_urls) >= 1
    assert result.video_urls[0].endswith(".webm")


@pytest.mark.asyncio
async def test_fetch_article_vgtimes():
    """Парсинг статьи VGTimes: fallback на описание из RSS (нет селектора)."""
    client = MockClient("vgtimes_article.html")
    result = await fetch_article(
        client, "https://vgtimes.ru/article/111", "vgtimes", timeout=10.0
    )
    assert result is None


@pytest.mark.asyncio
async def test_fetch_article_page_unavailable():
    """Страница недоступна (404) — graceful fallback, не исключение."""
    client = MockClient("stopgame_404.html", status_code=404)
    result = await fetch_article(
        client, "https://stopgame.ru/news/404", "stopgame", timeout=10.0
    )
    assert result is None, "При 404 должен вернуть None, не падать"


@pytest.mark.asyncio
async def test_fetch_article_selector_not_found():
    """Селектор не найден на странице — graceful fallback."""
    html = "<html><body><div class='other'>Текст не в контейнере</div></body></html>"
    client = MockClient()
    client.fixture_name = None

    async def mock_get(*args, **kwargs):
        return MockResponse(html)

    client.get = mock_get
    result = await fetch_article(
        client, "https://stopgame.ru/news/bad", "stopgame", timeout=10.0
    )
    assert result is None


if __name__ == "__main__":
    asyncio.run(pytest.main([__file__, "-v"]))