"""Диалог с водителем в личке: регистрация, описание поломки, уточнения, фото, подтверждение."""

from __future__ import annotations

import logging
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from ..config import Config
from ..db import Supabase, UniqueViolation, utcnow_iso
from ..flow import ask_next, schedule_ask_next, send_to_dispatcher
from ..llm import Extractor
from ..logic import (
    CARGO,
    CATEGORIES,
    DUPLICATE_WINDOW_MINUTES,
    FIELD_CHOICES,
    MAX_PHOTOS,
    MIN_PHOTOS,
    STATUSES,
    URGENCIES,
    merge_missing,
    next_step,
    normalize_name,
    normalize_phone,
    normalize_plate,
    text_hash,
)

log = logging.getLogger(__name__)

router = Router(name="driver")
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

WELCOME = ("Опишите поломку одним сообщением, например:\n"
           "<i>Пробил колесо на М3, 240 км, фура гружёная</i>")


# ---------- регистрация ----------

async def ask_registration(message: Message, driver: dict) -> None:
    step = driver["reg_step"]
    if step == "name":
        await message.answer("Здравствуйте! Это бот для заявок о поломках.\nКак вас зовут? Напишите имя и фамилию.",
                             reply_markup=kb.REMOVE)
    elif step == "phone":
        await message.answer("Поделитесь номером телефона кнопкой ниже.", reply_markup=kb.share_contact())
    elif step == "plate":
        await message.answer("Напишите госномер машины, например <b>А123ВС77</b>.", reply_markup=kb.REMOVE)


async def handle_registration(message: Message, db: Supabase, driver: dict) -> None:
    step = driver["reg_step"]
    if step == "name":
        name = normalize_name(message.text or "")
        if not name:
            await message.answer("Напишите имя текстом, например: Иван Петров.")
            return
        driver = await db.update_driver(driver["id"], {"full_name": name, "reg_step": "phone"})
    elif step == "phone":
        contact = message.contact
        if not contact:
            await message.answer("Нажмите кнопку «Поделиться контактом» внизу экрана.", reply_markup=kb.share_contact())
            return
        if contact.user_id != message.from_user.id:
            await message.answer("Нужен ваш собственный контакт. Нажмите кнопку ниже.", reply_markup=kb.share_contact())
            return
        driver = await db.update_driver(driver["id"], {"phone": normalize_phone(contact.phone_number), "reg_step": "plate"})
    elif step == "plate":
        plate = normalize_plate(message.text or "")
        if not plate:
            await message.answer("Не похоже на госномер. Напишите, например: А123ВС77.")
            return
        driver = await db.update_driver(driver["id"], {"plate": plate, "reg_step": "done"})
        await message.answer(f"Готово, {driver['full_name']}! Машина {plate} зарегистрирована.\n\n" + WELCOME,
                             reply_markup=kb.REMOVE)
        return
    await ask_registration(message, driver)


async def get_or_create_driver(message: Message, db: Supabase) -> dict:
    return await db.get_driver(message.from_user.id) or await db.create_driver(message.from_user.id, message.chat.id)


# ---------- команды ----------

@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot, db: Supabase) -> None:
    driver = await get_or_create_driver(message, db)
    if driver["reg_step"] != "done":
        await ask_registration(message, driver)
        return
    draft = await db.get_draft(driver["id"])
    if draft:
        await message.answer(f"У вас есть незаконченная заявка №{draft['id']}. Продолжим. Отменить: /cancel")
        await ask_next(bot, db, message.chat.id, draft)
    else:
        await message.answer(WELCOME, reply_markup=kb.REMOVE)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, db: Supabase) -> None:
    driver = await db.get_driver(message.from_user.id)
    draft = driver and await db.get_draft(driver["id"])
    if not draft:
        await message.answer("Незаконченных заявок нет.", reply_markup=kb.REMOVE)
        return
    await db.update_request(draft["id"], {"status": "cancelled", "awaiting": None}, from_status=("draft",))
    await message.answer(f"Заявка №{draft['id']} отменена.\n\n" + WELCOME, reply_markup=kb.REMOVE)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(WELCOME + "\n\nБот уточнит недостающее, попросит фото (1-5) и покажет заявку перед отправкой.\n"
                         "/cancel отменить незаконченную заявку")


