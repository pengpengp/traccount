"""emailnator.com provider — yields real @googlemail.com / @gmail.com addresses.

This is the ONLY known provider whose addresses are NOT blocked by Trae's
Login API (error_code 20116). Verified working 2026-07-18.

Architecture (Laravel + axios + XSRF-TOKEN):
    GET  /                         → sets XSRF-TOKEN + gmailnator_session cookies
    POST /generate-email           {"email": ["googleMail"]} → {"email": ["xxx@googlemail.com"]}
    POST /message-list             {"email": "..."}       → {"messageData": [{messageID, from, subject, time}]}
    POST /message-list             {"email":"...", "messageID": "..."} → HTML body (NOT JSON!)

Key gotchas:
    - Field name is `messageID` (PascalCase, capital D), NOT `messageId`.
    - Body endpoint returns text/html, NOT application/json.
    - X-XSRF-TOKEN header must be the URL-decoded cookie value.
    - Trae occasionally returns error_code 1023 ("Email is linked to another
      account") — caller should retry with a fresh address.

Email option strategy (verified 2026-07-18):
    - ``googleMail`` (DEFAULT): ``@googlemail.com`` — Google treats this as
      the SAME mailbox as ``@gmail.com``, but Trae treats it as a DIFFERENT
      registration address. So many base names that are 1023 on ``@gmail.com``
      are still fresh on ``@googlemail.com``. ~40% fresh rate.
    - ``dotGmail``: ``@gmail.com`` dot-trick — most base names already
      registered on Trae, ~0-10% fresh rate.
    - ``plusGmail``: ``name+random@gmail.com`` — base names pool is tiny
      and all already registered, ~0% fresh rate. NOT RECOMMENDED.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import string
from urllib.parse import unquote

import httpx

from ..config import get_proxy
from .base import Inbox, ProviderError

_log = logging.getLogger(__name__)

BASE = "https://www.emailnator.com"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)

# Trae email markers — only treat these as the OTP-bearing message.
_TRAE_FROM_HINT = "trae"
_TRAE_SUBJECT_HINT = "verification"

# Precise OTP extraction: Trae wraps the code in a span, e.g. `>698548</span`.
# This avoids false positives from CSS hex colors like `#111314`.
_PRECISE_OTP = re.compile(r">(\d{6})<")

# Cleanup patterns (kept here so the emailnator provider is self-contained
# and does not depend on the shared extract_otp heuristics being perfect).
_ZWSP = "\u200b\u200c\u200d\ufeff\u200e\u200f"
_STRIP_ZW = re.compile(rf"[{_ZWSP}]")
_STRIP_STYLE = re.compile(r"<style[^>]*>[\s\S]*?</style>", re.I)
_STRIP_SCRIPT = re.compile(r"<script[^>]*>[\s\S]*?</script>", re.I)
_STRIP_HEX = re.compile(r"#[0-9a-fA-F]{6}\b")
_STRIP_TAGS = re.compile(r"<[^>]+>")
_LOOSE_OTP = re.compile(r"(?<![#\d])(\d{6})(?!\d)")


def _best_otp(html: str) -> str:
    """Extract a 6-digit OTP from Trae email HTML.

    Critical: CSS hex colors like `#111314` look like 6-digit codes, so we
    MUST strip them BEFORE any digit-search heuristic. Order:
        1. Precise `>NNNNNN<` (Trae wraps OTP in a span).
        2. Cleaned loose match (strip style/script/hex/tags/zwsp first).
    """
    if not html:
        return ""
    m = _PRECISE_OTP.search(html)
    if m:
        return m.group(1)
    t = _STRIP_STYLE.sub(" ", html)
    t = _STRIP_SCRIPT.sub(" ", t)
    t = _STRIP_HEX.sub(" ", t)
    t = _STRIP_TAGS.sub(" ", t)
    t = _STRIP_ZW.sub("", t)
    m = _LOOSE_OTP.search(t)
    return m.group(1) if m else ""


class EmailNatorProvider:
    """Yields @googlemail.com / @gmail.com addresses via emailnator.com.

    The `prefix` argument to `create_inbox` is IGNORED — emailnator generates
    its own random email variant.

    Default email option is ``googleMail`` (``@googlemail.com``) because it
    has a much higher fresh-email rate than ``dotGmail`` (~40% vs ~0-10%).
    Both route to the same Gmail mailbox, but Trae treats ``@googlemail.com``
    as a distinct registration address from ``@gmail.com``.
    """

    name = "emailnator"

    def __init__(self, proxy: str | None = None, email_option: str = "googleMail"):
        """Initialize the provider.

        Args:
            proxy: proxy URL override; defaults to :func:`config.get_proxy`.
            email_option: emailnator option to use — ``"googleMail"`` (default,
                yields ``@googlemail.com``), ``"dotGmail"`` (``@gmail.com``
                dot-trick), or ``"plusGmail"`` (``name+rand@gmail.com``).
        """
        self._proxy = proxy if proxy is not None else get_proxy()
        self._client: httpx.AsyncClient | None = None
        self._xsrf_token: str = ""
        self._email_option = email_option

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": _UA, "Accept": "text/html,application/json"},
            timeout=30.0,
            proxy=self._proxy,
        )
        # Prime the session so Laravel sets XSRF-TOKEN + gmailnator_session.
        try:
            r = await self._client.get(f"{BASE}/")
            if r.status_code != 200:
                # Don't fail hard — some proxies strip cookies but still work.
                pass
        except httpx.HTTPError:
            pass
        self._xsrf_token = unquote(self._client.cookies.get("XSRF-TOKEN", ""))

    def _headers(self, extra: dict | None = None) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": BASE,
            "Referer": f"{BASE}/",
            "X-Requested-With": "XMLHttpRequest",
        }
        if self._xsrf_token:
            h["X-XSRF-TOKEN"] = self._xsrf_token
        if extra:
            h.update(extra)
        return h

    async def _generate_email(self, max_retries: int = 30) -> str:
        """Generate a fresh @googlemail.com (default) or @gmail.com address.

        Retries are mainly to dodge emailnator handing back an email that's
        already been registered on Trae (returns error_code 1023).
        """
        assert self._client is not None
        last_err: str = ""
        for _ in range(max_retries):
            # Refresh CSRF token if it rotated.
            tok = unquote(self._client.cookies.get("XSRF-TOKEN", ""))
            if tok:
                self._xsrf_token = tok
            try:
                r = await self._client.post(
                    f"{BASE}/generate-email",
                    json={"email": [self._email_option]},
                    headers=self._headers(),
                )
            except httpx.HTTPError as e:
                last_err = f"http: {e}"
                await asyncio.sleep(1.0)
                continue
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}: {r.text[:120]}"
                await asyncio.sleep(1.0)
                continue
            try:
                data = r.json()
            except ValueError:
                last_err = "non-JSON response"
                await asyncio.sleep(1.0)
                continue
            emails = data.get("email") or []
            if emails and "@" in emails[0]:
                return emails[0]
            last_err = "empty email list"
            await asyncio.sleep(0.5)
        raise ProviderError(f"emailnator: could not generate email after {max_retries} retries ({last_err})")

    async def create_inbox(self, prefix: str | None = None) -> Inbox:
        if self._client is None:
            raise ProviderError("emailnator: start() not called")
        address = await self._generate_email()
        return Inbox(
            address=address,
            token="",  # session-bound, no bearer token
            provider=self.name,
            meta={"xsrf_token": self._xsrf_token},
        )

    async def _fetch_message_list(self, address: str) -> list[dict]:
        assert self._client is not None
        try:
            r = await self._client.post(
                f"{BASE}/message-list",
                json={"email": address},
                headers=self._headers(),
            )
        except httpx.HTTPError:
            return []
        if r.status_code != 200:
            return []
        try:
            data = r.json()
        except ValueError:
            return []
        msgs = data.get("messageData") or []
        return [m for m in msgs if isinstance(m, dict)]

    async def _fetch_body(self, address: str, message_id: str) -> str:
        """Fetch the HTML body of a single message.

        NOTE: this endpoint returns text/html, NOT JSON.
        """
        assert self._client is not None
        try:
            r = await self._client.post(
                f"{BASE}/message-list",
                json={"email": address, "messageID": message_id},
                headers=self._headers({"Accept": "text/html, application/json"}),
            )
        except httpx.HTTPError:
            return ""
        if r.status_code != 200:
            return ""
        return r.text or ""

    def _is_trae_message(self, msg: dict) -> bool:
        from_field = str(msg.get("from", "")).lower()
        subject = str(msg.get("subject", "")).lower()
        return (_TRAE_FROM_HINT in from_field) or (_TRAE_SUBJECT_HINT in subject)

    async def wait_for_otp(
        self, inbox: Inbox, *, timeout: float = 180.0, poll: float = 4.0
    ) -> str:
        if self._client is None:
            raise ProviderError("emailnator: start() not called")
        deadline = asyncio.get_event_loop().time() + timeout
        seen: set[str] = set()
        address = inbox.address
        loop_time = asyncio.get_event_loop().time
        last_log = 0.0
        while loop_time() < deadline:
            msgs = await self._fetch_message_list(address)
            now = loop_time()
            # Log progress every ~15s so silent waits are diagnosable.
            if now - last_log >= 15.0:
                _log.info(
                    "emailnator: polling %s — %d message(s) so far, %d seen",
                    address, len(msgs), len(seen),
                )
                last_log = now
            for m in msgs:
                mid = str(m.get("messageID", "") or m.get("messageId", ""))
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                _log.info(
                    "emailnator: new message from=%r subject=%r",
                    m.get("from", ""), m.get("subject", ""),
                )
                if not self._is_trae_message(m):
                    continue
                body = await self._fetch_body(address, mid)
                code = _best_otp(body)
                if code:
                    return code
                _log.info("emailnator: Trae message had no OTP in body (len=%d)", len(body))
            await asyncio.sleep(poll)
        raise ProviderError(
            f"emailnator: no OTP within {timeout}s for {inbox.address}"
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        self._xsrf_token = ""
