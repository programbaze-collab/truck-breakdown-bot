"""Тонкий асинхронный клиент Supabase: PostgREST для таблиц и Storage для фото."""

from __future__ import annotations

import json as json_lib
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp


class DBError(Exception):
    def __init__(self, status: int, code: str | None, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code


class UniqueViolation(DBError):
    pass


class Supabase:
    def __init__(self, url: str, key: str, bucket: str):
        self.url = url
        self.bucket = bucket
        self._headers = {"apikey": key}
        # legacy-ключи (JWT) передаются ещё и как Bearer; новые sb_secret_ достаточно в apikey
        if key.startswith("eyJ"):
            self._headers["Authorization"] = f"Bearer {key}"
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(headers=self._headers, timeout=aiohttp.ClientTimeout(total=30))

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def _rest(self, method: str, table: str, *, params: dict | None = None,
                    json: Any = None, prefer: str | None = None) -> list[dict]:
        assert self._session, "Supabase.start() не вызван"
        headers = {"Prefer": prefer} if prefer else {}
        async with self._session.request(method, f"{self.url}/rest/v1/{table}",
                                         params=params, json=json, headers=headers) as resp:
            raw = await resp.text()
            try:
                body = json_lib.loads(raw) if raw.strip() else None
            except ValueError:
                body = {"message": raw[:300]}
            if resp.status >= 400:
                body = body if isinstance(body, dict) else {}
                cls = UniqueViolation if body.get("code") == "23505" else DBError
                raise cls(resp.status, body.get("code"), body.get("message") or str(body))
            return body if isinstance(body, list) else []

    async def _select_one(self, table: str, **filters: Any) -> dict | None:
        params = {"select": "*", "limit": "1", **{k: f"eq.{v}" for k, v in filters.items()}}
        rows = await self._rest("GET", table, params=params)
        return rows[0] if rows else None

    async def _update(self, table: str, row_id: int, fields: dict, extra: dict | None = None) -> list[dict]:
        fields = {**fields, "updated_at": utcnow_iso()} if table != "request_photos" else fields
        params = {"id": f"eq.{row_id}", **(extra or {})}
        return await self._rest("PATCH", table, params=params, json=fields, prefer="return=representation")

    # --- водители ---

    async def get_driver(self, tg_user_id: int) -> dict | None:
        return await self._select_one("drivers", tg_user_id=tg_user_id)

    async def get_driver_by_id(self, driver_id: int) -> dict | None:
        return await self._select_one("drivers", id=driver_id)

    async def create_driver(self, tg_user_id: int, tg_chat_id: int) -> dict:
        try:
            rows = await self._rest("POST", "drivers", json={"tg_user_id": tg_user_id, "tg_chat_id": tg_chat_id},
                                    prefer="return=representation")
            return rows[0]
        except UniqueViolation:
            driver = await self.get_driver(tg_user_id)
            assert driver
            return driver

    async def update_driver(self, driver_id: int, fields: dict) -> dict:
        return (await self._update("drivers", driver_id, fields))[0]

    # --- заявки ---

    async def get_request(self, request_id: int) -> dict | None:
        return await self._select_one("requests", id=request_id)

    async def get_draft(self, driver_id: int) -> dict | None:
        return await self._select_one("requests", driver_id=driver_id, status="draft")

    async def create_request(self, fields: dict) -> dict:
        """Бросает UniqueViolation, если заявка из этого сообщения уже есть."""
        rows = await self._rest("POST", "requests", json=fields, prefer="return=representation")
        return rows[0]

    async def update_request(self, request_id: int, fields: dict, *, from_status: tuple[str, ...] | None = None) -> dict | None:
        """Обновление с условием на текущий статус. None, если статус уже другой (кто-то успел раньше)."""
        extra = {"status": f"in.({','.join(from_status)})"} if from_status else None
        rows = await self._update("requests", request_id, fields, extra)
        return rows[0] if rows else None

    async def find_recent_duplicate(self, driver_id: int, text_hash: str, minutes: int) -> dict | None:
        since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = await self._rest("GET", "requests", params={
            "select": "*",
            "driver_id": f"eq.{driver_id}",
            "text_hash": f"eq.{text_hash}",
            "status": "in.(draft,new,accepted,help_sent)",
            "created_at": f"gte.{since}",
            "order": "id.desc",
            "limit": "1",
        })
        return rows[0] if rows else None

    # --- фото ---

    async def list_photos(self, request_id: int) -> list[dict]:
        return await self._rest("GET", "request_photos", params={
            "select": "*", "request_id": f"eq.{request_id}", "order": "id.asc"})

    async def add_photo(self, request_id: int, file_id: str, file_unique_id: str, url: str, kind: str) -> bool:
        """False, если это фото уже прикреплено к заявке."""
        try:
            await self._rest("POST", "request_photos", json={
                "request_id": request_id, "tg_file_id": file_id,
                "tg_file_unique_id": file_unique_id, "url": url, "kind": kind})
            return True
        except UniqueViolation:
            return False

    async def delete_photos(self, request_id: int) -> None:
        await self._rest("DELETE", "request_photos", params={"request_id": f"eq.{request_id}"})

    async def upload_photo(self, path: str, data: bytes, content_type: str = "image/jpeg") -> str:
        assert self._session
        async with self._session.post(f"{self.url}/storage/v1/object/{self.bucket}/{path}", data=data,
                                      headers={"Content-Type": content_type, "x-upsert": "true"}) as resp:
            if resp.status >= 400:
                raise DBError(resp.status, None, await resp.text())
        return f"{self.url}/storage/v1/object/public/{self.bucket}/{path}"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