# ---------- сообщения ----------

@router.message()
async def on_message(message: Message, bot: Bot, db: Supabase, llm: Extractor) -> None:
    driver = await get_or_create_driver(message, db)
    if driver["reg_step"] != "done":
        await handle_registration(message, db, driver)
        return
    draft = await db.get_draft(driver["id"])

    if message.photo or _is_image_document(message):
        await handle_photo(message, bot, db, llm, driver, draft)
    elif message.location:
        await handle_location(message, bot, db, draft)
    elif message.text:
        if draft:
            await handle_clarification(message, bot, db, llm, draft)
        else:
            await create_request(message, bot, db, llm, driver, message.text)
    else:
        await message.answer("Я понимаю текст, фото и геолокацию.")


async def create_request(message: Message, bot: Bot, db: Supabase, llm: Extractor, driver: dict,
                         text: str, *, ask: bool = True) -> dict | None:
    text = text.strip()
    if len(text) < 3:
        await message.answer("Опишите поломку чуть подробнее.")
        return None
    h = text_hash(text)
    dup = await db.find_recent_duplicate(driver["id"], h, DUPLICATE_WINDOW_MINUTES)
    if dup:
        await message.answer(f"Такая заявка уже есть: №{dup['id']} ({STATUSES[dup['status']].lower()}). "
                             "Повторно не создаю. Если это другая поломка, опишите её иначе.")
        return None
    try:
        # сначала «застолбить» сообщение в БД: повторная доставка того же сообщения упрётся в unique
        request = await db.create_request({
            "driver_id": driver["id"],
            "source_chat_id": message.chat.id,
            "source_message_id": message.message_id,
            "text_hash": h,
            "raw_text": text,
        })
    except UniqueViolation:
        log.info("duplicate message %s/%s ignored", message.chat.id, message.message_id)
        return None

    extracted = await llm.extract(text)
    if not extracted["is_breakdown"]:
        await db.update_request(request["id"], {"status": "cancelled"})
        await message.answer("Не похоже на сообщение о поломке.\n\n" + WELCOME)
        return None
    fields = merge_missing(request, extracted)
    if fields:
        request = await db.update_request(request["id"], fields) or request
    await message.answer(_recognized_text(request))
    if ask:
        await ask_next(bot, db, message.chat.id, request)
    return request


def _recognized_text(request: dict) -> str:
    parts = []
    if request.get("category"):
        parts.append(CATEGORIES[request["category"]].lower())
    if request.get("urgency"):
        parts.append(URGENCIES[request["urgency"]].lower())
    if request.get("location"):
        parts.append(request["location"])
    if request.get("cargo"):
        parts.append(CARGO[request["cargo"]].lower())
    head = f"Заявка №{request['id']} создана."
    return head + (" Понял: " + ", ".join(parts) + "." if parts else "")


async def handle_clarification(message: Message, bot: Bot, db: Supabase, llm: Extractor, draft: dict) -> None:
    text = message.text.strip()
    if text_hash(text) == draft["text_hash"]:
        # то же сообщение прислали ещё раз: заявку не дублируем, просто повторяем вопрос
        await ask_next(bot, db, message.chat.id, draft)
        return
    photos = await db.list_photos(draft["id"])
    step = next_step(draft, len(photos))

    if step == "location":
        draft = await db.update_request(draft["id"], {"location": text[:200], "latitude": None, "longitude": None}) or draft
        await message.answer("Место записал.", reply_markup=kb.REMOVE)
    elif step in FIELD_CHOICES:
        extracted = await llm.extract(text, context=draft["raw_text"])
        updates = merge_missing(draft, extracted)
        updates["raw_text"] = (draft["raw_text"] + "\n" + text)[:2000]
        draft = await db.update_request(draft["id"], updates) or draft
        if not draft.get(step):
            await message.answer("Не понял ответ. Выберите вариант кнопкой.")
    elif step == "photos":
        await message.answer("Сейчас нужны фото поломки. Пришлите фото.")
    else:
        await message.answer("Заявка ждёт подтверждения: нажмите «Отправить» или «Исправить».")
    await ask_next(bot, db, message.chat.id, draft)


