"""Загрузка/сохранение chat_id владельца бота.

Источник: переменная окружения OWNER_CHAT_ID или файл owner_chat_id.txt
рядом с БД (создаётся автоматически при первом сообщении боту).
"""
import logging
import os

log = logging.getLogger("owner")


def owner_file(db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "owner_chat_id.txt")


def load_owner(db_path: str) -> int | None:
    raw = os.getenv("OWNER_CHAT_ID", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            log.warning("OWNER_CHAT_ID не является числом: %r", raw)
    try:
        with open(owner_file(db_path)) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def save_owner(db_path: str, chat_id: int) -> None:
    path = owner_file(db_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(str(chat_id))
    log.info("Владелец бота сохранён: chat_id=%d", chat_id)