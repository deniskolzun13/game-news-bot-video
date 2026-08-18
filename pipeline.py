import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

import httpx

from article import fetch_article
from config import Config
from images import (
    clean_image,
    extract_game_name,
    prepare_photo,
)
from llm_ollama import OllamaClient, OllamaError
from notifier import Notifier
from parsers import build_parsers
from parsers.base import NewsItem
from publisher import TelegramPublisher
from storage import Storage, normalize_title

log = logging.getLogger("pipeline")

HIGH_PRIORITY_WORDS = (
    "анонс", "анонсировали", "дата релиза", "выйдет", "релиз", "трейлер",
    "обновлен", "патч", "дополнение", "dlc", "скидк", "бесплатн",
)
LOW_PRIORITY_WORDS = ("слух", "инсайд", "мем", "коспле", "опрос", "мод")


def _ensure_hashtag(text: str, title: str, game: str | None = None) -> str:
    """Гарантирует хэштег в конце поста, если LLM его не добавила."""
    stripped = text.strip()
    if stripped:
        last_line = stripped.splitlines()[-1]
        if "#" in last_line:
            return text
    if not game:
        game = extract_game_name(title)
    if game:
        tag = re.sub(r"[^A-Za-zА-Яа-яЁё0-9_]", "", game.replace(" ", ""))
        if not tag:
            return f"{text}\n#Игры"
        log.info("LLM не добавила хэштег, ставим #%s из названия игры", tag)
        return f"{text}\n#{tag}"
    log.info("LLM не добавила хэштег и название игры не найдено — ставим #Игры")
    return f"{text}\n#Игры"


def _add_channel_link(text: str, channel_id: str) -> str:
    """Вставляет упоминание канала (@имя) перед финальным хэштегом."""
    if not channel_id.startswith("@"):
        return text
    stripped = text.rstrip()
    lines = stripped.splitlines()
    if lines and lines[-1].lstrip().startswith("#"):
        return "\n".join(lines[:-1]) + f"\n\n{channel_id}\n{lines[-1]}"
    return f"{stripped}\n\n{channel_id}"


def _format_post(text: str) -> str:
    """Оформление поста для Telegram: жирный заголовок, экранирование HTML.

    Первая непустая строка — заголовок, выделяется <b>. Всё остальное
    экранируется, чтобы LLM-текст не сломал разметку.
    """
    lines = [escape(line) for line in text.rstrip().splitlines()]
    for i, line in enumerate(lines):
        if line.strip():
            lines[i] = f"<b>{line.strip()}</b>"
            break
    return "\n".join(lines)


def _news_priority(item: NewsItem) -> int:
    """Оценка важности: первыми обрабатываем новости с конкретным событием."""
    title = item.title.lower()
    score = sum(2 for word in HIGH_PRIORITY_WORDS if word in title)
    score -= sum(2 for word in LOW_PRIORITY_WORDS if word in title)
    if item.published_at:
        age_hours = (datetime.now(timezone.utc) - item.published_at).total_seconds() / 3600
        if age_hours <= 3:
            score += 2
        elif age_hours <= 12:
            score += 1
    return score


def _post_is_publishable(text: str) -> bool:
    """Минимальный автоматический контроль перед отправкой в Telegram."""
    lowered = text.lower()
    if len(text.strip()) < 40 or "http://" in lowered or "https://" in lowered:
        return False
    return len([line for line in text.splitlines() if line.strip()]) >= 2


@dataclass
class Post:
    """Готовый к публикации пост: текст, фото, видео, игра из заголовка."""
    text: str
    photos: list[bytes]
    video: bytes | None = None
    game: str | None = None