async def handle_location(message: Message, bot: Bot, db: Supabase, draft: dict | None) -> None:
    if not draft:
        await message.answer("Сначала опишите поломку текстом.\n\n" + WELCOME)
        return
    lat, lon = message.location.latitude, message.location.longitude
    fields = {"latitude": lat, "longitude": lon}
    if not draft.get("location") or draft.get("awaiting") == "location":
        fields["location"] = f"геолокация {lat:.5f}, {lon:.5f}"
    draft = await db.update_request(draft["id"], fields) or draft
    await message.answer("Геолокацию записал.", reply_markup=kb.REMOVE)
    await ask_next(bot, db, message.chat.id, draft)


def _is_image_document(message: Message) -> bool:
    doc = message.document
    return bool(doc and (doc.mime_type or "").startswith("image/"))


async def handle_photo(message: Message, bot: Bot, db: Supabase, llm: Extractor,
                       driver: dict, draft: dict | None) -> None:
    if not draft:
        if not message.caption:
            await message.answer("Сначала опишите поломку текстом, фото пришлёте следующим шагом.\n\n" + WELCOME)
            return
        # фото с подписью: подпись считаем описанием поломки
        draft = await create_request(message, bot, db, llm, driver, message.caption, ask=False)
        if not draft:
            return

    photos = await db.list_photos(draft["id"])
    if len(photos) >= MAX_PHOTOS:
        await message.answer(f"Уже есть {MAX_PHOTOS} фото, больше не нужно.")
        schedule_ask_next(bot, db, message.chat.id, draft["id"])
        return

    if message.photo:
        file = message.photo[-1]
        kind, content_type, ext = "photo", "image/jpeg", "jpg"
    else:
        file = message.document
        kind, content_type = "document", message.document.mime_type
        ext = content_type.split("/")[-1].replace("jpeg", "jpg")[:5]

    if any(p["tg_file_unique_id"] == file.file_unique_id for p in photos):
        schedule_ask_next(bot, db, message.chat.id, draft["id"])
        return
    try:
        buf = BytesIO()
        await bot.download(file.file_id, destination=buf)
        url = await db.upload_photo(f"{draft['id']}/{file.file_unique_id}.{ext}", buf.getvalue(), content_type)
    except Exception:
        log.exception("photo upload failed")
        await message.answer("Не удалось сохранить фото, попробуйте отправить его ещё раз.")
        return
    await db.add_photo(draft["id"], file.file_id, file.file_unique_id, url, kind)
    if len(photos) + 1 >= MAX_PHOTOS:
        await db.update_request(draft["id"], {"photos_done": True})
    schedule_ask_next(bot, db, message.chat.id, draft["id"])


# ---------- кнопки ----------

async def _own_draft(cb: CallbackQuery, db: Supabase, request_id: int) -> dict | None:
    request = await db.get_request(request_id)
    driver = request and await db.get_driver_by_id(request["driver_id"])
    if not driver or driver["tg_user_id"] != cb.from_user.id:
        await cb.answer("Заявка не найдена", show_alert=True)
        return None
    if request["status"] != "draft":
        await cb.answer(f"Заявка уже {STATUSES[request['status']].lower()}", show_alert=True)
        await _drop_markup(cb)
        return None
    return request


async def _drop_markup(cb: CallbackQuery, text: str | None = None) -> None:
    try:
        if text:
            await cb.message.edit_text(text, reply_markup=None)
        else:
            await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("set:"))
