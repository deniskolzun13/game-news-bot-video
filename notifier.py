"""Уведомления владельцу в личку об ошибках бота и утверждение постов.

Уведомления об ошибках — не чаще одного сообщения в 10 минут по категории.
Посты на утверждение приходят с кнопками «Опубликовать» / «Пропустить».
"""
import logging
import time

from aiogram import Bot
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)

from owner import load_owner

log = logging.getLogger("notifier")

MIN_INTERVAL_SECONDS = 600
MAX_CAPTION = 1024


class Notifier:
    def __init__(self, token: str, db_path: str):
        self._bot = Bot(token=token)
        self._db_path = db_path
        self._owner_id = load_owner(db_path)
        self._last_sent: dict[str, float] = {}

    @property
    def has_owner(self) -> bool:
        self._owner_id = load_owner(self._db_path)
        return self._owner_id is not None

    async def notify(self, category: str, text: str) -> None:
        self._owner_id = load_owner(self._db_path)
        if self._owner_id is None:
            return
        now = time.monotonic()
        if now - self._last_sent.get(category, 0) < MIN_INTERVAL_SECONDS:
            return
        self._last_sent[category] = now
        try:
            await self._bot.send_message(chat_id=self._owner_id, text=text)
        except Exception:
            log.exception("Не удалось отправить уведомление (%s)", category)

    async def send_for_approval(self, row: dict) -> None:
        """Отправляет готовый пост владельцу с кнопками утверждения."""
        self._owner_id = load_owner(self._db_path)
        if self._owner_id is None:
            return
        buttons = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Опубликовать", callback_data=f"pub:{row['id']}"
                ),
                InlineKeyboardButton(
                    text="Пропустить", callback_data=f"skip:{row['id']}"
                ),
            ]
        ])
        text = row["text"]
        html = {"parse_mode": "HTML"}
        caption = text if len(text) <= MAX_CAPTION else None

        if row["video"] is not None:
            video = BufferedInputFile(row["video"], filename="video.mp4")
            await self._bot.send_video(
                chat_id=self._owner_id, video=video, caption=caption,
                reply_markup=buttons, **html,
            )
            if caption is None:
                await self._bot.send_message(chat_id=self._owner_id, text=text, **html)
            return

        photos = row["photos"]
        if not photos:
            await self._bot.send_message(
                chat_id=self._owner_id, text=text, reply_markup=buttons, **html
            )
            return

        if len(photos) == 1:
            photo = BufferedInputFile(photos[0], filename="news.jpg")
            await self._bot.send_photo(
                chat_id=self._owner_id, photo=photo, caption=caption,
                reply_markup=buttons, **html,
            )
            if caption is None:
                await self._bot.send_message(chat_id=self._owner_id, text=text, **html)
            return

        media = []
        for i, p in enumerate(photos):
            first = i == 0 and caption is not None
            media.append(
                InputMediaPhoto(
                    media=BufferedInputFile(p, filename=f"news{i}.jpg"),
                    caption=caption if first else None,
                    parse_mode="HTML" if first else None,
                )
            )
        await self._bot.send_media_group(chat_id=self._owner_id, media=media)
        await self._bot.send_message(
            chat_id=self._owner_id,
            text=f"Пост #{row['id']} на утверждение:",
            reply_markup=buttons,
        )
        if caption is None:
            await self._bot.send_message(chat_id=self._owner_id, text=text, **html)

    async def close(self) -> None:
        await self._bot.session.close()

    async def send_for_review(self, posts: list[dict]) -> None:
        """Отправляет батч постов на ревью с кнопками для каждого."""
        self._owner_id = load_owner(self._db_path)
        if self._owner_id is None:
            return
        html = {"parse_mode": "HTML"}
        
        for row in posts:
            text = row["text"]
            preview = text[:300] + ("..." if len(text) > 300 else "")
            has_photo = bool(row["photos"])
            has_video = bool(row["video"])
            
            lines = [
                f"📋 <b>Пост #{row['id']}</b>",
                f"{preview}",
                f"📸 Фото: {'есть' if has_photo else 'нет'} | 🎬 Видео: {'есть' if has_video else 'нет'}",
            ]
            
            buttons = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Одобрить", callback_data=f"rapprove:{row['id']}"),
                    InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rreject:{row['id']}"),
                ],
                [
                    InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"redit:{row['id']}"),
                    InlineKeyboardButton(text="🗑 Убрать фото", callback_data=f"rrmphoto:{row['id']}"),
                ],
            ])
            
            await self._bot.send_message(
                chat_id=self._owner_id,
                text="\n".join(lines),
                reply_markup=buttons,
                **html
            )
            # Небольшая пауза между сообщениями, чтобы не спамить
            import asyncio
            await asyncio.sleep(0.5)
        
        # Отправляем кнопку завершения ревью
        await self._bot.send_message(
            chat_id=self._owner_id,
            text="Когда закончите — нажмите «Завершить ревью».",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Завершить ревью", callback_data="rdone")]
            ]),
            **html
        )
