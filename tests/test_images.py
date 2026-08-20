import asyncio
import io
import os
import pytest
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import httpx
from PIL import Image

from images import (
    clean_image,
    download_video,
    extract_game_name,
    image_size_ok,
    pixabay_image,
    playground_image,
    prepare_photo,
    steam_image,
    steamgriddb_image,
    wikipedia_image,
)


# --- Helper: create valid test image ---
def make_test_image(width: int = 2000, height: int = 1500, color: str = "red") -> bytes:
    img = Image.new("RGB", (width, height), color)
    # Add some noise to prevent extreme JPEG compression
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    for i in range(0, width, 10):
        draw.line([(i, 0), (i, height)], fill=(0, 0, 0), width=1)
    for i in range(0, height, 10):
        draw.line([(0, i), (width, i)], fill=(0, 0, 0), width=1)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def make_small_image() -> bytes:
    img = Image.new("RGB", (100, 100), "blue")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=50)
    return buf.getvalue()


def make_svg() -> bytes:
    return b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="100" height="100"/></svg>'


# --- extract_game_name tests ---
@pytest.mark.parametrize("title,expected", [
    ("The Blood of Dawnwalker вышла", "The Blood of Dawnwalker"),
    ("GTA 6 анонсирована", "GTA 6"),
    ("Cyberpunk 2077 вышла", None),  # 2077 starts with digit, breaks run
    ("Mortal Shell II выпущен", "Mortal Shell II"),
    ("всё в нижнем регистре", None),
    ("", None),
])
def test_extract_game_name(title, expected):
    assert extract_game_name(title) == expected


# --- image_size_ok tests ---
def test_image_size_ok_valid():
    img = make_test_image()  # default 2000x1500, >30KB
    assert image_size_ok(img, min_width=1024) is True


def test_image_size_ok_too_small():
    img = make_small_image()
    assert image_size_ok(img, min_width=1024) is False


def test_image_size_ok_svg_rejected():
    assert image_size_ok(make_svg(), min_width=1024) is False


def test_image_size_ok_aspect_ratio_rejected():
    img = make_test_image(2000, 200)
    assert image_size_ok(img, min_width=1024) is False


# --- prepare_photo tests ---
def test_prepare_photo_resizes():
    large = make_test_image(2000, 2000)
    result = prepare_photo(large, max_width=1280)
    img = Image.open(io.BytesIO(result))
    assert img.width <= 1280
    assert img.format == "JPEG"


def test_prepare_photo_passes_through_on_error():
    bad = b"not an image"
    result = prepare_photo(bad)
    assert result == bad


# --- API моки ---
class MockHTTPXResponse:
    def __init__(self, json_data=None, content=None, status_code=200, headers=None):
        self._json = json_data or {}
        self.content = content or b""
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=MagicMock(), response=self
            )

    def json(self):
        return self._json

    @property
    def text(self):
        return self.content.decode("utf-8", errors="replace")

    async def aiter_bytes(self, chunk_size: int = 8192):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]


@asynccontextmanager
async def mock_stream_context(response: MockHTTPXResponse):
    yield response


class MockAsyncClient:
    def __init__(self, responses: dict):
        self.responses = responses

    def _match(self, url: str):
        for pattern, resp in self.responses.items():
            if pattern in url:
                return resp
        return None

    async def get(self, url, headers=None, timeout=None, follow_redirects=True, params=None, **kwargs):
        resp = self._match(url)
        if resp is None:
            return MockHTTPXResponse(status_code=404)
        return resp

    @asynccontextmanager
    async def stream(self, method, url, headers=None, timeout=None, follow_redirects=True, **kwargs):
        resp = self._match(url)
        if resp is None:
            resp = MockHTTPXResponse(status_code=404)
        async with mock_stream_context(resp) as r:
            yield r


# --- pixabay_image ---
@pytest.mark.asyncio
async def test_pixabay_image_success():
    img = make_test_image()
    client = MockAsyncClient({
        "pixabay.com/api": MockHTTPXResponse(
            json_data={"hits": [{"largeImageURL": "https://pixabay.com/photo.jpg"}]},
        ),
        "pixabay.com/photo.jpg": MockHTTPXResponse(content=img),
    })
    result = await pixabay_image(client, "Cyberpunk 2077", "test_key", 1024, 10.0)
    assert result is not None
    assert len(result) > 0


