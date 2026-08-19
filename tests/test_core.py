import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pipeline as pipeline_module
from config import Config
from llm_ollama import OllamaClient, parse_json_response
from pipeline import NewsPipeline
from storage import Storage, normalize_title


class _NewsItem:
    def __init__(self, url, source, title, description="Описание новости", published_at=None):
        self.url = url
        self.source = source
        self.title = title
        self.description = description
        self.published_at = published_at or datetime.now(timezone.utc)


class _ParserWithItem:
    """Парсер, возвращающий одну новость — для проверки publish_one_now."""
    name = "test"

    def __init__(self, item):
        self._item = item

    async def fetch_items(self, max_age_hours=None):
        return [self._item]


class _FakeOllama:
    async def check(self):
        return "model"


def _make_pipeline(cfg: Config) -> NewsPipeline:
    """Pipeline без сети: задержка слота 0, слоты считаем детерминированно."""
    pipeline = NewsPipeline(cfg)
    pipeline._random_delay = lambda: timedelta(0)
    return pipeline


class PureFunctionTests(unittest.TestCase):
    def test_ensure_hashtag_keeps_existing(self):
        text = "Заголовок\n\nТекст новости\n\n#GTA6"
        self.assertEqual(
            pipeline_module._ensure_hashtag(text, "Заголовок", "GTA 6"),
            text,
        )

    def test_ensure_hashtag_appends_game(self):
        text = "Заголовок\n\nТекст новости"
        result = pipeline_module._ensure_hashtag(text, "Заголовок", "GTA 6")
        self.assertTrue(result.endswith("\n#GTA6"))

    def test_ensure_hashtag_cleans_game_name(self):
        text = "Заголовок\n\nТекст новости"
        result = pipeline_module._ensure_hashtag(text, "Заголовок", "Apex: Legends 2!")
        self.assertTrue(result.endswith("\n#ApexLegends2"))

    def test_ensure_hashtag_fallback_to_game(self):
        text = "Заголовок\n\nТекст новости"
        result = pipeline_module._ensure_hashtag(text, "Глава The Blood of Dawnwalker обновил игру", "")
        self.assertTrue(result.endswith("\n#ГлаваTheBloodofDawnwalker"))

    def test_ensure_hashtag_fallback_игры(self):
        text = "Заголовок\n\nТекст новости"
        result = pipeline_module._ensure_hashtag(text, "всё в нижнем регистре без игры", "")
        self.assertTrue(result.endswith("\n#Игры"))

    def test_add_channel_link_requires_at(self):
        text = "Заголовок\n\nТекст новости"
        self.assertEqual(pipeline_module._add_channel_link(text, "parser04ko"), text)

    def test_add_channel_link_before_hashtag(self):
        text = "Заголовок\n\nТекст новости\n\n#GTA6"
        result = pipeline_module._add_channel_link(text, "@parser04ko")
        self.assertEqual(result, "Заголовок\n\nТекст новости\n\n\n@parser04ko\n#GTA6")

    def test_add_channel_link_appends_without_hashtag(self):
        text = "Заголовок\n\nТекст новости"
        result = pipeline_module._add_channel_link(text, "@parser04ko")
        self.assertEqual(result, "Заголовок\n\nТекст новости\n\n@parser04ko")

    def test_format_post_bold_first_line_and_escape(self):
        text = "A < B & C\n\nТекст новости"
        result = pipeline_module._format_post(text)
        self.assertEqual(result, "<b>A &lt; B &amp; C</b>\n\nТекст новости")

    def test_post_is_publishable(self):
        self.assertFalse(pipeline_module._post_is_publishable("Короткий текст"))
        self.assertFalse(pipeline_module._post_is_publishable("Заголовок\n\nСсылка: https://example.com"))
        self.assertFalse(pipeline_module._post_is_publishable("Одна строка, но достаточно длинная чтобы пройти минимальную проверку"))
        self.assertTrue(pipeline_module._post_is_publishable("Заголовок новости\n\nСодержимое новости достаточно длинное для публикации."))

    def test_news_priority(self):
        now = datetime.now(timezone.utc)
        fresh_announce = _NewsItem("https://example.test/a", "s", "Анонс новой игры",
                                   published_at=now - timedelta(hours=1))
        old_neutral = _NewsItem("https://example.test/b", "s", "Тестовая новость",
                                published_at=now - timedelta(hours=48))
        rumor = _NewsItem("https://example.test/c", "s", "Слух о новой игре",
                          published_at=now - timedelta(hours=48))
        self.assertGreater(
            pipeline_module._news_priority(fresh_announce),
            pipeline_module._news_priority(old_neutral),
        )
        self.assertGreater(
            pipeline_module._news_priority(old_neutral),
            pipeline_module._news_priority(rumor),
        )


