import io
import logging
import re
import urllib.parse

import httpx
from PIL import Image

log = logging.getLogger("images")

API_HEADERS = {"User-Agent": "GameNewsBot/1.0 (https://t.me/yourchannel)"}

_LOWER_CONNECTORS = {
    "of", "the", "and", "a", "an", "for", "in", "on", "with", "vs", "and",
    "iv", "ii", "iii", "vi", "v", "ix", "x", "2", "3", "4", "5", "6",
    "3d", "hd", "remake", "remastered", "edition",
}


def extract_game_name(title: str) -> str | None:
    """Эвристика: ищет в заголовке последовательность слов-названий игры.

    Пример: «Глава разработки The Blood of Dawnwalker сравнил бои в игре
    с Guitar Hero» -> «The Blood of Dawnwalker».
    """
    tokens = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", title)
    best, i = "", 0
    while i < len(tokens):
        if tokens[i][0].isupper():
            run = [tokens[i]]
            i += 1
            while i < len(tokens):
                t = tokens[i]
                is_cap = t[0].isupper()
                is_connector = t.lower() in _LOWER_CONNECTORS and i + 1 < len(tokens)
                if not (is_cap or is_connector):
                    break
                run.append(t)
                i += 1
            if len(run) >= 2 and len(" ".join(run)) > len(best):
                best = " ".join(run)
        else:
            i += 1
    return best or None


def _is_raster(data: bytes) -> bool:
    return not data[:4] == b"<svg" and b"image/svg" not in data[:200].lower()


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9а-яё]+", "", s.lower())


def _names_match(requested: str, found: str) -> bool:
    """Совпадают ли названия (по вхождению, без учёта регистра/пунктуации)."""
    a, b = _norm(requested), _norm(found)
    if not a or not b:
        return False
    return a in b or b in a


def image_size_ok(data: bytes, min_width: int) -> bool:
    """Проверяет, что данные — растровое изображение шириной >= min_width
    и достаточного качества (большая картинка не может весить подозрительно мало)."""
    if len(data) < 30_000:
        log.info("Картинка слишком лёгкая (%d байт) — похоже на размытый плейсхолдер", len(data))
        return False
    try:
        img = Image.open(io.BytesIO(data))
        width = img.width
        height = img.height
        img.close()
    except Exception:
        return False
    if width < min_width:
        log.info("Картинка слишком маленькая (%dpx < %dpx), пропускаем", width, min_width)
        return False
    # Проверка на подозрительные соотношения сторон (водяные знаки часто в узких полосах)
    aspect = width / height
    if aspect > 5 or aspect < 0.2:
        log.info("Подозрительное соотношение сторон %.2f (%dx%d) — возможен баннер/водяной знак", aspect, width, height)
        return False
    return True


def prepare_photo(data: bytes, max_width: int = 1280, quality: int = 88) -> bytes:
    """Ужимает фото под Telegram: не шире max_width, перекодирование в JPEG.

    Telegram сам уменьшает фото до 1280px и пережимает, поэтому качество
    на канале не теряется, зато файл гарантированно укладывается в 10 МБ.
    """
    try:
        img = Image.open(io.BytesIO(data))
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, max(1, int(img.height * ratio))),
                             getattr(Image, "Resampling", Image).LANCZOS)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except Exception:
        log.warning("Не удалось ужать фото, отправляю как есть (%d байт)", len(data))
        return data


