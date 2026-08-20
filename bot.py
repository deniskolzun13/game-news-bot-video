#!/usr/bin/env python3
"""Точка входа: периодический сбор и публикация игровых новостей.

Запуск:        python bot.py
Один прогон:   python bot.py --once
Без публикации: python bot.py --dry-run
"""
import argparse
import asyncio
import logging
import os
import time
from datetime import datetime, timedelta

# На части сетей IPv6 к api.telegram.org не работает и вешает запросы —
# форсируем IPv4 для aiohttp (aiogram/Telegram API).
os.environ.setdefault("AIOHTTP_CLIENT_FORCE_IPV4", "1")

from commands import CommandLoop
from config import load_config
from logging_setup import setup_logging
from notifier import Notifier
from pipeline import NewsPipeline
from video_pipeline import VideoPipeline

log = logging.getLogger("bot")

TICK_SECONDS = 30
WATCHDOG_FILE = "watchdog.tmp"  # файл-метка для внешнего watchdog'а


async def run_once(pipeline: NewsPipeline) -> None:
    log.info("=== Сбор новостей ===")
    results = await pipeline.process_all()
    for source, (found, queued, errors) in results.items():
        log.info("Источник %s: найдено=%d запланировано=%d ошибок=%d",
                 source, found, queued, errors)
    log.info("=== Сбор завершён ===")


async def run_video_queue(video_pipeline: VideoPipeline, limit: int = 3) -> None:
    """Обрабатывает независимую видео-очередь (не блокирует Telegram-публикацию)."""
    if video_pipeline is None:
        return
    try:
        results = await video_pipeline.process_pending(limit=limit)
        for res in results:
            if res.get("error"):
                log.warning("[VIDEO] #%s «%s» — %s",
                            res.get("news_id"), res.get("title"), res.get("error"))
            else:
                log.info("[VIDEO] #%s «%s» — готово", res.get("news_id"), res.get("title"))
    except Exception:
        log.exception("Ошибка видео-очереди")


def _is_review_time(cfg, now: datetime) -> tuple[bool, int | None]:
    """Проверяет, наступило ли время ревью (возвращает (True, review_hour) или (False, None))."""
    if not cfg.review_enabled:
        return False, None
    local = now.astimezone()
    hour = local.hour
    if hour in cfg.review_times:
        return True, hour
    return False, None


