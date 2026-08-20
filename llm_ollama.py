"""LLM клиенты с поддержкой множественных провайдеров и circuit breaker.

Архитектура:
- LLMProvider: базовый абстрактный интерфейс
- OllamaProvider: локальный Ollama (основной)
- OpenAICompatibleProvider: любой OpenAI-совместимый API (fallback)
- LLMClient: высокоуровневый клиент с circuit breaker и авто-фолбэком

Circuit breaker для локального Ollama:
- После K подряд неудач — переходит в OPEN на M минут
- В состоянии OPEN запросы сразу идут к fallback провайдеру
- Логирование и уведомление владельца при смене провайдера
"""

import asyncio
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import httpx

log = logging.getLogger("ollama")


class ProviderType(Enum):
    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"


class LLMError(Exception):
    pass


# Backward compatibility
OllamaError = LLMError


class ProviderUnavailable(LLMError):
    pass


class ModelNotFound(LLMError):
    pass


class CircuitBreakerOpen(LLMError):
    pass


class CircuitBreaker:
    """Circuit breaker для LLM провайдера.

    States: CLOSED (normal) -> OPEN (failing) -> HALF_OPEN (testing)
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: float = 300.0,  # 5 минут
        half_open_max_calls: int = 1,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls

        self._state = "CLOSED"
        self._failure_count = 0
        self._last_failure_time: float = 0
        self._half_open_calls = 0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> str:
        return self._state

    def record_success(self) -> None:
        if self._state == "HALF_OPEN":
            self._half_open_calls += 1
            if self._half_open_calls >= self.half_open_max_calls:
                self._state = "CLOSED"
                self._failure_count = 0
                self._half_open_calls = 0
                log.info("Circuit breaker: HALF_OPEN -> CLOSED (recovered)")
        elif self._state == "CLOSED":
            self._failure_count = 0

    def record_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = time.time()

        if self._state == "CLOSED" and self._failure_count >= self.failure_threshold:
            self._state = "OPEN"
            log.warning(
                "Circuit breaker: CLOSED -> OPEN после %d подряд неудач",
                self.failure_threshold,
            )
        elif self._state == "HALF_OPEN":
            self._state = "OPEN"
            self._half_open_calls = 0
            log.warning("Circuit breaker: HALF_OPEN -> OPEN (тестовая неудача)")

    def can_execute(self) -> bool:
        if self._state == "CLOSED":
            return True
        if self._state == "OPEN":
            if time.time() - self._last_failure_time >= self.recovery_timeout:
                self._state = "HALF_OPEN"
                self._half_open_calls = 0
                log.info("Circuit breaker: OPEN -> HALF_OPEN (таймаут восстановления)")
                return True
            return False
        # HALF_OPEN
        return self._half_open_calls < self.half_open_max_calls


class LLMProvider(ABC):
    """Базовый интерфейс LLM провайдера."""

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system: str,
        temperature: float,
        num_ctx: int,
        json_mode: bool = False,
    ) -> str:
        pass

    @abstractmethod
    async def check_available(self) -> bool:
        pass

    @property
    @abstractmethod
    def provider_type(self) -> ProviderType:
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        pass


class OllamaProvider(LLMProvider):
    """Локальный Ollama провайдер."""

    def __init__(
        self,
        base_url: str,
        model: str,
        fallback_model: str,
        timeout: float = 120.0,
        concurrency: int = 2,
        external_client_ref: list = None,  # для backward compat тестов
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.fallback_model = fallback_model
        self.timeout = timeout
        self._external_client_ref = external_client_ref or [None]
        # Создаём клиент по умолчанию
        if self._external_client_ref[0] is None:
            self._external_client_ref[0] = httpx.AsyncClient(timeout=timeout)
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=3,
            recovery_timeout=300.0,
            half_open_max_calls=1,
        )

    @property
    def _client(self):
        """Прокси к внешнему клиенту (для backward compat тестов)."""
        return self._external_client_ref[0]

    @_client.setter
    def _client(self, value):
        self._external_client_ref[0] = value

    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.OLLAMA

    @property
    def name(self) -> str:
        return f"Ollama({self.base_url})"

    async def check_available(self) -> bool:
        try:
            resp = await self._client.get(f"{self.base_url}/api/tags")
            resp.raise_for_status()
            available = [m.get("name", "") for m in resp.json().get("models", [])]
            return any(
                self.model == n or n.startswith(self.model + ":") for n in available
            ) or (
                self.fallback_model
                and any(
                    self.fallback_model == n or n.startswith(self.fallback_model + ":")
                    for n in available
                )
            )
        except Exception:
            return False

    async def _get_model(self) -> str:
        """Возвращает доступную модель (основную или fallback)."""
        try:
            resp = await self._client.get(f"{self.base_url}/api/tags")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(
                f"Ollama недоступен: {exc}"
            ) from exc

        available = [m.get("name", "") for m in resp.json().get("models", [])]

        def _present(name: str) -> bool:
            if name in available:
                return True
            family = name.split(":")[0]
            return any(n.split(":")[0] == family for n in available)

        if _present(self.model):
            return self.model
        if self.fallback_model and _present(self.fallback_model):
            log.warning("Модель %s не найдена, используем запасную %s",
                       self.model, self.fallback_model)
            return self.fallback_model
        raise ProviderUnavailable(
            f"Не найдена ни модель {self.model}, ни запасная {self.fallback_model}"
        )

    async def generate(
        self,
        prompt: str,
        system: str,
        temperature: float,
        num_ctx: int,
        json_mode: bool = False,
        model: str = None,
    ) -> str:
        if not self._circuit_breaker.can_execute():
            raise CircuitBreakerOpen("Circuit breaker OPEN для Ollama")

        if model is None:
            model = await self._get_model()
        payload = {
            "model": model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "temperature": temperature,
            "options": {"num_ctx": num_ctx},
        }
        if json_mode:
            payload["format"] = "json"

        async with self._sem:
            try:
                resp = await self._client.post(
                    f"{self.base_url}/api/generate", json=payload
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                self._circuit_breaker.record_failure()
                raise ProviderUnavailable(f"Ошибка Ollama: {exc}") from exc

            self._circuit_breaker.record_success()
            return (resp.json().get("response") or "").strip()

    async def check_available(self) -> bool:
        return await self.check_available()

    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.OLLAMA

    @property
    def name(self) -> str:
        return f"Ollama({self.base_url})"

    def get_circuit_state(self) -> str:
        return self._circuit_breaker.state

    async def close(self) -> None:
        await self._client.aclose()


class OpenAICompatibleProvider(LLMProvider):
    """Любой OpenAI-совместимый API (OpenAI, OpenRouter, YandexGPT, vLLM, etc.)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 120.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.OPENAI_COMPATIBLE

    @property
    def name(self) -> str:
        return f"OpenAI-compat({self.base_url})"

    async def check_available(self) -> bool:
        try:
            # Пробуем простой запрос к /models или сразу генерацию
            resp = await self._client.get(f"{self.base_url}/models")
            return resp.status_code == 200
        except Exception:
            return False

    async def generate(
        self,
        prompt: str,
        system: str,
        temperature: float,
        num_ctx: int,
        json_mode: bool = False,
    ) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": min(num_ctx, 4096),
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            resp = await self._client.post(
                f"{self.base_url}/chat/completions", json=payload
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Ошибка OpenAI-compat API: {exc}") from exc
        except (KeyError, IndexError) as exc:
            raise ProviderUnavailable(f"Неверный формат ответа: {exc}") from exc

    async def check_available(self) -> bool:
        try:
            resp = await self._client.get(f"{self.base_url}/models")
            return resp.status_code == 200
        except Exception:
            return False

    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.OPENAI_COMPATIBLE

    @property
    def name(self) -> str:
        return f"OpenAI-compat({self.base_url})"

    async def close(self) -> None:
        await self._client.aclose()


def parse_json_response(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    raise LLMError(f"Не удалось разобрать JSON от модели: {text[:300]}")


# --- Промпты (как в оригинале) ---
SYSTEM_PROMPT = (
    "Ты — редактор новостей в Telegram-канале об играх. Перепиши новость в готовый "
    "пост для Telegram в формате, как в примере ниже.\n\n"
    "Пример поста:\n"
    "Lords of the Fallen получила русскую локализацию\n"
    "\n"
    "Это произошло в сегодняшнем патче вместе с другими улучшениями и изменениями.\n"
    "\n"
    "Кроме того, до 24 августа Lords of the Fallen можно купить со скидкой за "
    "3959₸/296₴/9,89$/9,89€. Игра недоступна в РФ, но её можно купить гифтом за 747₽.\n"
    "\n"
    "#LordsOfTheFallen\n\n"
    "Правила:\n"
    "- Каждый блок поста начинается с новой строки, между блоками — пустая строка.\n"
    "- Заголовок — короткий и ёмкий, по сути новости.\n"
    "- Второй абзац — суть новости в 1–2 предложениях.\n"
    "- Третий абзац (начинается ровно со слов «Кроме того, ») — ТОЛЬКО если в "
    "статье действительно есть дополнительные детали: цены, скидки, даты релиза, "
    "платформы, доступность. В этом единственном дополнительном абзаце должны "
    "быть собраны ВСЕ такие детали. Если их нет — не пиши этот абзац вовсе.\n"
    "- ВСЕГДА максимум три абзаца: заголовок, суть, и при необходимости один "
    "«Кроме того». Никаких абзацев после «Кроме того» — сразу хэштег.\n"
    "- В самом конце — один хэштег с названием игры или платформы (например "
    "#GTA6, #PlayStation). Хэштег обязателен в каждом посте, без исключений.\n"
    "- Не копируй текст источника дословно — переформулируй своими словами.\n"
    "- Обязательно сохраняй цифры из статьи: цены, даты, скидки.\n"
    "- Стиль живой, но нейтральный журналистский, без кликбейта, капслока и "
    "восклицаний.\n"
    "- Не добавляй слово «Источник», ссылки, пояснения и вступления — только сам пост."
)

GAME_NAME_PROMPT = (
    "Из заголовка новости об играх выдели название игры (или серии/франшизы). "
    "Ответь строго одним названием, как в официальном написании, без кавычек, "
    "точек и пояснений. Если в заголовке нет игры — ответь одним словом «нет»."
)

DEDUP_PROMPT = (
    "Два заголовка новостей об играх. Это одна и та же новость, только если "
    "они сообщают об одном и том же событии и одних и тех же фактах. "
    "Если это разные события или разные факты об одной игре — ответь «нет». "
    "Ответь строго одним словом: «да» или «нет»."
)

PROOFREAD_PROMPT = (
    "Ты — редактор. Перед тобой готовый пост для Telegram-канала об играх. "
    "Исправь в нём только орфографические и грамматические ошибки. Не меняй "
    "содержание, структуру, порядок абзацев и хэштеги. Не переписывай текст "
    "своими словами. Ответь только исправленным текстом поста целиком."
)

VIDEO_SCRIPT_PROMPT = """Ты — сценарист коротких новостных видео (вертикальный формат 9:16).
Пишешь на русском языке. Стиль: энергичный, понятный, без воды.

Правила:
- Начинай сразу с новости, без приветствий («Всем привет...» запрещено).
- Только факты из новости, ничего не выдумывай.
- Без чрезмерного кликбейта.
- Длина текста: 40–80 слов (примерно 20–45 секунд озвучки).
- Структура: крючок (hook) → что произошло → главная информация →
  почему это важно → короткое завершение.
- Предложения короткие, для устного произношения.

Всегда отвечай ТОЛЬКО JSON без пояснений в формате:
{"headline": "КОРОТКИЙ ЗАГОЛОВОК ДО 8 СЛОВ", "script": "Полный текст сценария"}"""


@dataclass
class LLMClientConfig:
    """Конфигурация LLM клиента из переменных окружения."""
    # Primary: Ollama
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
    ollama_fallback_model: str = "mistral"
    ollama_concurrency: int = 2
    ollama_timeout: float = 120.0

    # Fallback: OpenAI-compatible
    fallback_enabled: bool = False
    fallback_base_url: str = ""
    fallback_api_key: str = ""
    fallback_model: str = ""

    # Circuit breaker
    cb_failure_threshold: int = 3
    cb_recovery_timeout: float = 300.0

    # Notifier for alerts
    notifier = None

    @classmethod
    def from_env(cls) -> "LLMClientConfig":
        import os
        return cls(
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip(),
            ollama_model=os.getenv("OLLAMA_MODEL", "llama3.1:8b").strip(),
            ollama_fallback_model=os.getenv("OLLAMA_FALLBACK_MODEL", "mistral").strip(),
            ollama_concurrency=int(os.getenv("OLLAMA_CONCURRENCY", "2")),
            ollama_timeout=float(os.getenv("OLLAMA_TIMEOUT", "120.0")),
            fallback_enabled=os.getenv("LLM_FALLBACK_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on"),
            fallback_base_url=os.getenv("LLM_FALLBACK_BASE_URL", "").strip(),
            fallback_api_key=os.getenv("LLM_FALLBACK_API_KEY", "").strip(),
            fallback_model=os.getenv("LLM_FALLBACK_MODEL", "").strip(),
            cb_failure_threshold=int(os.getenv("LLM_CB_THRESHOLD", "3")),
            cb_recovery_timeout=float(os.getenv("LLM_CB_RECOVERY", "300.0")),
        )


class LLMClient:
    """Высокоуровневый клиент с поддержкой фолбэка и circuit breaker.

    Использование:
        client = LLMClient(config)
        model = await client.check()
        text = await client.rewrite(model, title, article_text)
    """

    def __init__(self, config: "LLMClientConfig", notifier=None):
        self.config = config
        self._notifier = notifier

        # Primary: Ollama
        self._primary = OllamaProvider(
            config.ollama_base_url,
            config.ollama_model,
            config.ollama_fallback_model,
            timeout=config.ollama_timeout,
            concurrency=config.ollama_concurrency,
        )
        self._primary._circuit_breaker.failure_threshold = config.cb_failure_threshold
        self._primary._circuit_breaker.recovery_timeout = config.cb_recovery_timeout

        # Fallback provider
        self._fallback = None
        if config.fallback_enabled and config.fallback_base_url and config.fallback_api_key:
            self._fallback = OpenAICompatibleProvider(
                config.fallback_base_url,
                config.fallback_api_key,
                config.fallback_model,
            )
            log.info("Fallback LLM провайдер настроен: %s", self._fallback.name)
        else:
            log.info("Fallback LLM провайдер НЕ настроен (LLM_FALLBACK_ENABLED=false или нет ключей)")

        self._current_provider = self._primary
        self._using_fallback = False

    async def check(self) -> str:
        """Проверяет доступность провайдеров, возвращает имя модели."""
        # Сначала пробуем primary
        if await self._primary.check_available():
            model = await self._primary._get_model()
            log.info("Primary LLM доступен: %s", model)
            return model

        # Пробуем fallback
        if self._fallback and await self._fallback.check_available():
            log.warning("Primary недоступен, переключение на fallback: %s", self._fallback.name)
            await self._switch_to_fallback("primary unavailable at startup")
            return self._fallback.model

        raise ProviderUnavailable("Ни primary, ни fallback LLM недоступны")

    async def _switch_to_fallback(self, reason: str) -> None:
        """Переключение на fallback с уведомлением."""
        if self._using_fallback:
            return
        if not self._fallback:
            raise ProviderUnavailable("Fallback не настроен")
        self._using_fallback = True
        log.warning("LLM: переключение на fallback (%s): %s", self._fallback.name, reason)
        if self.config.notifier:
            await self.config.notifier.notify(
                "llm_fallback",
                f"⚠️ LLM переключен на fallback ({self._fallback.name}): {reason}"
            )

    async def _switch_back_to_primary(self) -> None:
        if not self._using_fallback:
            return
        # Проверяем, восстановился ли primary
        if await self._primary.check_available():
            if self._primary._circuit_breaker.can_execute():
                self._using_fallback = False
                log.info("LLM: возврат к primary (Ollama восстановлен)")
                if self.config.notifier:
                    await self.config.notifier.notify(
                        "llm_primary",
                        "✅ LLM вернулся на primary (Ollama восстановлен)"
                    )

    def _get_provider(self) -> "LLMProvider":
        if self._using_fallback and self._fallback:
            return self._fallback
        return self._primary

    async def _generate_with_fallback(
        self,
        prompt: str,
        system: str,
        temperature: float,
        num_ctx: int,
        json_mode: bool = False,
    ) -> str:
        """Генерация с автоматическим фолбэком."""
        provider = self._get_provider()
        try:
            return await provider.generate(prompt, system, 0.7, 8192)
        except (ProviderUnavailable, CircuitBreakerOpen) as exc:
            if not self._using_fallback and self._fallback:
                await self._switch_to_fallback(str(exc))
                return await self._fallback.generate(prompt, system, 0.7, 8192)
            raise

    async def check(self) -> str:
        return await self._primary.check()

    async def rewrite(self, model: str, title: str, article_text: str) -> str:
        user_prompt = f"Заголовок: {title}\n\nТекст статьи:\n{article_text[:5000]}"
        return await self._generate_with_fallback(
            user_prompt, SYSTEM_PROMPT, 0.7, 8192
        )

    async def extract_game(self, model: str, title: str) -> Optional[str]:
        name = await self._generate_with_fallback(
            f"Заголовок: {title}", GAME_NAME_PROMPT, 0.0, 2048
        )
        name = name.strip('"').strip("«»").strip()
        if not name or name.lower() in ("нет", "no", "none", "-", "нет игры"):
            return None
        return name

    async def is_same_news(self, model: str, title_a: str, title_b: str) -> bool:
        response = await self._generate_with_fallback(
            f"Новость 1: {title_a}\nНовость 2: {title_b}",
            DEDUP_PROMPT, 0.0, 2048
        )
        return response.lower().startswith("да")

    async def proofread(self, model: str, text: str) -> str:
        fixed = await self._generate_with_fallback(
            text, PROOFREAD_PROMPT, 0.0, 4096
        )
        fixed = fixed.strip('"').strip("«»").strip()
        if not fixed:
            raise LLMError("LLM вернул пустой ответ на вычитку")
        return fixed

    async def generate_json(self, model: str, prompt: str, system: str,
                            temperature: float = 0.7, num_ctx: int = 4096) -> dict:
        raw = await self._generate_with_fallback(
            prompt, system, temperature, num_ctx, json_mode=True
        )
        return parse_json_response(raw)

    async def video_script(self, model: str, title: str, description: str,
                           source: str, category: str = "") -> dict:
        prompt = (
            "Напиши короткий сценарий видео по новости.\n\n"
            f"Заголовок: {title}\n"
            f"Описание: {(description or '')[:1200]}\n"
            f"Источник: {source}\n"
            f"Категория: {category}\n\n"
            "Верни JSON: headline — короткий броский заголовок для экрана "
            "(до 8 слов, без точки), script — полный текст сценария на "
            "русском для озвучки."
        )
        data = await self.generate_json(model, prompt, VIDEO_SCRIPT_PROMPT, 0.7)
        headline = str(data.get("headline") or "").strip().strip('"')
        script = str(data.get("script") or "").strip().strip('"')
        if not script:
            raise LLMError("Модель не вернула текст сценария")
        if not headline:
            headline = (title or script)[:60]
        return {"headline": headline[:80], "script": script}

    async def get_provider_status(self) -> dict:
        return {
            "current": self._current_provider.name if hasattr(self, '_current_provider') else "unknown",
            "using_fallback": self._using_fallback,
            "primary_state": self._primary.get_circuit_state(),
            "primary_available": await self._primary.check_available(),
            "fallback_configured": self._fallback is not None,
            "fallback_available": await self._fallback.check_available() if self._fallback else False,
        }

    async def close(self) -> None:
        await self._primary.close()
        if self._fallback:
            await self._fallback.close()

    def from_env(cls) -> "LLMClient":
        return cls(LLMClientConfig.from_env())


# Backward compatibility: старый интерфейс OllamaClient
class OllamaClient:
    """Обратная совместимость: старый интерфейс через OllamaProvider напрямую."""

    def __init__(
        self,
        base_url: str,
        model: str,
        fallback_model: str,
        timeout: float = 120.0,
        concurrency: int = 2,
    ):
        # Используем список-обертку для backward compat тестов
        self._external_client_ref = [None]
        self._provider = OllamaProvider(
            base_url, model, fallback_model, timeout=timeout, concurrency=concurrency,
            external_client_ref=self._external_client_ref
        )
        # Backward compat: expose provider's internal httpx client
        self._client = self._provider._client

    @property
    def _client(self):
        return self._provider._client

    @_client.setter
    def _client(self, value):
        self._provider._client = value

    async def check(self) -> str:
        return await self._provider.check()

    async def rewrite(self, model: str, title: str, article_text: str) -> str:
        return await self._provider.generate(
            f"Заголовок: {title}\n\nТекст статьи:\n{article_text[:5000]}",
            SYSTEM_PROMPT, 0.7, 8192, model=model
        )

    async def extract_game(self, model: str, title: str) -> Optional[str]:
        name = await self._provider.generate(
            f"Заголовок: {title}", GAME_NAME_PROMPT, 0.0, 2048, model=model
        )
        name = name.strip('"').strip("«»").strip()
        if not name or name.lower() in ("нет", "no", "none", "-", "нет игры"):
            return None
        return name

    async def is_same_news(self, model: str, title_a: str, title_b: str) -> bool:
        response = await self._provider.generate(
            f"Новость 1: {title_a}\nНовость 2: {title_b}",
            DEDUP_PROMPT, 0.0, 2048, model=model
        )
        return response.lower().startswith("да")

    async def proofread(self, model: str, text: str) -> str:
        fixed = await self._provider.generate(
            text, PROOFREAD_PROMPT, 0.0, 4096, model=model
        )
        fixed = fixed.strip('"').strip("«»").strip()
        if not fixed:
            raise LLMError("LLM вернул пустой ответ на вычитку")
        return fixed

    async def generate_json(self, model: str, prompt: str, system: str,
                            temperature: float = 0.7, num_ctx: int = 4096) -> dict:
        raw = await self._provider.generate(
            prompt, system, temperature, num_ctx, json_mode=True, model=model
        )
        return parse_json_response(raw)

    async def video_script(self, model: str, title: str, description: str,
                           source: str, category: str = "") -> dict:
        prompt = (
            "Напиши короткий сценарий видео по новости.\n\n"
            f"Заголовок: {title}\n"
            f"Описание: {(description or '')[:1200]}\n"
            f"Источник: {source}\n"
            f"Категория: {category}\n\n"
            "Верни JSON: headline — короткий броский заголовок для экрана "
            "(до 8 слов, без точки), script — полный текст сценария на "
            "русском для озвучки."
        )
        raw = await self._provider.generate(
            prompt, VIDEO_SCRIPT_PROMPT, 0.7, 4096, json_mode=True, model=model
        )
        data = parse_json_response(raw)
        headline = str(data.get("headline") or "").strip().strip('"')
        script = str(data.get("script") or "").strip().strip('"')
        if not script:
            raise LLMError("Модель не вернула текст сценария")
        if not headline:
            headline = (title or script)[:60]
        return {"headline": headline[:80], "script": script}

    async def close(self) -> None:
        await self._provider.close()

    @property
    def _sem(self):
        return self._provider._sem

    async def _generate(self, *args, **kwargs):
        return await self._provider.generate(*args, **kwargs)


from_env = LLMClient.from_env

__all__ = [
    "LLMClient",
    "LLMClientConfig",
    "OllamaClient",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "ProviderType",
    "ProviderUnavailable",
    "CircuitBreaker",
    "OllamaError",
    "LLMError",
    "parse_json_response",
    "from_env",
    "SYSTEM_PROMPT",
    "GAME_NAME_PROMPT",
    "DEDUP_PROMPT",
    "PROOFREAD_PROMPT",
    "VIDEO_SCRIPT_PROMPT",
]