"""Управление ботом через личные сообщения: панель кнопок-плиток.

Владелец определяется автоматически: первый, кто напишет боту в личку, —
или через переменную OWNER_CHAT_ID в .env. Любое сообщение владельца
открывает панель управления: публикация сейчас, очередь, последний пост,
удаление, статистика, здоровье, свой пост, помощь. Посты на утверждение
приходят с кнопками «Опубликовать» / «Пропустить».
"""
import asyncio
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from aiogram import Bot
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)

from owner import load_owner, save_owner
from pipeline import NewsPipeline

log = logging.getLogger("commands")

MSK = ZoneInfo("Europe/Moscow")

PANEL = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="Опубликовать сейчас", callback_data="publish_now"),
        InlineKeyboardButton(text="Свой пост", callback_data="post"),
    ],
    [
        InlineKeyboardButton(text="Очередь", callback_data="queue"),
        InlineKeyboardButton(text="Последний пост", callback_data="last"),
    ],
    [
        InlineKeyboardButton(text="Удалить последний", callback_data="delete_last"),
        InlineKeyboardButton(text="Статистика", callback_data="stats"),
    ],
    [
        InlineKeyboardButton(text="Здоровье", callback_data="health"),
        InlineKeyboardButton(text="Помощь", callback_data="help"),
    ],
])

HELP_TEXT = (
    "Управление ботом — кнопки: напишите любое сообщение, появится панель.\n\n"
    "Команды тоже работают:\n"
    "/publish_now — обработать и опубликовать одну свежую новость\n"
    "/stats — статистика: опубликовано постов, очередь\n"
    "/next — когда следующий пост из очереди\n"
    "/health — здоровье бота: Ollama, БД, ошибки за сутки\n"
    "/help — список команд\n\n"
    "Посты перед публикацией приходят вам на утверждение с кнопками "
    "«Опубликовать» / «Пропустить»."
)