class PipelineSlotTests(unittest.IsolatedAsyncioTestCase):
    def _cfg(self, peak_hours):
        with tempfile.TemporaryDirectory() as directory:
            return Config(
                "token", "@channel", db_path=str(Path(directory) / "bot.db"), dry_run=True,
                min_post_delay_minutes=0, max_post_delay_minutes=0,
                peak_hours=peak_hours, timezone="Europe/Moscow",
            )

    def _msk(self, iso: str) -> datetime:
        return datetime.fromisoformat(iso).astimezone(timezone.utc)

    async def test_next_slot_after_midnight(self):
        """После полуночи (23:45 МСК) ближайший слот — 08:00 следующего дня."""
        pipeline = _make_pipeline(self._cfg(list(range(8, 22))))
        try:
            slot = pipeline._next_slot(self._msk("2026-08-19T23:45:00+03:00"))
            self.assertEqual(slot, self._msk("2026-08-20T08:00:00+03:00"))
        finally:
            await pipeline.close()

    async def test_next_slot_new_day_after_peak(self):
        """Поздний вечер после последнего слота — слот на следующий день."""
        pipeline = _make_pipeline(self._cfg(list(range(8, 22))))
        try:
            slot = pipeline._next_slot(self._msk("2026-08-19T22:00:00+03:00"))
            self.assertEqual(slot, self._msk("2026-08-20T08:00:00+03:00"))
        finally:
            await pipeline.close()

    async def test_next_slot_after_exact_slot(self):
        """Ровно в слоте: следующий слот строго позже (08:30, а не 08:00)."""
        pipeline = _make_pipeline(self._cfg(list(range(8, 22))))
        try:
            slot = pipeline._next_slot(self._msk("2026-08-19T08:00:00+03:00"))
            self.assertEqual(slot, self._msk("2026-08-19T08:30:00+03:00"))
        finally:
            await pipeline.close()

    async def test_next_slot_single_peak_hour(self):
        """Один час в пике: слоты 08:00 и 08:30, затем следующий день."""
        pipeline = _make_pipeline(self._cfg([8]))
        try:
            self.assertEqual(
                pipeline._next_slot(self._msk("2026-08-19T07:50:00+03:00")),
                self._msk("2026-08-19T08:00:00+03:00"),
            )
            self.assertEqual(
                pipeline._next_slot(self._msk("2026-08-19T08:20:00+03:00")),
                self._msk("2026-08-19T08:30:00+03:00"),
            )
            self.assertEqual(
                pipeline._next_slot(self._msk("2026-08-19T09:00:00+03:00")),
                self._msk("2026-08-20T08:00:00+03:00"),
            )
        finally:
            await pipeline.close()

    async def test_next_slot_no_peak_hours(self):
        pipeline = _make_pipeline(self._cfg([]))
        try:
            with self.assertRaises(RuntimeError):
                pipeline._next_slot(self._msk("2026-08-19T12:00:00+03:00"))
        finally:
            await pipeline.close()

    async def test_next_slot_consecutive_increase(self):
        """Последовательные вызовы дают строго возрастающие слоты."""
        pipeline = _make_pipeline(self._cfg(list(range(8, 22))))
        try:
            first = pipeline._next_slot(self._msk("2026-08-19T10:00:00+03:00"))
            second = pipeline._next_slot(first)
            self.assertGreater(second, first)
        finally:
            await pipeline.close()


