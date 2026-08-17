import logging

from aiogram import Bot
from aiogram.types import BufferedInputFile, InputMediaPhoto

log = logging.getLogger("publisher")

MAX_MESSAGE_LENGTH = 4096


def _message_chunks(text: str) -> list[str]:
    """Делит длинный HTML-пост по границе строки для Bot API."""
    chunks: list[str] = []
    remaining = text
    while len(remaining) > MAX_MESSAGE_LENGTH:
        cut = remaining.rfind("\n", 0, MAX_MESSAGE_LENGTH + 1)
        if cut <= 0:
            cut = MAX_MESSAGE_LENGTH
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


class TelegramPublisher:
    """Публикация постов в Telegram-канал через aiogram (Bot API)."""

    def __init__(self, token: str, chat_id: str, max_caption_length: int = 1024):
        self._bot = Bot(token=token)
        self._chat_id = chat_id
        self._max_caption = max_caption_length

    async def publish(self, text: str, photos: list[bytes] | None = None,
                      video_bytes: bytes | None = None) -> list[int]:
        """Публикует пост и возвращает список message_id отправленных сообщений."""
        html = {"parse_mode": "HTML"}
        ids: list[int] = []
        caption = text if len(text) <= self._max_caption else None

        async def send_text() -> None:
            for chunk in _message_chunks(text):
                msg = await self._bot.send_message(chat_id=self._chat_id, text=chunk, **html)
                ids.append(msg.message_id)

        if video_bytes is not None:
            video = BufferedInputFile(video_bytes, filename="video.mp4")
            msg = await self._bot.send_video(
                chat_id=self._chat_id, video=video, caption=caption, **html
            )
            ids.append(msg.message_id)
            if caption is None:
                await send_text()
            log.info("Опубликован пост с видео (%d символов)", len(text))
            return ids

        if photos:
            if len(photos) == 1:
                photo = BufferedInputFile(photos[0], filename="news.jpg")
                msg = await self._bot.send_photo(
                    chat_id=self._chat_id, photo=photo, caption=caption, **html
                )
                ids.append(msg.message_id)
            else:
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
                msgs = await self._bot.send_media_group(
                    chat_id=self._chat_id, media=media
                )
                ids.extend(m.message_id for m in msgs)
                log.info("Опубликована галерея из %d фото (%d символов)",
                         len(photos), len(text))
            if caption is None:
                await send_text()
            log.info("Опубликован пост с фото (%d символов)", len(text))
            return ids

        await send_text()
        log.info("Опубликован текстовый пост (%d символов)", len(text))
        return ids

    async def close(self) -> None:
        await self._bot.session.close()