def _fmt(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(MSK).strftime("%d.%m %H:%M")
    except ValueError:
        return iso


class CommandLoop:
    def __init__(self, cfg, pipeline: NewsPipeline):
        self._cfg = cfg
        self._pipeline = pipeline
        self._bot = Bot(token=cfg.telegram_token)
        self._owner_id = load_owner(cfg.db_path)
        self._awaiting_post = False
        self._awaiting_review_text = None

    async def run(self) -> None:
        offset = 0
        while True:
            try:
                updates = await self._bot.get_updates(
                    offset=offset, timeout=25,
                    allowed_updates=["message", "callback_query"],
                )
                for upd in updates:
                    offset = upd.update_id + 1
                    if upd.callback_query:
                        await self._handle_callback(upd.callback_query)
                    else:
                        await self._handle(upd)
            except Exception:
                log.exception("Ошибка цикла команд")
            await asyncio.sleep(1)

    async def _handle(self, upd: Update) -> None:
        msg = upd.message
        if not msg or msg.chat.type != "private" or not msg.text:
            return
        if self._owner_id is None:
            setup_code = self._cfg.owner_setup_code
            if not setup_code or msg.text.strip() != f"/start {setup_code}":
                await self._send(
                    msg.chat.id,
                    "Владелец ещё не настроен. Укажите OWNER_CHAT_ID или "
                    "OWNER_SETUP_CODE и отправьте /start <код>.",
                )
                return
            self._owner_id = msg.chat.id
            save_owner(self._cfg.db_path, msg.chat.id)
            await self._send(msg.chat.id, "Ты теперь владелец бота.")
            await self._send(msg.chat.id, "Управление ботом:", keyboard=PANEL)
            return
        if msg.chat.id != self._owner_id:
            return

        if self._awaiting_post:
            self._awaiting_post = False
            ok = await self._pipeline.publish_text_post(msg.text)
            await self._send(
                msg.chat.id,
                "Опубликовано в канал." if ok else "Не удалось опубликовать — проверьте журнал.",
            )
            return

        if self._awaiting_review_text:
            queue_id = self._awaiting_review_text
            self._awaiting_review_text = False
            ok = await self._pipeline.review_update_text(queue_id, msg.text)
            await self._send(
                msg.chat.id,
                f"✅ Текст поста #{queue_id} обновлён." if ok else "❌ Ошибка обновления текста.",
            )
            await self._send_review_batch(msg.chat.id)
            return

        parts = msg.text.strip().split()
        if not parts:
            await self._send(msg.chat.id, "Управление ботом:", keyboard=PANEL)
            return
        cmd = parts[0].lower()
        if cmd == "/publish_now":
            reply = await self._cmd_publish_now()
            await self._send(msg.chat.id, reply)
        elif cmd == "/stats":
            await self._send(msg.chat.id, await self._cmd_stats())
        elif cmd == "/next":
            await self._send(msg.chat.id, await self._cmd_next())
        elif cmd == "/health":
            await self._send(msg.chat.id, await self._cmd_health())
        elif cmd in ("/help", "/start"):
            await self._send(msg.chat.id, HELP_TEXT)
            await self._send(msg.chat.id, "Управление ботом:", keyboard=PANEL)
        elif cmd.startswith("/"):
            await self._send(msg.chat.id, "Неизвестная команда. /help — список.")
        else:
            await self._send(msg.chat.id, "Управление ботом:", keyboard=PANEL)

    async def _handle_callback(self, cb: CallbackQuery) -> None:
        if cb.from_user.id != self._owner_id:
            await self._bot.answer_callback_query(cb.id, text="Нет доступа")
            return
        data = cb.data or ""
        try:
            if data.startswith("pub:"):
                ok = await self._pipeline.publish_approved(int(data.split(":", 1)[1]))
                await self._bot.answer_callback_query(
                    cb.id, text="Опубликован" if ok else "Пост уже обработан"
                )
            elif data.startswith("skip:"):
                ok = self._pipeline.skip_approved(int(data.split(":", 1)[1]))
                await self._bot.answer_callback_query(
                    cb.id, text="Пропущен" if ok else "Пост уже обработан"
                )
            elif data == "publish_now":
                await self._bot.answer_callback_query(cb.id)
                await self._send(cb.message.chat.id, await self._cmd_publish_now())
            elif data == "post":
                await self._bot.answer_callback_query(cb.id)
                self._awaiting_post = True
                await self._send(cb.message.chat.id, "Пришли текст поста — опубликую в канал.")
            elif data == "queue":
                await self._bot.answer_callback_query(cb.id)
                await self._send_queue(cb.message.chat.id)
            elif data.startswith("qpub:"):
                ok = await self._pipeline.publish_approved(int(data.split(":", 1)[1]))
                await self._bot.answer_callback_query(cb.id, text="Опубликован" if ok else "Пост недоступен")
            elif data.startswith("qdelay:"):
                ok = self._pipeline.postpone_queue_item(int(data.split(":", 1)[1]))
                await self._bot.answer_callback_query(cb.id, text="Перенесён на час" if ok else "Пост недоступен")
            elif data.startswith("qdrop:"):
                ok = self._pipeline.skip_approved(int(data.split(":", 1)[1]))
                await self._bot.answer_callback_query(cb.id, text="Удалён" if ok else "Пост недоступен")
            elif data == "last":
                await self._bot.answer_callback_query(cb.id)
                await self._send(cb.message.chat.id, self._cmd_last())
            elif data == "delete_last":
                await self._bot.answer_callback_query(cb.id)
                await self._send(cb.message.chat.id, await self._cmd_delete_last())
            elif data == "stats":
                await self._bot.answer_callback_query(cb.id)
                await self._send(cb.message.chat.id, await self._cmd_stats())
            elif data == "health":
                await self._bot.answer_callback_query(cb.id)
                await self._send(cb.message.chat.id, await self._cmd_health())
            elif data == "help":
                await self._bot.answer_callback_query(cb.id)
                await self._send(cb.message.chat.id, HELP_TEXT)
            elif data.startswith("rapprove:"):
                await self._handle_review_approve(cb, int(data.split(":", 1)[1]))
            elif data.startswith("rreject:"):
                await self._handle_review_reject(cb, int(data.split(":", 1)[1]))
            elif data.startswith("redit:"):
                await self._handle_review_edit(cb, int(data.split(":", 1)[1]))
            elif data.startswith("rrmphoto:"):
                await self._handle_review_remove_photo(cb, int(data.split(":", 1)[1]))
            elif data == "rdone":
                await self._handle_review_done(cb)
            else:
                await self._bot.answer_callback_query(cb.id)
        except Exception:
            log.exception("Ошибка обработки кнопки %s", data)

    async def _send(self, chat_id: int, text: str,
                    keyboard: InlineKeyboardMarkup | None = None) -> None:
        try:
            await self._bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
        except Exception:
            log.exception("Не удалось ответить в чат %s", chat_id)

    async def _cmd_publish_now(self) -> str:
        try:
            ok = await self._pipeline.publish_one_now()
        except Exception as exc:
            log.exception("publish_now по команде")
            return f"Ошибка: {exc}"
        if not ok:
            return "Свежих новостей нет — все уже опубликованы."
        await self._pipeline.publish_due(force=True)
        return "Готово: новость обработана и опубликована в канал."

    async def _cmd_stats(self) -> str:
        s = self._pipeline.stats()
        activity = self._pipeline.activity_stats()
        lines = [f"Опубликовано постов: {s['published']}"]
        lines.append(f"За сутки: {activity['day']}; за 7 дней: {activity['week']}")
        if activity["sources"]:
            lines.append("Источники за неделю: " + ", ".join(f"{name} — {count}" for name, count in activity["sources"]))
        if s["last_published_at"]:
            lines.append(f"Последний: {_fmt(s['last_published_at'])} МСК")
        if s["queued"]:
            lines.append(f"В очереди: {s['queued']} — следующий {_fmt(s['next_publish_at'])} МСК")
        else:
            lines.append("Очередь пуста")
        return "\n".join(lines)

    async def _cmd_next(self) -> str:
        s = self._pipeline.stats()
        if not s["queued"]:
            return "Очередь пуста — следующий пост появится после ближайшего сбора новостей."
        return f"В очереди {s['queued']} пост(ов). Следующий — {_fmt(s['next_publish_at'])} МСК."

    async def _send_queue(self, chat_id: int) -> None:
        rows = self._pipeline.queue_items()
        if not rows:
            await self._send(chat_id, "Очередь пуста.")
            return
        lines = ["Ближайшие посты:"]
        buttons = []
        for row in rows:
            lines.append(f"#{row['id']} — {_fmt(row['publish_at'])} МСК\n{row['title'][:70]}")
            buttons.append([
                InlineKeyboardButton(text="Опубликовать", callback_data=f"qpub:{row['id']}"),
                InlineKeyboardButton(text="+1 час", callback_data=f"qdelay:{row['id']}"),
                InlineKeyboardButton(text="Удалить", callback_data=f"qdrop:{row['id']}"),
            ])
        await self._send(chat_id, "\n\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))

    def _cmd_last(self) -> str:
        last = self._pipeline.last_post()
        if not last:
            return "Пока ничего не опубликовано."
        return f"Последний пост: {last[0][:80]}\n{_fmt(last[1])} МСК"

    async def _cmd_delete_last(self) -> str:
        ids = self._pipeline.last_messages()
        if not ids:
            return "Нет постов для удаления."
        removed = 0
        for mid in ids:
            try:
                await self._bot.delete_message(
                    chat_id=self._cfg.channel_id, message_id=mid
                )
                removed += 1
            except Exception:
                log.warning("Не удалось удалить сообщение %s из канала", mid)
        self._pipeline.drop_messages(ids)
        return f"Удалено сообщений: {removed}."

    async def _cmd_health(self) -> str:
        lines = ["Ollama: ", "БД: ", "Ошибки: "]
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self._cfg.ollama_base_url}/api/tags")
                resp.raise_for_status()
                models = [m.get("name", "") for m in resp.json().get("models", [])]
                lines[0] += "OK (" + ", ".join(models[:3]) + ")"
        except Exception as exc:
            lines[0] += f"недоступен ({type(exc).__name__})"
        db_path = os.path.abspath(self._cfg.db_path)
        try:
            size_mb = os.path.getsize(db_path) / (1024 * 1024)
        except OSError:
            size_mb = 0
        s = self._pipeline.stats()
        lines[1] += f"{size_mb:.1f} МБ, опубликовано {s['published']}, в очереди {s['queued']}"
        errors = self._errors_last_day()
        lines[2] += f"{errors} за сутки"
        return "\n".join(lines)

    def _errors_last_day(self) -> int:
        try:
            with open(os.path.join(self._cfg.log_dir, "bot.log")) as f:
                lines = f.readlines()
        except OSError:
            return 0
        cutoff = datetime.now().astimezone().timestamp() - 24 * 3600
        count = 0
        for line in lines[-5000:]:
            if "[ERROR]" not in line:
                continue
            try:
                ts = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").astimezone().timestamp()
            except ValueError:
                continue
            if ts >= cutoff:
                count += 1
        return count

    def _format_review_post(self, row: dict) -> tuple[str, InlineKeyboardMarkup]:
        """Форматирует пост для ревью с кнопками действий."""
        text = row["text"]
        preview = text[:300] + ("..." if len(text) > 300 else "")
        has_photo = bool(row["photos"])
        has_video = bool(row["video"])
        lines = [
            f"📋 <b>Пост #{row['id']}</b> — {_fmt(row['publish_at'])} МСК",
            f"{preview}",
            f"📸 Фото: {'есть' if has_photo else 'нет'} | 🎬 Видео: {'есть' if has_video else 'нет'}",
        ]
        buttons = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"rapprove:{row['id']}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rreject:{row['id']}"),
            ],
            [
                InlineKeyboardButton(text="✏️ Редактировать текст", callback_data=f"redit:{row['id']}"),
                InlineKeyboardButton(text="🗑 Убрать фото", callback_data=f"rrmphoto:{row['id']}"),
            ],
        ])
        return "\n".join(lines), buttons

    async def _handle_review_approve(self, cb: CallbackQuery, queue_id: int) -> None:
        ok = await self._pipeline.review_approve(queue_id)
        await self._bot.answer_callback_query(cb.id, text="✅ Одобрено" if ok else "Ошибка")
        if ok:
            await self._send_review_batch(cb.message.chat.id)

    async def _handle_review_reject(self, cb: CallbackQuery, queue_id: int) -> None:
        ok = await self._pipeline.review_reject(queue_id)
        await self._bot.answer_callback_query(cb.id, text="❌ Отклонено" if ok else "Ошибка")
        if ok:
            await self._send_review_batch(cb.message.chat.id)

    async def _handle_review_edit(self, cb: CallbackQuery, queue_id: int) -> None:
        self._awaiting_review_text = queue_id
        await self._bot.answer_callback_query(cb.id)
        await self._send(cb.message.chat.id, f"✏️ Пришли новый текст для поста #{queue_id}:")

    async def _handle_review_remove_photo(self, cb: CallbackQuery, queue_id: int) -> None:
        ok = await self._pipeline.review_remove_photo(queue_id)
        await self._bot.answer_callback_query(cb.id, text="🗑 Фото удалено" if ok else "Ошибка")
        if ok:
            await self._send_review_batch(cb.message.chat.id)

    async def _handle_review_done(self, cb: CallbackQuery) -> None:
        await self._bot.answer_callback_query(cb.id, text="Ревью завершено")
        await self._send(cb.message.chat.id, "✅ Ревью завершено. Одобренные посты будут опубликованы в своё время.", keyboard=PANEL)

    async def _send_review_batch(self, chat_id: int) -> None:
        posts = self._pipeline.review_get_posts()
        if not posts:
            await self._send(chat_id, "📭 Нет постов на ревью.", keyboard=PANEL)
            return
        await self._send(chat_id, f"📋 <b>Ревью постов</b> — {len(posts)} шт. на ближайшие 7 часов:")
        for row in posts:
            text, buttons = self._format_review_post(row)
            await self._send(chat_id, text, buttons)
        await self._send(chat_id, "Когда закончите — нажмите «Завершить ревью».", InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Завершить ревью", callback_data="rdone")]
        ]))

    async def close(self) -> None:
        await self._bot.session.close()