@pytest.mark.asyncio
async def test_pixabay_image_empty_hits():
    client = MockAsyncClient({
        "pixabay.com/api": MockHTTPXResponse(json_data={"hits": []}),
    })
    result = await pixabay_image(client, "UnknownGame", "key", 1024, 10.0)
    assert result is None


@pytest.mark.asyncio
async def test_pixabay_image_download_fails():
    client = MockAsyncClient({
        "pixabay.com/api": MockHTTPXResponse(json_data={"hits": [{"imageURL": "https://pixabay.com/img.jpg"}]}),
        "pixabay.com/img.jpg": MockHTTPXResponse(status_code=404),
    })
    result = await pixabay_image(client, "Game", "key", 1024, 10.0)
    assert result is None


# --- steam_image ---
@pytest.mark.asyncio
async def test_steam_image_success():
    img = make_test_image()
    client = MockAsyncClient({
        "store.steampowered.com/api/storesearch": MockHTTPXResponse(
            json_data={"items": [{"id": 12345, "name": "Cyberpunk 2077"}]},
        ),
        "cdn.akamai.steamstatic.com/steam/apps/12345/library_hero.jpg": MockHTTPXResponse(content=img),
    })
    result = await steam_image(client, "Cyberpunk 2077", 1024, 10.0)
    assert result is not None


@pytest.mark.asyncio
async def test_steam_image_no_match():
    client = MockAsyncClient({
        "store.steampowered.com/api/storesearch": MockHTTPXResponse(
            json_data={"items": [{"id": 999, "name": "Completely Different Game"}]},
        ),
    })
    result = await steam_image(client, "Cyberpunk 2077", 1024, 10.0)
    assert result is None


@pytest.mark.asyncio
async def test_steam_image_empty_items():
    client = MockAsyncClient({
        "store.steampowered.com/api/storesearch": MockHTTPXResponse(json_data={"items": []}),
    })
    result = await steam_image(client, "Game", 1024, 10.0)
    assert result is None


# --- wikipedia_image ---
@pytest.mark.asyncio
async def test_wikipedia_image_success():
    img = make_test_image()
    client = MockAsyncClient({
        "ru.wikipedia.org/w/api.php": MockHTTPXResponse(
            json_data={
                "query": {
                    "pages": {
                        "123": {
                            "original": {"source": "https://upload.wikimedia.org/img.jpg"}
                        }
                    }
                }
            }
        ),
        "upload.wikimedia.org/img.jpg": MockHTTPXResponse(content=img),
    })
    result = await wikipedia_image(client, "Cyberpunk 2077", 1024, 10.0)
    assert result is not None


@pytest.mark.asyncio
async def test_wikipedia_image_no_pages():
    client = MockAsyncClient({
        "ru.wikipedia.org/w/api.php": MockHTTPXResponse(
            json_data={"query": {"pages": {}}}
        ),
    })
    result = await wikipedia_image(client, "Game", 1024, 10.0)
    assert result is None


# --- playground_image ---
@pytest.mark.asyncio
async def test_playground_image_success():
    html = '''
    <html>
    <meta property="og:title" content="Cyberpunk 2077" />
    <meta property="og:image" content="https://playground.ru/image.jpg?600-600" />
    </html>
    '''
    img = make_test_image()
    client = MockAsyncClient({
        "playground.ru/cyberpunk_2077": MockHTTPXResponse(content=html.encode()),
        "playground.ru/image.jpg": MockHTTPXResponse(content=img),
    })
    result = await playground_image(client, "Cyberpunk 2077", 1024, 10.0)
    assert result is not None


@pytest.mark.asyncio
async def test_playground_image_title_mismatch():
    html = '''
    <html>
    <meta property="og:title" content="Another Game" />
    <meta property="og:image" content="https://playground.ru/image.jpg" />
    </html>
    '''
    client = MockAsyncClient({
        "playground.ru/cyberpunk_2077": MockHTTPXResponse(content=html.encode()),
    })
    result = await playground_image(client, "Cyberpunk 2077", 1024, 10.0)
    assert result is None


