import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import Config
from llm_ollama import OllamaClient
from pipeline import NewsPipeline
from storage import Storage, normalize_title


class _NewsItem:
    def __init__(self, url, source, title, description="Описание новости"):
        self.url = url
        self.source = source
        self.title = title
        self.description = description
        self.published_at = datetime.now(timezone.utc)


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
