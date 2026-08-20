"""Загрузка/сохранение chat_id владельца бота.

Источник: переменная окружения OWNER_CHAT_ID или файл owner_chat_id.txt
рядом с БД (создаётся автоматически при первом сообщении боту).

Безопасность:
- OWNER_SETUP_CODE: одноразовый код с TTL (10 минут) для первичной настройки владельца
- Команда /transfer_ownership: смена владельца (доступна только текущему владельцу)
- Логирование всех попыток авторизации
"""
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("owner")


@dataclass
class OwnerSetupCode:
    """Код настройки владельца с TTL."""
    code: str
    created_at: float
    used: bool = False

    @property
    def is_expired(self) -> bool:
        """Код живёт 10 минут (600 секунд)."""
        return time.time() - self.created_at > 600

    @property
    def is_valid(self) -> bool:
        return not self.used and not self.is_expired


# В памяти: код настройки (переживает перезапуски процесса только если сохранён в файле)
_setup_code_storage: OwnerSetupCode | None = None


def owner_file(db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "owner_chat_id.txt")


def setup_code_file(db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "owner_setup_code.json")


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


def load_setup_code(db_path: str) -> OwnerSetupCode | None:
    """Загружает код настройки из файла."""
    global _setup_code_storage
    if _setup_code_storage is not None:
        return _setup_code_storage

    path = setup_code_file(db_path)
    try:
        import json
        with open(path) as f:
            data = json.load(f)
        code = OwnerSetupCode(
            code=data["code"],
            created_at=data["created_at"],
            used=data.get("used", False),
        )
        _setup_code_storage = code
        return code
    except (OSError, ValueError, KeyError):
        return None


def save_setup_code(db_path: str, code: OwnerSetupCode) -> None:
    """Сохраняет код настройки в файл."""
    global _setup_code_storage
    path = setup_code_file(db_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    import json
    data = {
        "code": code.code,
        "created_at": code.created_at,
        "used": code.used,
    }
    with open(path, "w") as f:
        json.dump(data, f)
    _setup_code_storage = code


def generate_setup_code(db_path: str, provided_code: str | None = None) -> str:
    """Генерирует и сохраняет новый код настройки владельца.

    Если provided_code задан — использует его, иначе генерирует случайный.
    Возвращает сгенерированный код.
    """
    import secrets
    code_str = provided_code or secrets.token_urlsafe(16)
    code = OwnerSetupCode(code=code_str, created_at=time.time(), used=False)
    save_setup_code(db_path, code)
    log.info("Сгенерирован код настройки владельца (TTL 10 мин): %s", code_str)
    return code_str


def verify_setup_code(db_path: str, input_code: str) -> bool:
    """Проверяет код настройки владельца.

    Возвращает True, если код валиден (не истёк, не использован, совпадает).
    При успехе помечает код как использованный.
    """
    code = load_setup_code(db_path)
    if code is None:
        log.warning("Попытка использования несуществующего кода настройки: %s", input_code[:8])
        return False
    if code.is_expired:
        log.warning("Попытка использования просроченного кода настройки: %s", input_code[:8])
        return False
    if code.used:
        log.warning("Попытка повторного использования кода настройки: %s", input_code[:8])
        return False
    if code.code != input_code:
        log.warning("Неверный код настройки владельца: %s", input_code[:8])
        return False

    code.used = True
    save_setup_code(db_path, code)
    log.info("Код настройки владельца успешно использован")
    return True


def transfer_ownership(db_path: str, current_owner_id: int, new_owner_id: int) -> bool:
    """Смена владельца бота.

    Доступно только текущему владельцу. Возвращает True при успехе.
    """
    current = load_owner(db_path)
    if current != current_owner_id:
        log.warning("Попытка смены владельца не владельцем: %s (real: %s)",
                    current_owner_id, current)
        return False

    save_owner(db_path, new_owner_id)
    log.info("Владелец изменён: %d -> %d", current_owner_id, new_owner_id)
    return True


def log_auth_attempt(chat_id: int, success: bool, reason: str = "") -> None:
    """Логирует попытку авторизации."""
    if success:
        log.info("Авторизация успешна: chat_id=%d", chat_id)
    else:
        log.warning("Авторизация неудачна: chat_id=%d, reason=%s", chat_id, reason)


def is_owner(db_path: str, chat_id: int) -> bool:
    """Проверяет, является ли пользователь владельцем."""
    owner = load_owner(db_path)
    return owner == chat_id


__all__ = [
    "load_owner",
    "save_owner",
    "generate_setup_code",
    "verify_setup_code",
    "transfer_ownership",
    "log_auth_attempt",
    "is_owner",
    "OwnerSetupCode",
]