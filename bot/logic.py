"""Чистая логика без сети и Telegram: справочники, нормализация, порядок вопросов, тексты карточек.

Модуль не зависит от сторонних пакетов, поэтому покрыт тестами в tests/test_logic.py.
"""

from __future__ import annotations

import hashlib
import html
import re
from typing import Any

MAX_PHOTOS = 5
MIN_PHOTOS = 1
DUPLICATE_WINDOW_MINUTES = 30

CATEGORIES = {
    "wheel": "Колесо",
    "engine": "Двигатель",
    "brakes": "Тормоза",
    "electrics": "Электрика",
    "accident": "ДТП",
    "other": "Другое",
}
URGENCIES = {
    "can_drive": "Могу ехать",
    "need_help": "Нужна помощь",
    "stuck": "Стою на трассе",
}
CARGO = {
    "loaded": "Гружёный",
    "empty": "Пустой",
}
STATUSES = {
    "draft": "Черновик",
    "new": "Новая",
    "accepted": "Принята диспетчером",
    "help_sent": "Помощь выехала",
    "cancelled": "Отменена",
}

# порядок уточняющих вопросов
FIELDS_ORDER = ("category", "urgency", "location", "cargo")
REQUEST_FIELDS = set(FIELDS_ORDER)
FIELD_CHOICES = {"category": CATEGORIES, "urgency": URGENCIES, "cargo": CARGO}

QUESTIONS = {
    "category": "Что сломалось? Выберите категорию:",
    "urgency": "Насколько срочно?",
    "location": "Где вы находитесь? Напишите трассу и километр или город, либо отправьте геолокацию кнопкой.",
    "cargo": "Машина гружёная или пустая?",
    "photos": f"Пришлите фото поломки: от {MIN_PHOTOS} до {MAX_PHOTOS} штук.",
}

# статусы, которые может выставить диспетчер, и из каких состояний
DISPATCHER_TRANSITIONS = {
    "accepted": ("new",),
    "help_sent": ("new", "accepted"),
}
DRIVER_NOTIFICATIONS = {
    "accepted": "Диспетчер принял вашу заявку №{id}. Ожидайте связи.",
    "help_sent": "По заявке №{id} помощь выехала.",
}


def text_hash(text: str) -> str:
    """Хэш текста без учёта регистра, пробелов и пунктуации: так ловим повторно присланное сообщение."""
    normalized = re.sub(r"[\W_]+", " ", text.lower().replace("ё", "е")).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()


def normalize_extraction(data: dict[str, Any] | None) -> dict[str, Any]:
    """Оставляет из ответа модели только допустимые значения, остальное превращает в None."""
    data = data or {}
    result: dict[str, Any] = {}
    for field, choices in FIELD_CHOICES.items():
        value = data.get(field)
        result[field] = value if isinstance(value, str) and value in choices else None
    location = data.get("location")
    if isinstance(location, str):
        location = location.strip()
        if location.lower() in {"", "null", "none", "неизвестно", "не указано"}:
            location = None
    else:
        location = None
    result["location"] = location[:200] if location else None
    result["is_breakdown"] = data.get("is_breakdown") is not False
    return result


def merge_missing(request: dict[str, Any], extracted: dict[str, Any]) -> dict[str, Any]:
    """Возвращает только те поля, которых в заявке ещё нет, а модель их нашла."""
    return {f: v for f, v in extracted.items() if f in REQUEST_FIELDS and v and not request.get(f)}


def next_step(request: dict[str, Any], photo_count: int) -> str:
    """Следующий шаг диалога: имя поля, 'photos' или 'confirm'."""
    for field in FIELDS_ORDER:
        if not request.get(field):
            return field
    if photo_count < MIN_PHOTOS:
        return "photos"
    if not request.get("photos_done") and photo_count < MAX_PHOTOS:
        return "photos"
    return "confirm"


LATIN_TO_CYRILLIC = str.maketrans("ABEKMHOPCTYX", "АВЕКМНОРСТУХ")


def normalize_plate(text: str) -> str | None:
    """Госномер: верхний регистр, латиница-двойники в кириллицу, без пробелов и дефисов."""
    plate = re.sub(r"[\s\-]+", "", text.upper()).translate(LATIN_TO_CYRILLIC)
    if not re.fullmatch(r"[А-ЯЁA-Z0-9]{5,12}", plate) or not re.search(r"\d", plate):
        return None
    return plate


def normalize_name(text: str) -> str | None:
    name = " ".join(text.split())
    if not 2 <= len(name) <= 100 or not re.search(r"[A-Za-zА-Яа-яЁё]", name):
        return None
    return name


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    return "+" + digits if digits else phone


def _label(mapping: dict[str, str], value: str | None) -> str:
    return mapping.get(value or "", "не указано")


def _location_html(request: dict[str, Any]) -> str:
    text = html.escape(request.get("location") or "не указано")
    lat, lon = request.get("latitude"), request.get("longitude")
    if lat is not None and lon is not None:
        text += f' (<a href="https://maps.google.com/?q={lat},{lon}">карта</a>)'
    return text


def driver_summary(request: dict[str, Any], photo_count: int) -> str:
    """Сводка для водителя перед отправкой."""
    return (
        "<b>Проверьте заявку</b>\n\n"
        f"Поломка: {_label(CATEGORIES, request.get('category'))}\n"
        f"Срочность: {_label(URGENCIES, request.get('urgency'))}\n"
        f"Место: {_location_html(request)}\n"
        f"Груз: {_label(CARGO, request.get('cargo'))}\n"
        f"Фото: {photo_count} шт.\n\n"
        f"Описание: {html.escape(_short(request.get('raw_text') or '', 600))}"
    )


def dispatcher_card(request: dict[str, Any], driver: dict[str, Any], photo_urls: list[str]) -> str:
    """Карточка заявки в чате диспетчера. Укладывается в лимит подписи к фото (1024 символа)."""
    urgency = request.get("urgency")
    mark = {"stuck": "🔴", "need_help": "🟠", "can_drive": "🟢"}.get(urgency or "", "⚪")
    lines = [
        f"{mark} <b>Заявка №{request['id']}</b>",
        f"Статус: <b>{STATUSES.get(request.get('status', ''), request.get('status'))}</b>",
        "",
        f"Водитель: {html.escape(driver.get('full_name') or '')}",
        f"Телефон: {html.escape(driver.get('phone') or '')}",
        f"Госномер: {html.escape(driver.get('plate') or '')}",
        "",
        f"Поломка: {_label(CATEGORIES, request.get('category'))}",
        f"Срочность: {_label(URGENCIES, urgency)}",
        f"Место: {_location_html(request)}",
        f"Груз: {_label(CARGO, request.get('cargo'))}",
    ]
    if photo_urls:
        links = ", ".join(f'<a href="{html.escape(u, quote=True)}">{i}</a>' for i, u in enumerate(photo_urls, 1))
        lines.append(f"Фото: {links}")
    if request.get("accepted_by"):
        lines.append(f"Принял: {html.escape(request['accepted_by'])}")
    if request.get("help_sent_by"):
        lines.append(f"Помощь отправил: {html.escape(request['help_sent_by'])}")
    head = "\n".join(lines)
    budget = 1000 - len(head)
    description = _short(request.get("raw_text") or "", max(budget, 50))
    return head + "\n\n" + html.escape(description)


def _short(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
