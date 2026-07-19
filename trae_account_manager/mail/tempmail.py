"""tempmail.lol provider (free, no API key)."""
from __future__ import annotations

import asyncio

import httpx

from ..config import get_proxy
from .base import Inbox, ProviderError, extract_otp

API_BASE = "https://api.tempmail.lol"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class TempMailLolProvider:
    name = "tempmail.lol"

    def __init__(self, proxy: str | None = None):
        self._proxy = proxy if proxy is not None else get_proxy()
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=30.0,
            proxy=self._proxy,
        )

    async def create_inbox(self, prefix: str | None = None) -> Inbox:
        assert self._client is not None
        r = await self._client.get(f"{API_BASE}/generate")
        if r.status_code != 200:
            raise ProviderError(f"tempmail.lol generate HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError as e:
            raise ProviderError(f"tempmail.lol: non-JSON response: {r.text[:120]}") from e
        address = data.get("address", "")
        token = data.get("token", "")
        if not address or not token:
            raise ProviderError("tempmail.lol: incomplete generate response")
        return Inbox(address=address, token=token, provider=self.name)

    async def _fetch_messages(self, inbox: Inbox) -> list[dict]:
        assert self._client is not None
        r = await self._client.get(f"{API_BASE}/auth/{inbox.token}")
        if r.status_code != 200:
            return []
        data = r.json()
        return data if isinstance(data, list) else []

    async def wait_for_otp(
        self, inbox: Inbox, *, timeout: float = 180.0, poll: float = 4.0
    ) -> str:
        assert self._client is not None
        deadline = asyncio.get_event_loop().time() + timeout
        seen: set[str] = set()
        while asyncio.get_event_loop().time() < deadline:
            try:
                msgs = await self._fetch_messages(inbox)
            except httpx.HTTPError:
                msgs = []
            for m in msgs:
                mid = str(m.get("date", "")) + str(m.get("subject", ""))
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                body = m.get("body", "") or m.get("html", "")
                code = (
                    extract_otp(body)
                    or extract_otp(m.get("subject", ""))
                )
                if code:
                    return code
            await asyncio.sleep(poll)
        raise ProviderError(f"tempmail.lol: no OTP within {timeout}s for {inbox.address}")

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
