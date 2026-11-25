import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    bot_token: str
    dispatcher_chat_id: int
    openai_api_key: str
    openai_model: str
    supabase_url: str
    supabase_key: str
    supabase_bucket: str


def load_config() -> Config:
    load_dotenv()
    required = ["BOT_TOKEN", "DISPATCHER_CHAT_ID", "OPENAI_API_KEY", "SUPABASE_URL", "SUPABASE_SERVICE_KEY"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise SystemExit(f"Не заданы переменные окружения: {', '.join(missing)} (см. .env.example)")
    return Config(
        bot_token=os.environ["BOT_TOKEN"],
        dispatcher_chat_id=int(os.environ["DISPATCHER_CHAT_ID"]),
        openai_api_key=os.environ["OPENAI_API_KEY"],
        openai_model=os.getenv("OPENAI_MODEL") or "gpt-4o-mini",
        # частая ошибка: копируют адрес REST API вместо адреса проекта
        supabase_url=os.environ["SUPABASE_URL"].rstrip("/").removesuffix("/rest/v1"),
        supabase_key=os.environ["SUPABASE_SERVICE_KEY"],
        supabase_bucket=os.getenv("SUPABASE_BUCKET") or "breakdown-photos",
    )
