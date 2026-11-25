import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from .config import load_config
from .db import Supabase
from .handlers import dispatcher, driver
from .llm import Extractor
from .middlewares import PerUserLock


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()

    db = Supabase(config.supabase_url, config.supabase_key, config.supabase_bucket)
    await db.start()
    llm = Extractor(config.openai_api_key, config.openai_model)
    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))

    dp = Dispatcher(db=db, llm=llm, config=config)
    lock = PerUserLock()
    dp.message.middleware(lock)
    dp.callback_query.middleware(lock)
    dp.include_routers(dispatcher.router, driver.router)

    await bot.set_my_commands([
        BotCommand(command="start", description="Начать"),
        BotCommand(command="cancel", description="Отменить незаконченную заявку"),
        BotCommand(command="help", description="Как пользоваться"),
    ])
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