async def _download(client: httpx.AsyncClient, url: str, timeout: float) -> bytes | None:
    try:
        resp = await client.get(url, headers=API_HEADERS, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        return resp.content
    except httpx.HTTPError as exc:
        log.info("Не удалось скачать изображение %s: %s", url, exc)
        return None


async def pick_candidate(
    client: httpx.AsyncClient, url: str, min_width: int, timeout: float
) -> bytes | None:
    """Скачивает и проверяет одну картинку-кандидата (для галереи)."""
    data = await _download(client, url, timeout)
    if data and _is_raster(data) and image_size_ok(data, min_width):
        return data
    return None


async def pick_from_article(
    client: httpx.AsyncClient, candidates: list[str], min_width: int, timeout: float
) -> bytes | None:
    """Берёт первую подходящую картинку из списка кандидатов (og:image, RSS, статья)."""
    for url in candidates:
        data = await _download(client, url, timeout)
        if data and _is_raster(data) and image_size_ok(data, min_width):
            log.info("Выбрано фото со страницы: %s", url)
            return data
    return None


_STEAM_CDN = "cdn.akamai.steamstatic.com"


def _steam_cdn_url(app_id: int, kind: str) -> str:
    """URL картинки Steam на рабочем CDN (cdn.akamai.steamstatic.com)."""
    return f"https://{_STEAM_CDN}/steam/apps/{app_id}/{kind}"


async def steam_image(
    client: httpx.AsyncClient, game_name: str, min_width: int, timeout: float
) -> bytes | None:
    """Поиск обложки игры в Steam (API без ключа)."""
    try:
        resp = await client.get(
            "https://store.steampowered.com/api/storesearch/",
            params={"term": game_name, "l": "ru", "cc": "ru"},
            headers=API_HEADERS,
            timeout=timeout,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if not items:
            return None
        app_id = None
        for it in items[:5]:
            if _names_match(game_name, it.get("name", "")):
                app_id = it["id"]
                break
        if app_id is None:
            log.info("В Steam нет игры, совпадающей с «%s» — пропускаем Steam", game_name)
            return None
        # Картинки качаем с cdn.akamai.steamstatic.com: основной CDN
        # (shared.akamai.steamstatic.com) недоступен из РФ.
        candidates = [
            _steam_cdn_url(app_id, "library_hero.jpg"),
            _steam_cdn_url(app_id, "header.jpg"),
            _steam_cdn_url(app_id, "library_600x900.jpg"),
        ]
        for image_url in candidates:
            img = await _download(client, image_url, timeout)
            if img and image_size_ok(img, min_width):
                log.info("Фото из Steam для «%s»", game_name)
                return img
    except (httpx.HTTPError, ValueError) as exc:
        log.info("Steam-поиск для «%s» не сработал: %s", game_name, exc)
    return None


async def wikipedia_image(
    client: httpx.AsyncClient, game_name: str, min_width: int, timeout: float
) -> bytes | None:
    """Поиск картинки в Википедии (API без ключа)."""
    try:
        resp = await client.get(
            "https://ru.wikipedia.org/w/api.php",
            params={
                "action": "query", "generator": "search",
                "gsrsearch": game_name, "gsrnamespace": 0, "gsrlimit": 3,
                "prop": "pageimages", "piprop": "original|thumbnail",
                "pithumbsize": 800, "format": "json",
            },
            headers=API_HEADERS,
            timeout=timeout,
        )
        resp.raise_for_status()
        pages = resp.json().get("query", {}).get("pages", {})
        for page in pages.values():
            source = (page.get("original") or {}).get("source") or (page.get("thumbnail") or {}).get("source")
            if not source:
                continue
            img = await _download(client, source, timeout)
            if img and _is_raster(img) and image_size_ok(img, min_width):
                log.info("Фото из Википедии для «%s»", game_name)
                return img
    except (httpx.HTTPError, ValueError) as exc:
        log.info("Вики-поиск для «%s» не сработал: %s", game_name, exc)
    return None


_ROMAN_TO_DIGIT = [
    ("viii", "8"), ("vii", "7"), ("iii", "3"), ("vi", "6"),
    ("iv", "4"), ("ii", "2"), ("ix", "9"), ("v", "5"), ("i", "1"),
]


def _playground_slug(game_name: str) -> str:
    """Slug для playground.ru: «Mortal Shell II» -> mortal_shell_2."""
    s = game_name.lower()
    for roman, digit in _ROMAN_TO_DIGIT:
        s = re.sub(rf"\b{roman}\b", digit, s)
    s = re.sub(r"[^a-zа-яё0-9]+", "_", s)
    return s.strip("_")


async def playground_image(
    client: httpx.AsyncClient, game_name: str, min_width: int, timeout: float
) -> bytes | None:
    """Обложка игры с playground.ru (без ключа).

    Страница игры: https://playground.ru/{slug} — в og:image лежит чистая
    обложка (без водяных знаков). Проверяем, что og:title совпадает
    с искомой игрой, чтобы не взять чужую карточку.
    """
    slug = _playground_slug(game_name)
    if not slug:
        return None
    try:
        resp = await client.get(
            f"https://playground.ru/{slug}",
            headers=API_HEADERS,
            timeout=timeout,
            follow_redirects=True,
        )
        resp.raise_for_status()
        title = re.search(r'property="og:title" content="([^"]+)"', resp.text)
        if not title or not _names_match(game_name, title.group(1)):
            log.info("Playground: карточка %r не совпадает с «%s» — пропускаем",
                     title.group(1) if title else slug, game_name)
            return None
        m = re.search(r'property="og:image" content="([^"]+)"', resp.text)
        if not m:
            return None
        url = urllib.parse.urlsplit(m.group(1))
        # og:image приходит с суффиксом ?600-600 — убираем, чтобы получить
        # картинку в полном разрешении.
        full_url = urllib.parse.urlunsplit((url.scheme, url.netloc, url.path, "", ""))
        img = await _download(client, full_url, timeout)
        if img and _is_raster(img) and image_size_ok(img, min_width):
            log.info("Фото с playground.ru для «%s»", game_name)
            return img
    except (httpx.HTTPError, ValueError) as exc:
        log.info("Playground для «%s» не сработал: %s", game_name, exc)
    return None


async def pixabay_image(
    client: httpx.AsyncClient, game_name: str, api_key: str, min_width: int, timeout: float
) -> bytes | None:
    """Поиск картинки через Pixabay (нужен бесплатный API-ключ)."""
    try:
        resp = await client.get(
            "https://pixabay.com/api/",
            params={
                "key": api_key, "q": game_name, "image_type": "photo",
                "min_width": min_width, "safesearch": "true",
            },
            headers=API_HEADERS,
            timeout=timeout,
        )
        resp.raise_for_status()
        hits = resp.json().get("hits", [])
        if not hits:
            return None
        best = hits[0]
        url = best.get("imageURL") or best.get("largeImageURL") or best.get("webformatURL")
        img = await _download(client, url, timeout)
        if img and image_size_ok(img, min_width):
            log.info("Фото из Pixabay для «%s»", game_name)
            return img
    except (httpx.HTTPError, ValueError) as exc:
        log.info("Pixabay-поиск для «%s» не сработал: %s", game_name, exc)
    return None


async def steamgriddb_image(
    client: httpx.AsyncClient, game_name: str, api_key: str, min_width: int, timeout: float
) -> bytes | None:
    """Официальная обложка игры с SteamGridDB (нужен бесплатный API-ключ).

    Обложки 600x900 — меньше нашего минимума в 1024px, поэтому проверяем
    отдельным порогом и ставим источник последним среди «чистых».
    """
    headers = {**API_HEADERS, "Authorization": f"Bearer {api_key}"}
    try:
        resp = await client.get(
            f"https://www.steamgriddb.com/api/v2/search/autocomplete/{urllib.parse.quote(game_name)}",
            headers=headers,
            timeout=timeout,
        )
        resp.raise_for_status()
        games = resp.json().get("data", [])
        if not games:
            return None
        game_id = games[0]["id"]
        resp = await client.get(
            f"https://www.steamgriddb.com/api/v2/grids/game/{game_id}",
            params={"style": "official", "type": "static"},
            headers=headers,
            timeout=timeout,
        )
        resp.raise_for_status()
        grids = resp.json().get("data", [])
        if not grids:
            return None
        for grid in grids:
            url = grid.get("url")
            if not url:
                continue
            img = await _download(client, url, timeout)
            if img and image_size_ok(img, min(600, min_width)):
                log.info("Обложка со SteamGridDB для «%s»", game_name)
                return img
    except (httpx.HTTPError, ValueError) as exc:
        log.info("SteamGridDB для «%s» не сработал: %s", game_name, exc)
    return None


async def download_video(
    client: httpx.AsyncClient, url: str, max_bytes: int, timeout: float
) -> bytes | None:
    """Скачивает видео с проверкой размера по ходу (mp4/webm — по сигнатуре)."""
    try:
        async with client.stream(
            "GET", url, headers=API_HEADERS, timeout=timeout, follow_redirects=True
        ) as resp:
            resp.raise_for_status()
            content_length = int(resp.headers.get("content-length") or 0)
            if content_length > max_bytes:
                log.info("Видео слишком большое (%d МБ > %d МБ), пропускаем",
                         content_length // (1024 * 1024), max_bytes // (1024 * 1024))
                return None
            chunks = []
            size = 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    log.info("Видео превысило лимит %d МБ, пропускаем", max_bytes // (1024 * 1024))
                    return None
                chunks.append(chunk)
            data = b"".join(chunks)
    except httpx.HTTPError as exc:
        log.info("Не удалось скачать видео %s: %s", url, exc)
        return None
    if len(data) < 16 or not (
        data[4:8] == b"ftyp" or data[:4] == b"\x1a\x45\xdf\xa3"
    ):
        log.info("Скачанный файл %s не похож на mp4/webm, пропускаем", url)
        return None
    return data


async def clean_image(
    client: httpx.AsyncClient,
    game_name: str,
    pixabay_api_key: str | None,
    min_width: int,
    timeout: float,
    steamgriddb_api_key: str | None = None,
) -> bytes | None:
    """Поиск чистого фото: Pixabay -> Steam -> Playground -> Википедия -> SteamGridDB."""
    if pixabay_api_key:
        img = await pixabay_image(client, game_name, pixabay_api_key, min_width, timeout)
        if img:
            log.info("Чистое фото найдено через Pixabay для «%s»", game_name)
            return img

    img = await steam_image(client, game_name, min_width, timeout)
    if img:
        log.info("Чистое фото найдено через Steam для «%s»", game_name)
        return img

    img = await playground_image(client, game_name, min_width, timeout)
    if img:
        log.info("Чистое фото найдено через Playground для «%s»", game_name)
        return img

    img = await wikipedia_image(client, game_name, min_width, timeout)
    if img:
        log.info("Чистое фото найдено через Википедию для «%s»", game_name)
        return img

    if steamgriddb_api_key:
        img = await steamgriddb_image(
            client, game_name, steamgriddb_api_key, min_width, timeout
        )
        if img:
            log.info("Чистое фото найдено через SteamGridDB для «%s»", game_name)
            return img
    return None