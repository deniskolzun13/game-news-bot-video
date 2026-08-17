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
from datetime import datetime

# На части сетей IPv6 к api.telegram.org не работает и вешает запросы —
# форсируем IPv4 для aiohttp (aiogram/Telegram API).
os.environ.setdefault("AIOHTTP_CLIENT_FORCE_IPV4", "1")

from commands import CommandLoop
from config import load_config
from logging_setup import setup_logging
from notifier import Notifier
from pipeline import NewsPipeline

log = logging.getLogger("bot")

TICK_SECONDS = 30


async def run_once(pipeline: NewsPipeline) -> None:
    log.info("=== Сбор новостей ===")
    results = await pipeline.process_all()
    for source, (found, queued, errors) in results.items():
        log.info("Источник %s: найдено=%d запланировано=%d ошибок=%d",
                 source, found, queued, errors)
    log.info("=== Сбор завершён ===")


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
    parser.add_argument("--dry-run", action="store_true",
                        help="собрать и обработать новости без публикации в Telegram")
    args = parser.parse_args()

    cfg = load_config(dry_run=args.dry_run)
    setup_logging(cfg.log_dir)

    notifier = Notifier(cfg.telegram_token, cfg.db_path) if not args.dry_run else None
    pipeline = NewsPipeline(cfg, notifier)
    try:
        if args.now:
            if await pipeline.publish_one_now():
                await pipeline.publish_due(force=True)
            return
        if args.once or args.dry_run:
            await run_once(pipeline)
            # Одноразовый запуск (например, из cron) не обрабатывает callback-и
            # Telegram, поэтому новые queued-посты публикуем сразу. Уже отправленные
            # на утверждение записи остаются ждать нажатия кнопки.
            await pipeline.publish_due(force=not args.dry_run)
            return

        log.info(
            "Планировщик: сбор каждые %d мин, посты публикуются с рандомной "
            "задержкой %d–%d мин. Остановка — Ctrl+C.",
            cfg.poll_interval_minutes,
            cfg.min_post_delay_minutes,
            cfg.max_post_delay_minutes,
        )
        backup = pipeline.backup()
        if backup:
            log.info("Бэкап БД: %s", backup)
        cmd_loop = CommandLoop(cfg, pipeline)
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
            while True:
                tick += 1
                now = datetime.now()
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
        if notifier:
            await notifier.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановлено.")