async def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram-бот с игровыми новостями")
    parser.add_argument("--once", action="store_true",
                        help="выполнить один сбор и завершиться (удобно для cron)")
    parser.add_argument("--now", action="store_true",
                        help="обработать и сразу опубликовать одну свежую новость")
    parser.add_argument("--publish-now", action="store_true",
                        help="синоним --now")
    parser.add_argument("--dry-run", action="store_true",
                        help="собрать и обработать новости без публикации в Telegram")
    parser.add_argument("--generate-video", metavar="NEWS_ID", type=int, default=None,
                        help="сгенерировать видео для конкретной новости по id")
    parser.add_argument("--test", action="store_true",
                        help="тестовый прогон сбора без публикации и без сети Telegram")
    args = parser.parse_args()
    if args.publish_now:
        args.now = True

    cfg = load_config(dry_run=args.dry_run or args.test)
    setup_logging(cfg.log_dir)

    notifier = Notifier(cfg.telegram_token, cfg.db_path) if not (args.dry_run or args.test) else None
    pipeline = NewsPipeline(cfg, notifier)
    video_pipeline = VideoPipeline(cfg, notifier) if not (args.dry_run or args.test) else None
    try:
        if args.generate_video is not None:
            res = await video_pipeline.generate_one(args.generate_video)
            print(f"Новость #{args.generate_video}: {'ГОТОВО' if res['ready'] else 'ОШИБКА'}")
            for step, status in res.get("steps", {}).items():
                print(f"  {step}: {status}")
            if res.get("mp4"):
                print(f"  MP4: {res['mp4']}")
            if res.get("error"):
                print(f"  Ошибка: {res['error']}")
            return
        if args.now:
            if await pipeline.publish_one_now():
                await pipeline.publish_due(force=True)
            return
        if args.once or args.dry_run or args.test:
            await run_once(pipeline)
            await run_video_queue(video_pipeline, limit=1)
            # Одноразовый запуск (например, из cron) не обрабатывает callback-и
            # Telegram, поэтому новые queued-посты публикуем сразу. Уже отправленные
            # на утверждение записи остаются ждать нажатия кнопки.
            await pipeline.publish_due(force=not (args.dry_run or args.test))
            return

        log.info(
            "Планировщик: сбор каждые %d мин, посты публикуются с рандомной "
            "задержкой %d–%d мин. Остановка — Ctrl+C.",
            cfg.poll_interval_minutes,
            cfg.min_post_delay_minutes,
            cfg.max_post_delay_minutes,
        )
        # Проверка пропущенных слотов публикации после перезапуска
        await _check_and_publish_missed_slots(pipeline, notifier)
        backup = pipeline.backup()
        if backup:
            log.info("Бэкап БД: %s", backup)
        cmd_loop = CommandLoop(cfg, pipeline, video_pipeline)
        cmd_task = asyncio.create_task(cmd_loop.run())
        try:
            try:
                await run_once(pipeline)
            except Exception:
                log.exception("Ошибка первого сбора новостей")
                if notifier:
                    await notifier.notify("loop", "Ошибка первого сбора новостей — проверьте журнал.")

            tick = 0
            review_done_today = set()  # часы ревью, уже выполненные сегодня
            last_day = None
            # Инициализируем watchdog файл
            _update_watchdog()
            while True:
                tick += 1
                now = datetime.now()
                # Обновляем watchdog файл на каждом тике
                _update_watchdog()
                # Сброс множества ревью в полночь
                if last_day is not None and now.date() != last_day:
                    review_done_today.clear()
                last_day = now.date()

                try:
                    await pipeline.publish_due()
                except Exception:
                    log.exception("Ошибка при публикации из очереди")
                    if notifier:
                        await notifier.notify("loop", "Ошибка при публикации из очереди.")

                # Независимая видео-очередь: сбой не ломает публикацию постов.
                if cfg.video_enabled and video_pipeline is not None:
                    await run_video_queue(video_pipeline, limit=1)

                # Проверка времени ревью
                is_review_time, review_hour = _is_review_time(cfg, now)
                if is_review_time and review_hour not in review_done_today:
                    try:
                        count = await pipeline.run_review()
                        if count > 0:
                            log.info("Ревью в %02d:00: отправлено %d постов на ревью", review_hour, count)
                        review_done_today.add(review_hour)
                    except Exception:
                        log.exception("Ошибка при запуске ревью в %02d:00", review_hour)
                        if notifier:
                            await notifier.notify("loop", f"Ошибка ревью в {review_hour}:00 — проверьте журнал.")

                # Каждые POLL_INTERVAL_MINUTES минут — сбор новых новостей.
                if tick % max(1, cfg.poll_interval_minutes * 60 // TICK_SECONDS) == 0:
                    try:
                        await run_once(pipeline)
                    except Exception:
                        log.exception("Необработанная ошибка цикла сбора")
                        if notifier:
                            await notifier.notify("loop", "Необработанная ошибка цикла сбора.")
                await asyncio.sleep(TICK_SECONDS)
        finally:
            cmd_task.cancel()
            await cmd_loop.close()
    finally:
        await pipeline.close()
        if video_pipeline:
            await video_pipeline.close()
        if notifier:
            await notifier.close()


def _update_watchdog() -> None:
    """Обновляет файл-метку watchdog для внешнего мониторинга.

    Внешний watchdog (systemd WatchdogSec, или отдельный скрипт)
    может проверять время модификации этого файла.
    """
    try:
        Path(WATCHDOG_FILE).write_text(str(time.time()))
    except Exception:
        pass  # не критично


async def _check_and_publish_missed_slots(pipeline: NewsPipeline, notifier) -> None:
    """Проверяет и публикует пропущенные слоты после перезапуска.

    При перезапуске бота некоторые посты могли не успеть опубликоваться
    в свои слоты. Эта функция находит такие посты и публикует их.
    """
    try:
        storage = pipeline._storage
        now = datetime.now()
        due = storage.due_items(now)
        missed = [row for row in due if row["status"] not in ("awaiting", "awaiting_review")]
        if missed:
            log.info("Найдено пропущенных постов после перезапуска: %d", len(missed))
            for row in missed:
                if row["status"] == "awaiting_review":
                    continue
                fresh = True
                if row.get("created_at"):
                    try:
                        created = datetime.fromisoformat(row["created_at"])
                        if datetime.now() - created > timedelta(hours=pipeline._cfg.news_freshness_hours):
                            fresh = False
                    except ValueError:
                        pass
                if not fresh:
                    log.info("Пропущен устаревший пост #%d: %s", row["id"], row["url"])
                    storage.dequeue(row["id"])
                    continue
                try:
                    await pipeline.publish_due(force=True)
                    log.info("Принудительно опубликован пост #%d", row["id"])
                    if notifier:
                        await notifier.notify("missed_slot", f"Опубликован пропущенный пост #{row['id']}")
                except Exception:
                    log.exception("Ошибка публикации пропущенного поста #%d", row["id"])
    except Exception:
        log.exception("Ошибка проверки пропущенных слотов")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановлено.")
