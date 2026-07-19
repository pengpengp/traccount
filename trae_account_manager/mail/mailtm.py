"""mail.tm provider (free, no API key)."""
from __future__ import annotations

import asyncio
import random
import string

import httpx

from ..config import get_proxy
from .base import Inbox, ProviderError, extract_otp

API_BASE = "https://api.mail.tm"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class MailTmProvider:
    name = "mail.tm"

    def __init__(self, proxy: str | None = None):
        self._proxy = proxy if proxy is not None else get_proxy()
        self._client: httpx.AsyncClient | None = None
        self._domains: list[str] = []

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=30.0,
            proxy=self._proxy,
        )
        try:
            r = await self._client.get(f"{API_BASE}/domains?page=1")
            if r.status_code == 200:
                data = r.json()
                items = data.get("hydra:member", data) if isinstance(data, dict) else data
                self._domains = [d["domain"] for d in items if d.get("isActive", True)]
        except httpx.HTTPError:
            pass
        if not self._domains:
            self._domains = ["fexpost.com"]

    async def create_inbox(self, prefix: str | None = None) -> Inbox:
        assert self._client is not None
        if not self._domains:
            raise ProviderError("mail.tm: no domains available")
        user = prefix or "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        domain = random.choice(self._domains)
        address = f"{user}@{domain}"
        password = "".join(random.choices(string.ascii_letters + string.digits, k=16))

        r = await self._client.post(
            f"{API_BASE}/accounts", json={"address": address, "password": password}
        )
        if r.status_code not in (200, 201):
            raise ProviderError(f"mail.tm create account HTTP {r.status_code}: {r.text[:200]}")

        r = await self._client.post(
            f"{API_BASE}/token", json={"address": address, "password": password}
        )
        if r.status_code != 200:
            raise ProviderError(f"mail.tm token HTTP {r.status_code}")
        token = r.json().get("token", "")
        if not token:
            raise ProviderError("mail.tm: empty token")
        return Inbox(
            address=address, token=token, provider=self.name,
            meta={"password": password},
        )

    async def _fetch_messages(self, inbox: Inbox) -> list[dict]:
        assert self._client is not None
        r = await self._client.get(
            f"{API_BASE}/messages?page=1",
            headers={"Authorization": f"Bearer {inbox.token}"},
        )
        if r.status_code != 200:
            return []
        data = r.json()
        return data.get("hydra:member", data) if isinstance(data, dict) else data

    async def _fetch_body(self, inbox: Inbox, msg_id: str) -> str:
        assert self._client is not None
        r = await self._client.get(
            f"{API_BASE}/messages/{msg_id}",
            headers={"Authorization": f"Bearer {inbox.token}"},
        )
        if r.status_code != 200:
            return ""
        data = r.json()
        body = data.get("text", "") or data.get("html", "")
        if isinstance(body, list):
            body = " ".join(str(b) for b in body)
        return str(body)

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
                mid = str(m.get("id"))
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                body = await self._fetch_body(inbox, mid)
                code = (
                    extract_otp(body)
                    or extract_otp(m.get("subject", ""))
                    or extract_otp(m.get("intro", ""))
                )
                if code:
                    return code
            await asyncio.sleep(poll)
        raise ProviderError(f"mail.tm: no OTP within {timeout}s for {inbox.address}")

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