class NewsPipeline:
    def __init__(self, cfg: Config, notifier: Notifier | None = None):
        self._cfg = cfg
        self._notifier = notifier
        self._storage = Storage(cfg.db_path)
        self._http = httpx.AsyncClient(timeout=cfg.http_timeout)
        self._ollama = OllamaClient(
            cfg.ollama_base_url, cfg.ollama_model, cfg.ollama_fallback_model,
            concurrency=cfg.ollama_concurrency,
        )
        self._publisher = (
            None
            if cfg.dry_run
            else TelegramPublisher(cfg.telegram_token, cfg.channel_id, cfg.max_caption_length)
        )

    def _random_delay(self) -> timedelta:
        minutes = random.randint(
            self._cfg.min_post_delay_minutes, self._cfg.max_post_delay_minutes
        )
        return timedelta(minutes=minutes)

    def _next_slot(self, after: datetime) -> datetime:
        """Ближайшее окно публикации: каждый час из peak_hours (8:00–21:00 МСК),
        дополнительно в :30 — до двух постов в час, если новостей хватает.
        Слоты заполняются по порядку: пустые просто не используются."""
        local = after.astimezone(ZoneInfo(self._cfg.timezone))
        floor = local + self._random_delay()
        for day in range(4):
            for hour in sorted(self._cfg.peak_hours):
                for minute in (0, 30):
                    cand = (local + timedelta(days=day)).replace(
                        hour=hour, minute=minute, second=0, microsecond=0
                    )
                    if cand > floor:
                        return cand.astimezone(timezone.utc)
        raise RuntimeError("Не нашлось подходящего слота публикации")

    def _now_local(self) -> datetime:
        return datetime.now(ZoneInfo(self._cfg.timezone))

    def _is_fresh(self, row: dict) -> bool:
        """Новость не старше news_freshness_hours к моменту публикации."""
        if not row.get("created_at"):
            return True
        try:
            created = datetime.fromisoformat(row["created_at"])
        except ValueError:
            return True
        return datetime.now(timezone.utc) - created < timedelta(hours=self._cfg.news_freshness_hours)

    def _in_quiet_hours(self) -> bool:
        """Сейчас тихие часы (по умолчанию 22:00–8:00 по МСК)."""
        hour = self._now_local().hour
        start, end = self._cfg.quiet_start_hour, self._cfg.quiet_end_hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def _next_morning_utc(self) -> datetime:
        """Ближайшее утро (час quiet_end_hour) в UTC."""
        morning = self._now_local().replace(
            hour=self._cfg.quiet_end_hour, minute=0, second=0, microsecond=0
        )
        if morning <= self._now_local():
            morning += timedelta(days=1)
        return morning.astimezone(timezone.utc)

    async def process_all(self) -> dict[str, tuple[int, int, int]]:
        """Собирает новости и ставит их в очередь с рандомной задержкой 1–3 часа.

        Возвращает {источник: (найдено, запланировано, ошибок)}.
        """
        model = await self._ollama.check()
        parsers = build_parsers(self._http, self._cfg.enabled_sources)
        results: dict[str, tuple[int, int, int]] = {}
        now = datetime.now(timezone.utc)

        for parser in parsers:
            found = queued = errors = 0
            try:
                items = await parser.fetch_items(max_age_hours=self._cfg.max_item_age_hours)
                items.sort(key=_news_priority, reverse=True)
                found = len(items)
                # Слоты считаем последовательно: каждый следующий пост —
                # после самого позднего уже запланированного/опубликованного.
                slot = self._storage.latest_publish_time(
                    self._cfg.news_freshness_hours
                ) or now
                tasks = []
                for item in items[: self._cfg.max_items_per_source]:
                    slot = self._next_slot(max(slot, now))
                    tasks.append((item, slot))

                # Семафор только для сетевой работы (статьи/фото/видео).
                # Запросы к Ollama ограничены отдельно: OLLAMA_CONCURRENCY
                # (локальная LLM почти последовательна, см. OllamaClient).
                sem = asyncio.Semaphore(3)

                async def _handle(item, slot):
                    async with sem:
                        try:
                            return 1 if await self._process_item(item, model, slot) else 0
                        except Exception:
                            log.exception("Ошибка обработки новости %s (%s)", item.url, parser.name)
                            return -1
                        finally:
                            await asyncio.sleep(1)

                res = await asyncio.gather(*(_handle(it, sl) for it, sl in tasks))
                queued = sum(1 for r in res if r == 1)
                errors = sum(1 for r in res if r < 0)
            except Exception:
                log.exception("Сбой источника %s — пропускаем его, продолжаем дальше", parser.name)
                errors += 1
            results[parser.name] = (found, queued, errors)
            if errors and self._notifier:
                await self._notifier.notify(
                    "source", f"⚠ Источник {parser.name}: {errors} ошибок при сборе новостей."
                )
        return results

    async def _build_post(self, item: NewsItem, model: str) -> Post | None:
        """Собирает готовый пост из новости: статья → игра → дубликат → LLM →
        контроль качества → фото/видео → хэштег → упоминание канала → формат.

        Общая цепочка для плановой (_process_item) и немедленной
        (publish_one_now) публикации. Возвращает None, если новость не прошла
        хотя бы один этап (тогда она не публикуется вообще).
        """
        article = await fetch_article(
            self._http, item.url, item.source, self._cfg.http_timeout
        )
        article_text = article.text if article else ""
        if not article_text:
            article_text = item.description
        if not article_text:
            log.warning("Нет текста статьи, пропускаем: %s", item.url)
            return None

        game = await self._game_name(model, item.title)
        if await self._is_duplicate(item, model, game):
            return None
        if game and await self._is_recent_game(game):
            log.info("Игра %r недавно уже упоминалась — пропускаем: %s", game, item.url)
            return None

        text = await self._ollama.rewrite(model, item.title, article_text)
        try:
            text = await self._ollama.proofread(model, text)
        except OllamaError:
            log.warning("Проверка орфографии не сработала — публикуем пост как есть")
        if not _post_is_publishable(text):
            log.warning("Пост не прошёл контроль качества, пропускаем: %s", item.url)
            return None

        # Канал публикует только посты с чистой фотографией: картинки из
        # статьи не используем, чтобы не допустить водяные знаки. Видео —
        # только при ENABLE_VIDEO_POSTS=true (по умолчанию выключено).
        video = None
        if self._cfg.enable_video_posts:
            video = await self._pick_video(article)
        photos = await self._pick_photos(item, article, game)
        if not photos:
            log.info("Нет чистого фото — новость не ставим в очередь: %s", item.url)
            return None

        text = _ensure_hashtag(text, item.title, game)
        text = _add_channel_link(text, self._cfg.channel_id)
        text = _format_post(text)
        return Post(text=text, photos=photos, video=video, game=game)

    async def _process_item(self, item: NewsItem, model: str, publish_at: datetime) -> bool:
        if self._storage.is_published(item.url) or self._storage.is_queued(item.url):
            log.debug("Уже публиковалось или в очереди: %s", item.url)
            return False

        log.info("Новость: %s", item.title)
        post = await self._build_post(item, model)
        if post is None:
            return False

        if self._publisher is None:
            log.info("[dry-run] Был бы запланирован пост на %s UTC (%d символов, фото: %d, видео: %s)",
                     publish_at.strftime("%d.%m %H:%M"), len(post.text),
                     len(post.photos), "есть" if post.video else "нет")
            log.info("[dry-run] Текст:\n%s", post.text)
            self._storage.mark_published(item.url, item.source, item.title)
        else:
            inserted = self._storage.enqueue(
                item.url, item.source, item.title, post.text,
                post.photos[0] if post.photos else None, publish_at, post.video,
                extra_photos=self._extra_photos(post.photos),
                created_at=item.published_at or datetime.now(timezone.utc),
            )
            if not inserted:
                log.info("Новость уже добавлена параллельной задачей: %s", item.url)
                return False
            self._storage.trim_queue(self._cfg.queue_capacity, keep_url=item.url)
            log.info("Запланирован пост на %s UTC: %s",
                     publish_at.strftime("%d.%m %H:%M"), item.url)
        return True

    async def publish_one_now(self) -> bool:
        """Обрабатывает самую свежую новость из лент и ставит её в очередь
        на немедленную публикацию."""
        model = await self._ollama.check()
        parsers = build_parsers(self._http, self._cfg.enabled_sources)
        for parser in parsers:
            try:
                items = await parser.fetch_items(max_age_hours=self._cfg.max_item_age_hours)
                items.sort(key=_news_priority, reverse=True)
            except Exception:
                log.exception("Сбой источника %s — пробуем следующий", parser.name)
                continue
            for item in items[:10]:
                if self._storage.is_published(item.url) or self._storage.is_queued(item.url):
                    continue
                try:
                    post = await self._build_post(item, model)
                    if post is None:
                        continue
                    if self._publisher is None:
                        log.info("[dry-run] Немедленная публикация:\n%s", post.text)
                    else:
                        inserted = self._storage.enqueue(
                            item.url, item.source, item.title, post.text,
                            post.photos[0] if post.photos else None,
                            datetime.now(timezone.utc), post.video,
                            extra_photos=self._extra_photos(post.photos),
                            created_at=item.published_at or datetime.now(timezone.utc),
                        )
                        if not inserted:
                            continue
                        self._storage.trim_queue(self._cfg.queue_capacity, keep_url=item.url)
                        log.info("Поставлено на немедленную публикацию: %s", item.url)
                    return True
                except Exception:
                    log.exception("Ошибка обработки новости %s", item.url)
                    if self._notifier:
                        await self._notifier.notify(
                            "now", f"Не удалось обработать новость: {item.title[:80]}"
                        )
                    continue
        return False

    async def publish_due(self, force: bool = False) -> int:
        """Публикует все посты из очереди, которым наступило время.

        В тихие часы (по умолчанию 22:00–8:00 МСК) автоматическая публикация
        приостанавливается, а просроченные посты переносятся на утро с
        разбросом по минутам. force=True (ручные --now и /publish_now)
        публикует сразу.
        """
        if self._publisher is None:
            return 0
        now = datetime.now(timezone.utc)
        if not force and self._in_quiet_hours():
            due = self._storage.due_items(now)
            if due:
                morning = self._next_morning_utc()
                for i, row in enumerate(due):
                    self._storage.reschedule(
                        row["id"], morning + timedelta(minutes=5 * i)
                    )
                log.info(
                    "Тихие часы (%02d:00–%02d:00): %d пост(ов) перенесены на утро (%s)",
                    self._cfg.quiet_start_hour, self._cfg.quiet_end_hour,
                    len(due), morning.astimezone(ZoneInfo(self._cfg.timezone)).strftime("%d.%m %H:%M"),
                )
            else:
                log.info("Тихие часы: очередь пуста")
            return 0
        due = self._storage.due_items(now)
        if (
            not force
            and self._cfg.approve_posts
            and self._notifier is not None
            and self._notifier.has_owner
        ):
            for row in due:
                if row["status"] == "awaiting":
                    continue
                if not self._is_fresh(row):
                    log.info("Новость устарела (старше %d ч) — удаляем пост #%d: %s",
                             self._cfg.news_freshness_hours, row["id"], row["url"])
                    self._storage.dequeue(row["id"])
                    continue
                try:
                    await self._notifier.send_for_approval(row)
                    self._storage.set_status(row["id"], "awaiting")
                    log.info("Пост #%d отправлен владельцу на утверждение", row["id"])
                except Exception:
                    log.exception("Не удалось отправить пост #%d на утверждение", row["id"])
                    if self._notifier:
                        await self._notifier.notify(
                            "publish", "Не удалось отправить пост на утверждение — проверьте журнал."
                        )
            return 0
        due = self._storage.due_items(now)
        published = 0
        for row in due:
            if row["status"] == "awaiting" and (self._cfg.approve_posts or force):
                continue
            fresh = self._is_fresh(row)
            if not fresh:
                log.info("Новость устарела (старше %d ч) — пропускаем пост #%d: %s",
                         self._cfg.news_freshness_hours, row["id"], row["url"])
                self._storage.dequeue(row["id"])
                continue
            try:
                ids = await self._publisher.publish(row["text"], row["photos"], row["video"])
                self._storage.record_messages(ids)
                self._storage.mark_published(row["url"], row["source"], row["title"])
                self._storage.dequeue(row["id"])
                published += 1
            except Exception:
                log.exception("Ошибка публикации из очереди (%s) — попробуем позже", row["url"])
                if self._notifier:
                    await self._notifier.notify(
                        "publish", "Ошибка публикации из очереди — пост застрял, проверяю журнал."
                    )
        if published:
            log.info("Опубликовано из очереди: %d постов", published)
        return published

    async def publish_approved(self, queue_id: int) -> bool:
        """Публикует пост после одобрения владельцем (кнопка «Опубликовать»)."""
        row = self._storage.get_item(queue_id)
        if row is None:
            return False
        if not self._is_fresh(row):
            log.info("Одобренный пост #%d устарел — не публикуем", queue_id)
            self._storage.dequeue(queue_id)
            return False
        try:
            ids = await self._publisher.publish(row["text"], row["photos"], row["video"])
            self._storage.record_messages(ids)
            self._storage.mark_published(row["url"], row["source"], row["title"])
            self._storage.dequeue(queue_id)
            return True
        except Exception:
            log.exception("Ошибка публикации одобренного поста #%d", queue_id)
            if self._notifier:
                await self._notifier.notify(
                    "publish", "Не удалось опубликовать одобренный пост — проверьте журнал."
                )
            return False

    def skip_approved(self, queue_id: int) -> bool:
        """Отклоняет пост (кнопка «Пропустить»): убирает из очереди и помечает
        как обработанный, чтобы новость не предлагалась снова."""
        row = self._storage.get_item(queue_id)
        if row is None:
            return False
        self._storage.mark_published(row["url"], row["source"], row["title"])
        self._storage.dequeue(queue_id)
        return True

    @staticmethod
    def _extra_photos(photos: list[bytes]) -> str | None:
        if len(photos) <= 1:
            return None
        return json.dumps([base64.b64encode(p).decode() for p in photos[1:]])

    async def _pick_video(self, article) -> bytes | None:
        """Видео для поста: mp4 из статьи -> скачивание YouTube через yt-dlp."""
        if article is None:
            return None
        max_bytes = self._cfg.max_video_size_mb * 1024 * 1024
        for url in article.video_urls:
            log.info("Пробуем скачать видео из статьи: %s", url)
            data = await download_video(self._http, url, max_bytes, self._cfg.http_timeout)
            if data:
                log.info("Видео из статьи скачано (%d МБ): %s",
                         len(data) // (1024 * 1024), url)
                return data
        if self._cfg.ytdlp_enabled and article.youtube_links:
            url = article.youtube_links[0]
            log.info("Качаем YouTube-видео через yt-dlp: %s", url)
            data = await self._download_youtube_video(url)
            if data:
                log.info("YouTube-видео скачано (%d МБ)", len(data) // (1024 * 1024))
                return data
        return None

    async def _download_youtube_video(self, url: str) -> bytes | None:
        """Скачивает видео с YouTube через yt-dlp (720p, лимит размера и времени)."""
        max_bytes = self._cfg.ytdlp_max_mb * 1024 * 1024
        fd, out_path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        try:
            cmd = [
                sys.executable, "-m", "yt_dlp",
                "--no-playlist",
                "--extractor-args", "youtube:player_client=android",
                "-f", (
                    f"bestvideo[height<={self._cfg.ytdlp_height}][ext=mp4]"
                    f"+bestaudio[ext=m4a]/18/best[height<={self._cfg.ytdlp_height}]/best"
                ),
                "--max-filesize", f"{self._cfg.ytdlp_max_mb}M",
                "--merge-output-format", "mp4",
                "-o", out_path,
                url,
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=self._cfg.ytdlp_timeout)
            except asyncio.TimeoutError:
                proc.kill()
                log.warning("yt-dlp не уложился в %d с для %s", self._cfg.ytdlp_timeout, url)
                return None
            if proc.returncode != 0:
                log.warning("yt-dlp не смог скачать %s (код %d)", url, proc.returncode)
                return None
            size = os.path.getsize(out_path)
            if size <= 16 or size > max_bytes:
                log.warning("yt-dlp: недопустимый размер %d байт для %s", size, url)
                return None
            with open(out_path, "rb") as f:
                data = f.read()
            if not (data[4:8] == b"ftyp" or data[:4] == b"\x1a\x45\xdf\xa3"):
                log.warning("yt-dlp вернул не mp4/webm для %s", url)
                return None
            return data
        except Exception:
            log.exception("Ошибка yt-dlp для %s", url)
            return None
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass

    async def _is_duplicate(self, item: NewsItem, model: str, game: str | None) -> bool:
        """Пропускает новость, если такая же уже опубликована или в очереди.

        Гейт: название игры встречается в чьём-то заголовке. Подтверждение:
        LLM решает, что это одна и та же новость (иначе два разных сюжета
        об одной игре не пересекались бы друг с другом).
        """
        norm_title = normalize_title(item.title)
        if len(norm_title) >= 10:
            other = self._storage.exact_norm_title(norm_title)
            if other is not None:
                log.info("Дубликат (тот же заголовок, что «%s»): %s — пропускаем",
                         other, item.title)
                return True
        if not game:
            return False
        sig = normalize_title(game)
        if not sig:
            return False
        hits = self._storage.titles_containing_game(sig)
        if not hits:
            return False
        for other in hits[:2]:
            try:
                if await self._ollama.is_same_news(model, item.title, other):
                    log.info("Дубликат (то же событие, что «%s»): %s — пропускаем",
                             other, item.title)
                    return True
            except Exception:
                log.warning("Не удалось проверить дубликат для %s — пропускаем проверку", item.title)
                return False
        return False

    async def _game_name(self, model: str, title: str) -> str | None:
        """Название игры: LLM, при сбое/пустоте — эвристика по заголовку."""
        try:
            name = await self._ollama.extract_game(model, title)
            if name:
                log.info("Название игры (LLM): %r", name)
                return name
        except Exception:
            log.warning("LLM не выделила игру для %r — используем эвристику", title)
        return extract_game_name(title)

    async def _pick_photos(self, item: NewsItem, article, game: str | None = None,
                           max_photos: int = 3) -> list[bytes]:
        """Фото для поста только из источников без водяных знаков.

        Картинки из статей не используем: игровые издания часто добавляют в них
        логотипы и водяные знаки. Если чистую картинку найти не удалось, пост
        остаётся текстовым.
        """
        photos: list[bytes] = []
        seen: set[bytes] = set()

        def add(data: bytes | None) -> None:
            if not data:
                return
            digest = hashlib.md5(data).digest()
            if digest in seen:
                return
            seen.add(digest)
            photos.append(prepare_photo(data))

        if game:
            log.info("Ищем чистое фото по названию игры: %r", game)
            img = await clean_image(
                self._http,
                game,
                self._cfg.pixabay_api_key,
                self._cfg.min_image_width,
                self._cfg.http_timeout,
                self._cfg.steamgriddb_api_key,
            )
            add(img)
            if photos:
                log.info("Чистое фото игры найдено")
        else:
            log.info("Название игры не найдено — пост будет без фото")

        if not photos:
            log.info("Чистое фото не найдено — публикуем текстовый пост без фото")
        return photos

    async def _is_recent_game(self, game: str) -> bool:
        """Одна и та же игра не чаще раза в GAME_REPEAT_HOURS часов."""
        sig = re.sub(r"[^a-zа-яё0-9]+", "", game.lower())
        if not sig:
            return False
        for title in self._storage.recent_titles(self._cfg.game_repeat_hours):
            if sig in re.sub(r"[^a-zа-яё0-9]+", "", title.lower()):
                return True
        return False

    def stats(self) -> dict:
        """Краткая статистика для команды /stats."""
        return self._storage.stats()

    def activity_stats(self) -> dict:
        return self._storage.activity_stats()

    def last_post(self) -> tuple[str, str] | None:
        """(заголовок, время) последнего опубликованного поста."""
        return self._storage.last_published()

    def last_messages(self) -> list[int]:
        """message_id последнего опубликованного поста."""
        return self._storage.last_messages()

    def drop_messages(self, message_ids: list[int]) -> None:
        self._storage.drop_messages(message_ids)

    def queue_items(self, limit: int = 5) -> list[dict]:
        return self._storage.queue_preview(limit)

    def postpone_queue_item(self, queue_id: int, minutes: int = 60) -> bool:
        row = self._storage.get_item(queue_id)
        if row is None:
            return False
        self._storage.reschedule(queue_id, datetime.now(timezone.utc) + timedelta(minutes=minutes))
        return True

    async def publish_text_post(self, text: str) -> bool:
        """Публикует присланный владельцем пост как есть."""
        if self._publisher is None:
            return False
        try:
            ids = await self._publisher.publish(_format_post(text), None, None)
            self._storage.record_messages(ids)
            return True
        except Exception:
            log.exception("Не удалось опубликовать пост владельца")
            return False

    def _get_review_window_posts(self, now: datetime | None = None) -> list[dict]:
        """Возвращает посты в окне ревью (review_window_hours часов вперёд)."""
        if now is None:
            now = datetime.now(timezone.utc)
        window_end = now + timedelta(hours=self._cfg.review_window_hours)
        rows = self._storage.due_items(window_end)
        # Фильтруем только те, что еще не на ревью и не опубликованы
        return [r for r in rows if r.get("status") not in ("awaiting", "reviewed")]

    async def run_review(self) -> int:
        """Запускает ежедневное ревью: шлёт владельцу посты на ближайшие 7 часов."""
        if not self._cfg.review_enabled or self._notifier is None or not self._notifier.has_owner:
            return 0
        now = datetime.now(timezone.utc)
        posts = self._get_review_window_posts(now)
        if not posts:
            log.info("Ревью: нет постов в окне %d ч", self._cfg.review_window_hours)
            return 0
        log.info("Ревью: отправляю %d постов на ревью", len(posts))
        await self._notifier.send_for_review(posts)
        # Помечаем как на ревью
        for row in posts:
            self._storage.set_status(row["id"], "awaiting_review")
        return len(posts)

    def review_get_posts(self) -> list[dict]:
        """Возвращает посты, ожидающие ревью."""
        rows = self._storage.due_items(datetime.now(timezone.utc) + timedelta(hours=24))
        return [r for r in rows if r.get("status") == "awaiting_review"]

    async def review_approve(self, queue_id: int) -> bool:
        """Одобряет пост на ревью: ставит статус 'reviewed' для публикации."""
        row = self._storage.get_item(queue_id)
        if row is None or row.get("status") != "awaiting_review":
            return False
        self._storage.set_status(queue_id, "reviewed")
        return True

    async def review_reject(self, queue_id: int) -> bool:
        """Отклоняет пост на ревью: удаляет из очереди и помечает как опубликованный."""
        row = self._storage.get_item(queue_id)
        if row is None or row.get("status") != "awaiting_review":
            return False
        self._storage.mark_published(row["url"], row["source"], row["title"])
        self._storage.dequeue(queue_id)
        return True

    async def review_update_text(self, queue_id: int, new_text: str) -> bool:
        """Обновляет текст поста на ревью."""
        row = self._storage.get_item(queue_id)
        if row is None or row.get("status") != "awaiting_review":
            return False
        self._storage.update_text(queue_id, new_text)
        return True

    async def review_remove_photo(self, queue_id: int) -> bool:
        """Удаляет фото из поста на ревью."""
        row = self._storage.get_item(queue_id)
        if row is None or row.get("status") != "awaiting_review":
            return False
        self._storage.update_photos(queue_id, [], None, None)
        return True

    def backup(self) -> str | None:
        """Резервная копия БД (на старте бота)."""
        return self._storage.backup()

    async def close(self) -> None:
        self._storage.close()
        await self._http.aclose()
        await self._ollama.close()
        if self._publisher:
            await self._publisher.close()