async def cb_set(cb: CallbackQuery, bot: Bot, db: Supabase) -> None:
    _, rid, field, value = cb.data.split(":")
    if field not in FIELD_CHOICES or value not in FIELD_CHOICES[field]:
        await cb.answer()
        return
    request = await _own_draft(cb, db, int(rid))
    if not request:
        return
    request = await db.update_request(request["id"], {field: value}) or request
    label = FIELD_CHOICES[field][value]
    await cb.answer(label)
    await _drop_markup(cb, f"{cb.message.html_text}\n<b>{label}</b>")
    await ask_next(bot, db, cb.message.chat.id, request)


@router.callback_query(F.data.startswith("photos_done:"))
async def cb_photos_done(cb: CallbackQuery, bot: Bot, db: Supabase) -> None:
    request = await _own_draft(cb, db, int(cb.data.split(":")[1]))
    if not request:
        return
    if len(await db.list_photos(request["id"])) < MIN_PHOTOS:
        await cb.answer("Нужно хотя бы одно фото", show_alert=True)
        return
    request = await db.update_request(request["id"], {"photos_done": True}) or request
    await cb.answer()
    await _drop_markup(cb)
    await ask_next(bot, db, cb.message.chat.id, request)


@router.callback_query(F.data.startswith("submit:"))
async def cb_submit(cb: CallbackQuery, bot: Bot, db: Supabase, config: Config) -> None:
    request = await _own_draft(cb, db, int(cb.data.split(":")[1]))
    if not request:
        return
    photos = await db.list_photos(request["id"])
    if next_step(request, len(photos)) != "confirm":
        await cb.answer("Заявка заполнена не до конца", show_alert=True)
        await ask_next(bot, db, cb.message.chat.id, request)
        return
    # условный update: при двойном нажатии вторая попытка ничего не найдёт
    submitted = await db.update_request(request["id"], {"status": "new", "awaiting": None, "submitted_at": utcnow_iso()},
                                        from_status=("draft",))
    if not submitted:
        await cb.answer("Заявка уже отправлена")
        await _drop_markup(cb)
        return
    try:
        await send_to_dispatcher(bot, db, config.dispatcher_chat_id, submitted)
    except Exception:
        log.exception("send to dispatcher failed")
        await db.update_request(request["id"], {"status": "draft", "awaiting": "confirm"}, from_status=("new",))
        await cb.answer("Не удалось отправить диспетчеру, попробуйте ещё раз", show_alert=True)
        return
    await cb.answer("Отправлено")
    await _drop_markup(cb, cb.message.html_text + f"\n\n✅ <b>Заявка №{request['id']} отправлена диспетчеру.</b>")
    await bot.send_message(cb.message.chat.id, "Сообщу, когда диспетчер примет заявку.\n"
                           "Новая поломка: просто опишите её сообщением.")


@router.callback_query(F.data.startswith("edit:"))
async def cb_edit(cb: CallbackQuery, db: Supabase) -> None:
    request = await _own_draft(cb, db, int(cb.data.split(":")[1]))
    if not request:
        return
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=kb.edit_menu(request["id"]))


@router.callback_query(F.data.startswith("fix:"))
async def cb_fix(cb: CallbackQuery, bot: Bot, db: Supabase) -> None:
    _, rid, field = cb.data.split(":")
    request = await _own_draft(cb, db, int(rid))
    if not request:
        return
    await cb.answer()
    if field == "back":
        await cb.message.edit_reply_markup(reply_markup=kb.confirm(request["id"]))
        return
    if field == "cancel":
        await db.update_request(request["id"], {"status": "cancelled", "awaiting": None}, from_status=("draft",))
        await _drop_markup(cb, cb.message.html_text + "\n\n🗑 <b>Заявка отменена.</b>")
        await bot.send_message(cb.message.chat.id, WELCOME)
        return
    if field == "photos":
        await db.delete_photos(request["id"])
        updates = {"photos_done": False}
    elif field == "location":
        updates = {"location": None, "latitude": None, "longitude": None}
    elif field in FIELD_CHOICES:
        updates = {field: None}
    else:
        return
    request = await db.update_request(request["id"], updates) or request
    await _drop_markup(cb)
    await ask_next(bot, db, cb.message.chat.id, request)
