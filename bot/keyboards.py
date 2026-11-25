from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from .logic import FIELD_CHOICES

REMOVE = ReplyKeyboardRemove()


def share_contact() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться контактом", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )


def share_location() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Отправить геолокацию", request_location=True)]],
        resize_keyboard=True, one_time_keyboard=True,
        input_field_placeholder="Например: М4, 512 км",
    )


def choices(field: str, request_id: int) -> InlineKeyboardMarkup:
    """Кнопки для поля с вариантами. callback: set:<id>:<field>:<value>"""
    items = list(FIELD_CHOICES[field].items())
    rows, row = [], []
    for value, label in items:
        row.append(InlineKeyboardButton(text=label, callback_data=f"set:{request_id}:{field}:{value}"))
        if len(row) == (2 if len(items) > 3 else 3):
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def photos_done(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Готово, фото больше нет", callback_data=f"photos_done:{request_id}")]])


def confirm(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Отправить", callback_data=f"submit:{request_id}"),
        InlineKeyboardButton(text="✏️ Исправить", callback_data=f"edit:{request_id}"),
    ]])


def edit_menu(request_id: int) -> InlineKeyboardMarkup:
    def b(text: str, field: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=f"fix:{request_id}:{field}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [b("Поломка", "category"), b("Срочность", "urgency")],
        [b("Место", "location"), b("Груз", "cargo")],
        [b("Фото заново", "photos")],
        [b("⬅️ Назад", "back"), b("🗑 Отменить заявку", "cancel")],
    ])


def dispatcher(request_id: int, status: str) -> InlineKeyboardMarkup | None:
    buttons = []
    if status == "new":
        buttons.append(InlineKeyboardButton(text="👍 Принял", callback_data=f"st:{request_id}:accepted"))
    if status in ("new", "accepted"):
        buttons.append(InlineKeyboardButton(text="🚐 Помощь выехала", callback_data=f"st:{request_id}:help_sent"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None