class StorageTests(unittest.TestCase):
    def test_queue_rejects_duplicate_url(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(str(Path(directory) / "bot.db"))
            try:
                now = datetime.now(timezone.utc)
                self.assertTrue(storage.enqueue("https://example.test/a", "s", "t", "x", None, now))
                self.assertFalse(storage.enqueue("https://example.test/a", "s", "t", "x", None, now))
                self.assertEqual(storage.queue_count(), 1)
            finally:
                storage.close()

    def test_exact_norm_title_finds_duplicate(self):
        """Дубликат по заголовку ищется через norm_title, без перебора all_titles()."""
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(str(Path(directory) / "bot.db"))
            try:
                now = datetime.now(timezone.utc)
                storage.enqueue("https://example.test/a", "s", "Cyberpunk 2077: Финал", "x", None, now)
                found = storage.exact_norm_title(normalize_title("cyberpunk 2077 финал!!"))
                self.assertEqual(found, "Cyberpunk 2077: Финал")
                self.assertIsNone(storage.exact_norm_title(normalize_title("Cyberpunk 2078")))
            finally:
                storage.close()

    def test_titles_containing_game_sig(self):
        """Поиск заголовков с именем игры (гейт перед LLM-проверкой дубликата)."""
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(str(Path(directory) / "bot.db"))
            try:
                now = datetime.now(timezone.utc)
                storage.enqueue("https://example.test/a", "s", "В Starfield добавили моды", "x", None, now)
                storage.enqueue("https://example.test/b", "s", "Совсем про другую игру", "x", None, now)
                hits = storage.titles_containing_game(normalize_title("Starfield"))
                self.assertEqual(hits, ["В Starfield добавили моды"])
            finally:
                storage.close()

    def test_backfilled_norm_title(self):
        """Старые строки БД (без norm_title) получают его при открытии."""
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "bot.db")
            storage = Storage(db_path)
            now = datetime.now(timezone.utc)
            storage.mark_published("https://example.test/a", "s", "Старая новость: Продолжение")
            storage.close()

            import sqlite3

            conn = sqlite3.connect(db_path)
            conn.execute("UPDATE published SET norm_title = NULL")
            conn.commit()
            conn.close()

            storage = Storage(db_path)
            try:
                found = storage.exact_norm_title(normalize_title("старая новость продолжение"))
                self.assertEqual(found, "Старая новость: Продолжение")
            finally:
                storage.close()


class _Publisher:
    def __init__(self):
        self.posts = []

    async def publish(self, text, photos, video):
        self.posts.append(text)
        return [1]

    async def close(self):
        pass


class OllamaClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_ollama_semaphore_limits_concurrency(self):
        """OLLAMA_CONCURRENCY=1: запросы к Ollama не выполняются параллельно,
        даже если сетевой семафор позволяет больше."""
        import httpx

        class _FakeTransport(httpx.AsyncBaseTransport):
            def __init__(self):
                self.concurrent = 0
                self.max_concurrent = 0

            async def handle_async_request(self, request):
                self.concurrent += 1
                self.max_concurrent = max(self.max_concurrent, self.concurrent)
                await asyncio.sleep(0.05)
                self.concurrent -= 1
                return httpx.Response(200, json={"response": "текст"})

        transport = _FakeTransport()
        client = OllamaClient(
            "http://ollama.test", "model", "fallback", timeout=5, concurrency=1
        )
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=transport, timeout=5)
        try:
            await asyncio.gather(*[
                client.rewrite("model", f"Заголовок {i}", "статья")
                for i in range(6)
            ])
            self.assertEqual(transport.max_concurrent, 1)
        finally:
            await client.close()


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_awaiting_post_is_not_published_by_force(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config(
                "token", "@channel", db_path=str(Path(directory) / "bot.db"),
                dry_run=True, approve_posts=True,
            )
            pipeline = NewsPipeline(cfg)
            publisher = _Publisher()
            pipeline._publisher = publisher
            try:
                pipeline._storage.enqueue(
                    "https://example.test/a", "source", "title", "text", None,
                    datetime.now(timezone.utc) - timedelta(minutes=1), status="awaiting",
                )
                self.assertEqual(await pipeline.publish_due(force=True), 0)
                self.assertEqual(publisher.posts, [])
                self.assertEqual(pipeline._storage.queue_count(), 1)
            finally:
                await pipeline.close()

    async def test_awaiting_review_post_is_not_published(self):
        """Пост на ревью не публикуется ни планово, ни с force — только после
        кнопки «Опубликовать» (review_approve → reviewed)."""
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config(
                "token", "@channel", db_path=str(Path(directory) / "bot.db"),
                dry_run=True,
            )
            pipeline = NewsPipeline(cfg)
            publisher = _Publisher()
            pipeline._publisher = publisher
            try:
                now = datetime.now(timezone.utc)
                pipeline._storage.enqueue(
                    "https://example.test/a", "source", "title", "text", None,
                    now - timedelta(minutes=1), status="awaiting_review",
                )
                self.assertEqual(await pipeline.publish_due(), 0)
                self.assertEqual(await pipeline.publish_due(force=True), 0)
                self.assertEqual(publisher.posts, [])
                self.assertEqual(pipeline._storage.queue_count(), 1)
            finally:
                await pipeline.close()

    async def test_awaiting_review_publishes_after_approve(self):
        """После одобрения (статус reviewed) пост выходит в свой слот."""
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config(
                "token", "@channel", db_path=str(Path(directory) / "bot.db"),
                dry_run=True,
            )
            pipeline = NewsPipeline(cfg)
            publisher = _Publisher()
            pipeline._publisher = publisher
            try:
                now = datetime.now(timezone.utc)
                pipeline._storage.enqueue(
                    "https://example.test/a", "source", "title", "text", None,
                    now - timedelta(minutes=1), status="awaiting_review",
                )
                row = pipeline._storage.due_items(now)[0]
                self.assertTrue(await pipeline.review_approve(row["id"]))
                self.assertEqual(await pipeline.publish_due(force=True), 1)
                self.assertEqual(publisher.posts, ["text"])
                self.assertEqual(pipeline._storage.queue_count(), 0)
            finally:
                await pipeline.close()

    async def test_review_window_skips_awaiting_review(self):
        """Повторный run_review не шлёт посты, которые уже на ревью."""
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config(
                "token", "@channel", db_path=str(Path(directory) / "bot.db"),
                dry_run=True, review_window_hours=7,
            )
            pipeline = NewsPipeline(cfg)
            try:
                now = datetime.now(timezone.utc)
                pipeline._storage.enqueue(
                    "https://example.test/a", "s", "Свежий пост", "text", None,
                    now, status="queued",
                )
                pipeline._storage.enqueue(
                    "https://example.test/b", "s", "Уже на ревью", "text", None,
                    now, status="awaiting_review",
                )
                pipeline._storage.enqueue(
                    "https://example.test/c", "s", "Одобренный", "text", None,
                    now, status="reviewed",
                )
                posts = pipeline._get_review_window_posts(now)
                urls = [p["url"] for p in posts]
                self.assertIn("https://example.test/a", urls)
                self.assertNotIn("https://example.test/b", urls)
                self.assertNotIn("https://example.test/c", urls)
            finally:
                await pipeline.close()

    async def test_next_slot_respects_minimum_delay(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config(
                "token", "@channel", db_path=str(Path(directory) / "bot.db"), dry_run=True,
                min_post_delay_minutes=60, max_post_delay_minutes=60,
                peak_hours=list(range(24)),
            )
            pipeline = NewsPipeline(cfg)
            try:
                after = datetime.now(timezone.utc)
                self.assertGreaterEqual(pipeline._next_slot(after), after + timedelta(minutes=60))
            finally:
                await pipeline.close()

    async def test_both_publish_paths_use_build_post(self):
        """Обе точки публикации (_process_item и publish_one_now) должны
        строить пост через единый _build_post, а не дублировать цепочку."""
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config(
                "token", "@channel", db_path=str(Path(directory) / "bot.db"), dry_run=True,
            )
            pipeline = NewsPipeline(cfg)
            try:
                calls = []

                async def fake_build_post(item, model):
                    calls.append(item.url)
                    return None

                pipeline._build_post = fake_build_post
                pipeline._ollama.check = _FakeOllama().check
                item = _NewsItem("https://example.test/a", "test", "Заголовок новости")

                self.assertFalse(await pipeline._process_item(item, "model", datetime.now(timezone.utc)))

                import pipeline as pipeline_module

                parser = _ParserWithItem(item)
                original = pipeline_module.build_parsers
                pipeline_module.build_parsers = lambda *args, **kwargs: [parser]
                try:
                    self.assertFalse(await pipeline.publish_one_now())
                finally:
                    pipeline_module.build_parsers = original

                self.assertEqual(len(calls), 2, "каждый путь должен вызвать _build_post ровно один раз")
                self.assertEqual(calls[0], calls[1])
            finally:
                await pipeline.close()


class NewsLifecycleTests(unittest.TestCase):
    """Жизненный цикл новости: status (Telegram) + video_status (видео)."""

    def test_upsert_news_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(str(Path(directory) / "bot.db"))
            try:
                nid = storage.upsert_news(
                    "https://example.test/a", "s", "Новость", "Описание",
                    "2026-08-20T10:00:00+00:00",
                )
                self.assertGreater(nid, 0)
                again = storage.upsert_news(
                    "https://example.test/a", "s", "Новость", "Описание",
                    "2026-08-20T10:00:00+00:00",
                )
                self.assertEqual(nid, again, "повторная вставка — тот же id")
            finally:
                storage.close()

    def test_news_photos_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(str(Path(directory) / "bot.db"))
            try:
                nid = storage.upsert_news(
                    "https://example.test/a", "s", "Новость", "Описание", ""
                )
                storage.save_news_photos(nid, [b"jpeg-one", b"jpeg-two"])
                news = storage.get_news(nid)
                self.assertEqual(storage.load_news_photos(news), [b"jpeg-one", b"jpeg-two"])
            finally:
                storage.close()

    def test_video_lifecycle(self):
        """Полный цикл видео: none → processing → ready → published → pending(retry)."""
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(str(Path(directory) / "bot.db"))
            try:
                nid = storage.upsert_news(
                    "https://example.test/a", "s", "Новость", "Описание", ""
                )
                storage.set_news_status(nid, "telegram_published")
                storage.set_video_status(nid, "video_processing")
                storage.mark_video_ready(nid, "/tmp/a.mp4", 30.5, "script", "headline")
                storage.mark_video_published(nid, "https://drive.google.com/file/d/x/view")
                news = storage.get_news(nid)
                self.assertEqual(news["status"], "telegram_published")
                self.assertEqual(news["video_status"], "video_published")
                self.assertEqual(news["video_duration"], 30.5)
                self.assertEqual(news["video_path"], "/tmp/a.mp4")
                # новость с видео больше не в очереди
                self.assertNotIn(nid, [r["id"] for r in storage.video_pending()])
                # retry возвращает в очередь
                self.assertTrue(storage.retry_video(nid))
                self.assertEqual(storage.get_news(nid)["video_status"], "pending")
                self.assertIn(nid, [r["id"] for r in storage.video_pending()])
            finally:
                storage.close()

    def test_video_pending_only_published_news(self):
        """Видео-очередь берёт только опубликованные новости (независимость от Telegram)."""
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(str(Path(directory) / "bot.db"))
            try:
                nid_ready = storage.upsert_news(
                    "https://example.test/p", "s", "Опубликована", "Описание", ""
                )
                storage.set_news_status(nid_ready, "telegram_published")
                nid_new = storage.upsert_news(
                    "https://example.test/n", "s", "Новая", "Описание", ""
                )
                pending = [r["id"] for r in storage.video_pending()]
                self.assertIn(nid_ready, pending)
                self.assertNotIn(nid_new, pending)
            finally:
                storage.close()

    def test_news_stats(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(str(Path(directory) / "bot.db"))
            try:
                a = storage.upsert_news("https://example.test/a", "s", "A", "", "")
                storage.set_news_status(a, "telegram_ready")
                b = storage.upsert_news("https://example.test/b", "s", "B", "", "")
                storage.set_news_status(b, "telegram_published")
                storage.set_video_status(b, "video_ready")
                stats = storage.news_stats()
                self.assertEqual(stats["status"].get("telegram_ready"), 1)
                self.assertEqual(stats["status"].get("telegram_published"), 1)
                self.assertEqual(stats["video_status"].get("video_ready"), 1)
            finally:
                storage.close()


class OllamaJsonTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_json_response_strips_code_fences(self):
        data = parse_json_response('```json\n{"headline": "H", "script": "S"}\n```')
        self.assertEqual(data, {"headline": "H", "script": "S"})

    def test_parse_json_response_extracts_embedded_json(self):
        data = parse_json_response('Вот результат: {"score": 7, "interesting": true}')
        self.assertEqual(data["score"], 7)
        self.assertTrue(data["interesting"])

    def test_parse_json_response_raises_on_garbage(self):
        with self.assertRaises(Exception):
            parse_json_response("никакого json нет")

    async def test_video_script_uses_json_mode(self):
        """video_script должен запросить JSON у модели и вернуть headline+script."""
        import httpx

        class _FakeTransport(httpx.AsyncBaseTransport):
            def __init__(self):
                self.used_format = None

            async def handle_async_request(self, request):
                body = json.loads(request.content)
                self.used_format = body.get("format")
                return httpx.Response(
                    200, json={"response": '{"headline": "Заголовок", "script": "Сценарий текста"}'
                })

        transport = _FakeTransport()
        client = OllamaClient("http://ollama.test", "model", "fallback", timeout=5)
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=transport, timeout=5)
        try:
            res = await client.video_script("model", "Заголовок", "Описание", "stopgame")
            self.assertEqual(res["headline"], "Заголовок")
            self.assertEqual(res["script"], "Сценарий текста")
            self.assertEqual(transport.used_format, "json")
        finally:
            await client.close()

    async def test_video_script_raises_without_script(self):
        import httpx

        class _FakeTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                return httpx.Response(200, json={"response": '{"headline": "H"}'})

        client = OllamaClient("http://ollama.test", "model", "fallback", timeout=5)
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=_FakeTransport(), timeout=5)
        try:
            with self.assertRaises(Exception):
                await client.video_script("model", "Заголовок", "Описание", "s")
        finally:
            await client.close()
