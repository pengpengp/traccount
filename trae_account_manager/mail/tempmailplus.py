"""TempMailPlus provider (mailto.plus and 8 alias domains).

All 9 domains confirmed to receive Trae OTP emails (verified 2026-07-15):
  mailto.plus, fexpost.com, fexbox.org, mailbox.in.ua, rover.info,
  chitthi.in, fextemp.com, any.pink, merepost.com
"""
from __future__ import annotations

import asyncio
import random
import re
import string

import httpx

from ..config import get_proxy
from .base import Inbox, ProviderError, extract_otp

API_BASE = "https://tempmail.plus"
# 9 domains confirmed to receive Trae OTP (tested 2026-07-15)
_DOMAINS = [
    "mailto.plus",
    "fexpost.com",
    "fexbox.org",
    "mailbox.in.ua",
    "rover.info",
    "chitthi.in",
    "fextemp.com",
    "any.pink",
    "merepost.com",
]
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "sec-ch-ua": '"Not)A;Brand";v="99", "HeadlessChrome";v="138", "Chromium";v="138"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}
# Trae OTP body embeds zero-width chars between digits to defeat scrapers.
_ZWSP = "\u200b\u200c\u200d\ufeff"
_STRIP_ZW = re.compile(rf"[{_ZWSP}]")
_OTP_RE = re.compile(r"\b(\d{6})\b")


class TempMailPlusProvider:
    name = "tempmailplus"

    def __init__(self, proxy: str | None = None):
        self._proxy = proxy if proxy is not None else get_proxy()
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            headers=_HEADERS, timeout=30.0, proxy=self._proxy
        )

    async def create_inbox(self, prefix: str | None = None) -> Inbox:
        assert self._client is not None
        user = prefix or "".join(
            random.choices(string.ascii_lowercase + string.digits, k=10)
        )
        domain = random.choice(_DOMAINS)
        address = f"{user}@{domain}"
        # No account creation required — any address @domain is live.
        # Store epin="" (empty) so the same Inbox can be polled later.
        return Inbox(address=address, token="", provider=self.name,
                      meta={"epin": ""})

    async def _fetch_list(self, inbox: Inbox, first_id: int = 0) -> list[dict]:
        assert self._client is not None
        epin = inbox.meta.get("epin", "")
        r = await self._client.get(
            f"{API_BASE}/api/mails",
            params={"email": inbox.address, "first_id": first_id, "epin": epin},
        )
        if r.status_code != 200:
            return []
        data = r.json()
        if not isinstance(data, dict):
            return []
        return data.get("mail_list", []) or []

    async def _fetch_body(self, inbox: Inbox, mail_id: str) -> str:
        assert self._client is not None
        epin = inbox.meta.get("epin", "")
        r = await self._client.get(
            f"{API_BASE}/api/mails/{mail_id}",
            params={"email": inbox.address, "epin": epin},
        )
        if r.status_code != 200:
            return ""
        data = r.json()
        if not isinstance(data, dict):
            return ""
        # body may be in 'text', 'html', 'body', or 'body_text'
        body = (
            data.get("body_text")
            or data.get("text")
            or data.get("body")
            or data.get("html")
            or ""
        )
        if isinstance(body, list):
            body = " ".join(str(b) for b in body)
        return str(body)

    def _extract_otp(self, text: str) -> str | None:
        """Strip zero-width chars then match 6-digit code."""
        if not text:
            return None
        cleaned = _STRIP_ZW.sub("", text)
        m = _OTP_RE.search(cleaned)
        if m:
            return m.group(1)
        return extract_otp(cleaned)

    async def wait_for_otp(
        self, inbox: Inbox, *, timeout: float = 180.0, poll: float = 4.0
    ) -> str:
        assert self._client is not None
        deadline = asyncio.get_event_loop().time() + timeout
        seen: set[str] = set()
        while asyncio.get_event_loop().time() < deadline:
            try:
                msgs = await self._fetch_list(inbox)
            except httpx.HTTPError:
                msgs = []
            for m in msgs:
                mid = str(m.get("mail_id") or m.get("id") or "")
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                # Try subject first (cheap, no extra request)
                subj = m.get("subject", "") or ""
                code = self._extract_otp(subj)
                if not code:
                    body = await self._fetch_body(inbox, mid)
                    code = self._extract_otp(body)
                if code:
                    return code
            await asyncio.sleep(poll)
        raise ProviderError(
            f"tempmailplus: no OTP within {timeout}s for {inbox.address}"
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
