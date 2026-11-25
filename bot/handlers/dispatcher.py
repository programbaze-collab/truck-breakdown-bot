"""Чат диспетчера: кнопки «Принял» / «Помощь выехала» и служебная команда /chatid."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from ..config import Config
from ..db import Supabase, utcnow_iso
from ..logic import DISPATCHER_TRANSITIONS, DRIVER_NOTIFICATIONS, STATUSES, dispatcher_card

log = logging.getLogger(__name__)

router = Router(name="dispatcher")


@router.message(Command("chatid"))
async def cmd_chatid(message: Message) -> None:
    """Помогает узнать DISPATCHER_CHAT_ID: отправить /chatid в группе диспетчера."""
    await message.answer(f"ID этого чата: <code>{message.chat.id}</code>")


@router.callback_query(F.data.startswith("st:"))
async def cb_status(cb: CallbackQuery, bot: Bot, db: Supabase, config: Config) -> None:
    if not cb.message or cb.message.chat.id != config.dispatcher_chat_id:
        await cb.answer("Недоступно", show_alert=True)
        return
    _, rid, new_status = cb.data.split(":")
    if new_status not in DISPATCHER_TRANSITIONS:
        await cb.answer()
        return

    who = cb.from_user.full_name + (f" (@{cb.from_user.username})" if cb.from_user.username else "")
    fields = {"status": new_status}
    if new_status == "accepted":
        fields |= {"accepted_by": who, "accepted_at": utcnow_iso()}
    else:
        fields |= {"help_sent_by": who, "help_sent_at": utcnow_iso()}

    # переход только из допустимого статуса: два диспетчера одновременно не сломают заявку
    request = await db.update_request(int(rid), fields, from_status=DISPATCHER_TRANSITIONS[new_status])
    changed = request is not None
    request = request or await db.get_request(int(rid))
    if not request:
        await cb.answer("Заявка не найдена", show_alert=True)
        return

    await _refresh_card(cb, db, request)
    if not changed:
        await cb.answer(f"Статус уже: {STATUSES[request['status']]}")
        return
    await cb.answer(STATUSES[new_status])

    driver = await db.get_driver_by_id(request["driver_id"])
    if driver:
        try:
            await bot.send_message(driver["tg_chat_id"], DRIVER_NOTIFICATIONS[new_status].format(id=request["id"]))
        except TelegramAPIError:
            log.warning("cannot notify driver %s", driver["tg_chat_id"])


async def _refresh_card(cb: CallbackQuery, db: Supabase, request: dict) -> None:
    driver = await db.get_driver_by_id(request["driver_id"]) or {}
    photos = await db.list_photos(request["id"])
    card = dispatcher_card(request, driver, [p["url"] for p in photos])
    markup = kb.dispatcher(request["id"], request["status"])
    try:
        if cb.message.photo:
            await cb.message.edit_caption(caption=card, reply_markup=markup)
        else:
            await cb.message.edit_text(card, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            log.warning("card refresh failed: %s", e)
