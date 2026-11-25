"""Общие шаги диалога: задать следующий вопрос, отправить заявку диспетчеру."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.types import InputMediaPhoto, Message, ReplyParameters

from . import keyboards as kb
from .db import Supabase
from .logic import FIELD_CHOICES, MAX_PHOTOS, QUESTIONS, dispatcher_card, driver_summary, next_step

log = logging.getLogger(__name__)


async def ask_next(bot: Bot, db: Supabase, chat_id: int, request: dict) -> str:
    """Задаёт ровно один следующий вопрос (или показывает сводку). Возвращает шаг."""
    photos = await db.list_photos(request["id"])
    step = next_step(request, len(photos))
    if request.get("awaiting") != step:
        request = await db.update_request(request["id"], {"awaiting": step}) or request
    rid = request["id"]

    if step in FIELD_CHOICES:
        await bot.send_message(chat_id, QUESTIONS[step], reply_markup=kb.choices(step, rid))
    elif step == "location":
        await bot.send_message(chat_id, QUESTIONS[step], reply_markup=kb.share_location())
    elif step == "photos":
        if photos:
            await bot.send_message(
                chat_id,
                f"Фото получено: {len(photos)} из {MAX_PHOTOS}. Пришлите ещё или нажмите «Готово».",
                reply_markup=kb.photos_done(rid),
            )
        else:
            await bot.send_message(chat_id, QUESTIONS["photos"], reply_markup=kb.REMOVE)
    else:
        await bot.send_message(chat_id, driver_summary(request, len(photos)), reply_markup=kb.confirm(rid))
    return step


# Альбом из 5 фото приходит пятью сообщениями. Чтобы не отвечать на каждое,
# вопрос после фото откладывается и отправляется один раз, когда фото перестали приходить.
_prompt_tasks: dict[int, asyncio.Task] = {}


def schedule_ask_next(bot: Bot, db: Supabase, chat_id: int, request_id: int, delay: float = 1.5) -> None:
    old = _prompt_tasks.get(chat_id)
    if old and not old.done():
        old.cancel()

    async def run() -> None:
        await asyncio.sleep(delay)
        if _prompt_tasks.get(chat_id) is task:
            del _prompt_tasks[chat_id]
        request = await db.get_request(request_id)
        if request and request["status"] == "draft":
            await ask_next(bot, db, chat_id, request)

    task = asyncio.create_task(run())
    _prompt_tasks[chat_id] = task


async def send_to_dispatcher(bot: Bot, db: Supabase, chat_id: int, request: dict) -> Message:
    driver = await db.get_driver_by_id(request["driver_id"]) or {}
    photos = await db.list_photos(request["id"])
    card = dispatcher_card(request, driver, [p["url"] for p in photos])
    markup = kb.dispatcher(request["id"], request["status"])

    def media(p: dict) -> str:
        # фото пересылаем по file_id (быстро), файлы-картинки по публичной ссылке Supabase
        return p["tg_file_id"] if p["kind"] == "photo" else p["url"]

    if len(photos) == 1:
        msg = await bot.send_photo(chat_id, media(photos[0]), caption=card, reply_markup=markup)
    else:
        album = await bot.send_media_group(chat_id, [InputMediaPhoto(media=media(p)) for p in photos])
        msg = await bot.send_message(chat_id, card, reply_markup=markup,
                                     reply_parameters=ReplyParameters(message_id=album[0].message_id))
    await db.update_request(request["id"], {"dispatcher_chat_id": chat_id, "dispatcher_message_id": msg.message_id})
    return msg
