import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import Config
from pipeline import NewsPipeline
from storage import Storage


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
