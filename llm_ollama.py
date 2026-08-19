import asyncio
import json
import logging
import re

import httpx

log = logging.getLogger("ollama")

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


def parse_json_response(text: str) -> dict:
    """Надёжно извлекает JSON из ответа модели."""
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
    raise OllamaError(f"Не удалось разобрать JSON от модели: {text[:300]}")


class OllamaError(Exception):
    pass


class OllamaNotRunning(OllamaError):
    pass


class OllamaModelMissing(OllamaError):
    pass


class OllamaClient:
    def __init__(self, base_url: str, model: str, fallback_model: str,
                 timeout: float = 120.0, concurrency: int = 2):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.fallback_model = fallback_model
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)
        # Отдельный от сетевого параллелизма семафор: локальная LLM обрабатывает
        # генерации практически последовательно (одна GPU/CPU-инференс-очередь),
        # поэтому лимит не поднимаем до сетевого уровня, иначе запросы к Ollama
        # просто копятся у неё в очереди, пока сеть уже свободна.
        self._sem = asyncio.Semaphore(max(1, concurrency))

    async def check(self) -> str:
        """Проверяет, что ollama serve запущен и нужная модель скачана.

        Возвращает имя модели, которую будем использовать (основную или запасную).
        При проблемах поднимает понятное исключение.
        """
        try:
            resp = await self._client.get(f"{self.base_url}/api/tags")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise OllamaNotRunning(
                "Ollama недоступен по адресу %s (%s). "
                "Запустите `ollama serve` (или службу ollama) и попробуйте снова."
                % (self.base_url, exc)
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
            log.warning(
                "Модель %s не найдена, используем запасную %s",
                self.model, self.fallback_model,
            )
            return self.fallback_model
        raise OllamaModelMissing(
            "Не найдена ни модель %s, ни запасная %s. "
            "Скачайте модель командой: `ollama pull %s`" % (self.model, self.fallback_model, self.model)
        )

    async def _generate(self, model: str, prompt: str, system: str,
                        temperature: float, num_ctx: int,
                        json_mode: bool = False) -> str:
        """Базовый запрос к /api/generate с общим семафором и обработкой ошибок."""
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
                resp = await self._client.post(f"{self.base_url}/api/generate", json=payload)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise OllamaError(f"Ошибка запроса к Ollama: {exc}") from exc
            return (resp.json().get("response") or "").strip()

    async def rewrite(self, model: str, title: str, article_text: str) -> str:
        """Переписывает новость в пост для Telegram через локальную LLM."""
        user_prompt = f"Заголовок: {title}\n\nТекст статьи:\n{article_text[:5000]}"
        text = await self._generate(
            model, user_prompt, SYSTEM_PROMPT, 0.7, 8192
        )
        text = text.strip('"').strip("«»").strip()
        if not text:
            raise OllamaError("Ollama вернул пустой ответ")
        return text

    async def extract_game(self, model: str, title: str) -> str | None:
        """Выделяет название игры из заголовка через LLM.

        Возвращает None, если игры в заголовке нет или модель не ответила.
        """
        name = await self._generate(
            model, f"Заголовок: {title}", GAME_NAME_PROMPT, 0.0, 2048
        )
        name = name.strip('"').strip("«»").strip()
        if not name or name.lower() in ("нет", "no", "none", "-", "нет игры"):
            return None
        return name

    async def is_same_news(self, model: str, title_a: str, title_b: str) -> bool:
        """Одна и та же новость в двух заголовках? (LLM, ответ «да»/«нет»)."""
        response = await self._generate(
            model,
            f"Новость 1: {title_a}\nНовость 2: {title_b}",
            DEDUP_PROMPT,
            0.0,
            2048,
        )
        return response.lower().startswith("да")

    async def proofread(self, model: str, text: str) -> str:
        """Вычитка поста: исправляет орфографию/грамматику без правки содержания."""
        fixed = await self._generate(
            model, text, PROOFREAD_PROMPT, 0.0, 4096
        )
        fixed = fixed.strip('"').strip("«»").strip()
        if not fixed:
            raise OllamaError("Ollama вернул пустой ответ на вычитку")
        return fixed

    async def generate_json(self, model: str, prompt: str, system: str,
                            temperature: float = 0.7, num_ctx: int = 4096) -> dict:
        """Запрос в JSON-режиме (Ollama format=json) с надёжным разбором."""
        raw = await self._generate(
            model, prompt, system, temperature, num_ctx, json_mode=True
        )
        return parse_json_response(raw)

    async def video_script(self, model: str, title: str, description: str,
                           source: str, category: str = "") -> dict:
        """Генерирует сценарий видео и короткий заголовок.

        Возвращает {'headline': str, 'script': str}.
        """
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
            raise OllamaError("Модель не вернула текст сценария")
        if not headline:
            headline = (title or script)[:60]
        return {"headline": headline[:80], "script": script}

    async def close(self) -> None:
        await self._client.aclose()