@pytest.mark.asyncio
async def test_playground_image_404():
    client = MockAsyncClient({
        "playground.ru/cyberpunk_2077": MockHTTPXResponse(status_code=404),
    })
    result = await playground_image(client, "Cyberpunk 2077", 1024, 10.0)
    assert result is None


# --- steamgriddb_image ---
@pytest.mark.asyncio
async def test_steamgriddb_image_success():
    img = make_test_image(1200, 900)  # larger to pass 30KB check
    client = MockAsyncClient({
        "steamgriddb.com/api/v2/search/autocomplete": MockHTTPXResponse(
            json_data={"data": [{"id": 42}]},
        ),
        "steamgriddb.com/api/v2/grids/game/42": MockHTTPXResponse(
            json_data={"data": [{"url": "https://steamgriddb.com/grid.jpg"}]},
        ),
        "steamgriddb.com/grid.jpg": MockHTTPXResponse(content=img),
    })
    result = await steamgriddb_image(client, "Cyberpunk 2077", "sgdb_key", 1024, 10.0)
    assert result is not None


@pytest.mark.asyncio
async def test_steamgriddb_image_no_grids():
    client = MockAsyncClient({
        "steamgriddb.com/api/v2/search/autocomplete": MockHTTPXResponse(
            json_data={"data": [{"id": 42}]},
        ),
        "steamgriddb.com/api/v2/grids/game/42": MockHTTPXResponse(json_data={"data": []}),
    })
    result = await steamgriddb_image(client, "Game", "key", 1024, 10.0)
    assert result is None


# --- clean_image fallback chain ---
@pytest.mark.asyncio
async def test_clean_image_fallback_chain():
    """Pixabay -> Steam -> Playground -> Wiki -> SteamGridDB."""
    img = make_test_image()

    # Steam succeeds (Pixabay not called since no key)
    client = MockAsyncClient({
        "store.steampowered.com/api/storesearch": MockHTTPXResponse(
            json_data={"items": [{"id": 12345, "name": "Cyberpunk 2077"}]},
        ),
        "cdn.akamai.steamstatic.com/steam/apps/12345/library_hero.jpg": MockHTTPXResponse(content=img),
    })
    result = await clean_image(client, "Cyberpunk 2077", None, 1024, 10.0)
    assert result is not None

    # All fail -> None
    client = MockAsyncClient({})
    result = await clean_image(client, "Unknown", None, 1024, 10.0)
    assert result is None


# --- download_video ---
@pytest.mark.asyncio
async def test_download_video_success():
    video_data = b"\x00\x00\x00\x20ftypmp42" + b"x" * 1000
    client = MockAsyncClient({
        "example.com/video.mp4": MockHTTPXResponse(
            content=video_data,
            headers={"content-length": str(len(video_data))},
        ),
    })
    result = await download_video(client, "https://example.com/video.mp4", 10 * 1024 * 1024, 10.0)
    assert result is not None
    assert len(result) == len(video_data)


@pytest.mark.asyncio
async def test_download_video_too_large():
    video_data = b"\x00\x00\x00\x20ftypmp42" + b"x" * (20 * 1024 * 1024)
    client = MockAsyncClient({
        "example.com/large.mp4": MockHTTPXResponse(
            content=video_data,
            headers={"content-length": str(len(video_data))},
        ),
    })
    result = await download_video(client, "https://example.com/large.mp4", 10 * 1024 * 1024, 10.0)
    assert result is None


@pytest.mark.asyncio
async def test_download_video_not_mp4():
    video_data = b"not a video file"
    client = MockAsyncClient({
        "example.com/fake.mp4": MockHTTPXResponse(content=video_data),
    })
    result = await download_video(client, "https://example.com/fake.mp4", 10 * 1024 * 1024, 10.0)
    assert result is None


if __name__ == "__main__":
    asyncio.run(pytest.main([__file__, "-v"]))