import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import Config
from pipeline import NewsPipeline
from storage import Storage


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


class _Publisher:
    def __init__(self):
        self.posts = []

    async def publish(self, text, photos, video):
        self.posts.append(text)
        return [1]

    async def close(self):
        pass


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
