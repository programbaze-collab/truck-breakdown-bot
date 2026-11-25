"""Разбор свободного текста водителя через OpenAI (structured outputs)."""

from __future__ import annotations

import json
import logging

from openai import AsyncOpenAI

from .logic import CARGO, CATEGORIES, URGENCIES, normalize_extraction

log = logging.getLogger(__name__)

SYSTEM_PROMPT = f"""Ты разбираешь сообщения водителей грузовиков о поломках для диспетчерской.
Извлеки из текста поля. Если поле в тексте явно не указано и не следует из него однозначно, верни null. Не выдумывай.

category: {", ".join(f"{k} ({v})" for k, v in CATEGORIES.items())}.
  Прокол, шина, колесо -> wheel. Не заводится, глохнет, перегрев, течёт масло, турбина -> engine.
  Авария, столкновение -> accident. Аккумулятор, проводка, фары, стартер -> electrics.
  Поломка понятна, но не подходит ни под одну категорию -> other.
urgency: {", ".join(f"{k} ({v})" for k, v in URGENCIES.items())}.
  stuck: водитель прямо пишет, что стоит и не может ехать (стою, встал, на обочине).
  need_help: водитель прямо просит помощь или эвакуатор, но не пишет, что стоит.
  can_drive: водитель пишет, что может ехать или доедет сам.
  Сам факт поломки срочность не определяет: если водитель про срочность ничего не написал, верни null.
location: место как написал водитель (трасса, километр, город, ориентир), кратко. Без места -> null.
cargo: loaded (гружёный, с грузом, полный) или empty (пустой, порожний, без груза).

is_breakdown: true, если сообщение про поломку, неисправность или ДТП машины; иначе false и остальные поля null."""

SCHEMA = {
    "name": "breakdown",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["is_breakdown", "category", "urgency", "location", "cargo"],
        "properties": {
            "is_breakdown": {"type": "boolean"},
            "category": {"type": ["string", "null"], "enum": [*CATEGORIES, None]},
            "urgency": {"type": ["string", "null"], "enum": [*URGENCIES, None]},
            "location": {"type": ["string", "null"]},
            "cargo": {"type": ["string", "null"], "enum": [*CARGO, None]},
        },
    },
}


class Extractor:
    def __init__(self, api_key: str, model: str):
        self.client = AsyncOpenAI(api_key=api_key, timeout=30, max_retries=2)
        self.model = model

    async def extract(self, text: str, context: str | None = None) -> dict:
        """Поля заявки + is_breakdown. При ошибке API все поля None: бот тогда задаст все вопросы сам."""
        user = text if not context else f"Ранее водитель написал: {context}\n\nНовое сообщение: {text}"
        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                temperature=0,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
                response_format={"type": "json_schema", "json_schema": SCHEMA},
            )
            data = json.loads(resp.choices[0].message.content or "{}")
        except Exception:
            log.exception("OpenAI extraction failed")
            data = {"is_breakdown": True}  # без ИИ продолжаем: бот просто задаст все вопросы
        return normalize_extraction(data)
