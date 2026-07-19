"""DeepMailsProvider v3: robust response-interception approach.

Strategy:
1. Open deepmails.org main page (Cloudflare Turnstile-protected).
2. Intercept BOTH requests and responses for ``/get-messages`` so we
   can capture (email, code) from the request body AND any incoming
   messages from the response — without depending on JS execution
   context (which gets destroyed when the page auto-navigates).
3. The page periodically auto-polls ``/get-messages`` to refresh its UI,
   so we don't need to trigger fetches ourselves — we just listen.
4. To trigger an extra poll when needed, we can click the page's refresh
   button or call ``page.reload()`` (preserves cookies).
5. To read an individual email body, open ``/mail/view/{Code}`` in a
   fresh tab and capture the raw HTTP response body via
   ``page.expect_response()``.
6. Extract the OTP from the raw HTML by stripping ``<style>``/``<script>``
   blocks first to avoid CSS hex colours.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Request,
    Response,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)

from ..config import get_proxy
from .base import Inbox, ProviderError, extract_otp

log = logging.getLogger(__name__)

SITE = "https://www.deepmails.org/"
VIEW_URL_FMT = "https://www.deepmails.org/mail/view/{code}"
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
# deepmails.org domain itself is sometimes blocked by Trae — avoid it
_BLOCKED_DOMAINS = {"deepmails.org"}
_MAX_INBOX_ATTEMPTS = 5


class DeepMailsProvider:
    name = "deepmails"

    def __init__(self, headless: bool = True, proxy: str | None = None):
        self._headless = headless
        self._proxy = proxy if proxy is not None else get_proxy()
        self._pw = None
        self._browser: Browser | None = None

    async def start(self) -> None:
        if self._browser is not None:
            return
        self._pw = await async_playwright().start()
        launch_args = {"headless": self._headless}
        if self._proxy:
            launch_args["proxy"] = {"server": self._proxy}
        self._browser = await self._pw.chromium.launch(**launch_args)

    async def _new_context(self):
        assert self._browser is not None
        return await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

    @staticmethod
    def _is_blocked_email(email: str) -> bool:
        if not email or "@" not in email:
            return True
        domain = email.rsplit("@", 1)[-1].lower()
        return domain in _BLOCKED_DOMAINS

    async def create_inbox(self, prefix: str | None = None) -> Inbox:
        """Open deepmails.org, capture (email, code) from intercepted
        ``/get-messages`` request. Retry until a non-blocked domain is
        assigned.
        """
        last_err: Exception | None = None
        for attempt in range(_MAX_INBOX_ATTEMPTS):
            context = await self._new_context()
            page = await context.new_page()
            captured: dict[str, Any] = {
                "code": "",
                "email": "",
                "messages": [],  # list of message dicts from intercepted responses
            }

            async def on_request(req: Request) -> None:
                if "/get-messages" in req.url and req.method == "POST":
                    try:
                        body = req.post_data
                        if body:
                            data = json.loads(body)
                            if data.get("code") and not captured["code"]:
                                captured["code"] = data["code"]
                                captured["email"] = data.get("email", "")
                    except Exception:  # noqa: BLE001
                        pass

            async def on_response(resp: Response) -> None:
                if "/get-messages" in resp.url and resp.request.method == "POST":
                    try:
                        data = await resp.json()
                        if data and data.get("success"):
                            emails = data.get("emails") or []
                            if isinstance(emails, dict):
                                emails = list(emails.values())
                            if isinstance(emails, list):
                                for m in emails:
                                    if m and m not in captured["messages"]:
                                        captured["messages"].append(m)
                    except Exception:  # noqa: BLE001
                        pass

            # page.on handlers must be sync; wrap async work in tasks
            page.on("request", lambda r: asyncio.create_task(on_request(r)))
            page.on("response", lambda r: asyncio.create_task(on_response(r)))

            try:
                await page.goto(SITE, wait_until="domcontentloaded", timeout=30_000)
            except PlaywrightTimeout:
                pass  # page might still be loading

            # Wait for code to be captured (with extra fallback to mailCodeGlobal)
            deadline = asyncio.get_event_loop().time() + 25
            while asyncio.get_event_loop().time() < deadline:
                if captured["code"] and captured["email"]:
                    break
                # Try mailCodeGlobal as fallback
                if not captured["code"]:
                    try:
                        val = await page.evaluate(
                            "() => typeof window.mailCodeGlobal !== 'undefined' "
                            "? window.mailCodeGlobal : null"
                        )
                        if val and isinstance(val, str):
                            captured["code"] = val
                    except PlaywrightError:
                        pass
                # Try cookie fallback for email
                if not captured["email"]:
                    try:
                        cookies = await context.cookies()
                        for c in cookies:
                            if c["name"] == "temp_mail":
                                captured["email"] = c["value"].replace("%40", "@")
                                break
                    except PlaywrightError:
                        pass
                await asyncio.sleep(0.5)

            email = captured["email"]
            code = captured["code"]

            if not email or not code:
                last_err = ProviderError(
                    f"deepmails: failed to capture email/code "
                    f"(email={email!r}, code={code[:20] if code else 'empty'!r})"
                )
                await self._safe_close_context(context)
                continue

            if self._is_blocked_email(email):
                log.info("deepmails: domain blocked for %s, retrying", email)
                await self._safe_close_context(context)
                continue

            log.info("deepmails inbox: %s (code=%s...)", email, code[:16])
            return Inbox(
                address=email, token=code, provider=self.name,
                meta={
                    "page": page,
                    "context": context,
                    "captured": captured,  # shared dict updated by listeners
                },
            )

        raise last_err or ProviderError("deepmails: max inbox attempts exceeded")

    async def _safe_close_context(self, context: BrowserContext) -> None:
        try:
            await context.close()
        except PlaywrightError:
            pass

    # ------------------------------------------------------------------
    # Message fetching
    # ------------------------------------------------------------------

    async def _fetch_messages(self, inbox: Inbox) -> list[dict]:
        """Return messages currently seen for this inbox.

        Strategy: prefer the response-intercepted ``captured['messages']``
        list (always-updated by the page's own auto-poll). As a fallback,
        also issue a manual fetch via evaluate (which may fail if the
        page has navigated — that's fine, we still have intercepted ones).
        """
        captured: dict = inbox.meta.get("captured", {})
        # Try a manual evaluate first to trigger a fresh poll
        try:
            page: Page = inbox.meta["page"]
            result = await page.evaluate("""async (args) => {
                try {
                    const resp = await fetch('/get-messages', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({email: args.email, code: args.code}),
                    });
                    return await resp.json();
                } catch (e) { return {success: false, error: String(e)}; }
            }""", {"email": inbox.address, "code": inbox.token})
            if result and result.get("success"):
                emails = result.get("emails") or []
                if isinstance(emails, dict):
                    emails = list(emails.values())
                if isinstance(emails, list):
                    # Merge into captured (dedup by Code)
                    existing_codes = {
                        str(m.get("Code") or m.get("code") or m.get("Subject"))
                        for m in captured.get("messages", [])
                    }
                    for m in emails:
                        mcode = str(m.get("Code") or m.get("code") or m.get("Subject"))
                        if m and mcode not in existing_codes:
                            captured.setdefault("messages", []).append(m)
                            existing_codes.add(mcode)
        except PlaywrightError as e:
            log.debug("deepmails evaluate poll failed: %s", e)

        return list(captured.get("messages", []))

    async def _fetch_message_html(self, inbox: Inbox, msg_code: str) -> str:
        """Fetch raw HTML of /mail/view/{msg_code} in a fresh tab.

        Opens a new page in the same browser context so navigation does
        not disturb the main deepmails page. Returns the HTTP response
        body so we get the email content even when JS rendering leaves
        ``.email-content`` empty in headless mode.
        """
        context: BrowserContext | None = inbox.meta.get("context")
        if not context or not msg_code:
            return ""
        page = await context.new_page()
        try:
            view_url = VIEW_URL_FMT.format(code=msg_code)
            try:
                async with page.expect_response(
                    lambda r: "/mail/view/" in r.url and r.status == 200,
                    timeout=15_000,
                ) as resp_info:
                    try:
                        await page.goto(view_url, wait_until="domcontentloaded", timeout=15_000)
                    except PlaywrightTimeout:
                        pass
                try:
                    resp = await resp_info.value
                    return await resp.text()
                except PlaywrightError:
                    return await page.content()
            except PlaywrightTimeout:
                return await page.content()
        except PlaywrightError as e:
            log.debug("deepmails _fetch_message_html failed: %s", e)
            return ""
        finally:
            try:
                await page.close()
            except PlaywrightError:
                pass

    # ------------------------------------------------------------------
    # OTP extraction
    # ------------------------------------------------------------------

    _SCRIPT_STYLE_RE = re.compile(
        r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
    )
    _TAG_RE = re.compile(r"<[^>]+>")
    _CSS_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}\b")
    _STANDALONE_DIGIT_RE = re.compile(r"(?<![\w-])\d{6}(?![\w-])")
    # Common CSS colour values to ignore
    _CSS_BLOCKLIST = {
        "000000", "ffffff", "333333", "666666", "999999",
        "111111", "222222", "444444", "555555", "777777", "888888",
        "aaaaaa", "bbbbbb", "cccccc", "dddddd", "eeeeee",
        "ff0000", "00ff00", "0000ff", "008000", "ff69b4",
    }

    def _extract_otp_from_html(self, html: str) -> str | None:
        """Strip style/script tags then search for a 6-digit OTP."""
        if not html:
            return None
        # Remove <style> and <script> blocks entirely
        cleaned = self._SCRIPT_STYLE_RE.sub(" ", html)
        # Strip remaining tags
        text = self._TAG_RE.sub(" ", cleaned)
        # Decode common HTML entities
        text = (text.replace("&nbsp;", " ")
                    .replace("&amp;", "&")
                    .replace("&lt;", "<")
                    .replace("&gt;", ">")
                    .replace("&#39;", "'")
                    .replace("&quot;", '"'))
        # Collect all 6-digit standalone candidates
        candidates = self._STANDALONE_DIGIT_RE.findall(text)
        # Prefer candidates that appear near OTP/verification/code keywords
        for kw_pat in (
            re.compile(r"(verification|verify|otp|code|password|密码|验证|验证码)[^0-9]{0,80}(\d{6})", re.IGNORECASE),
            re.compile(r"(\d{6})[^0-9]{0,80}(verification|verify|otp|code|password)", re.IGNORECASE),
        ):
            m = kw_pat.search(text)
            if m:
                grp = m.group(2 if m.lastindex == 2 else 1)
                if grp and grp.isdigit() and len(grp) == 6:
                    return grp
        # Fallback: pick the first candidate that's not a known CSS colour
        css_hexes = set(self._CSS_HEX_RE.findall(cleaned))
        for cand in candidates:
            if f"#{cand}" not in css_hexes and cand not in self._CSS_BLOCKLIST:
                return cand
        return None

    async def wait_for_otp(
        self, inbox: Inbox, *, timeout: float = 180.0, poll: float = 4.0
    ) -> str:
        """Poll for new messages and extract OTP from the first one we find."""
        deadline = asyncio.get_event_loop().time() + timeout
        seen: set[str] = set()
        # Seed `seen` with any messages already captured (pre-existing inbox
        # state — these were there before we sent the OTP, so skip them).
        try:
            for m in await self._fetch_messages(inbox):
                mid = str(m.get("Code") or m.get("code") or m.get("Id") or m.get("Subject") or "")
                if mid:
                    seen.add(mid)
            if seen:
                log.debug("deepmails: pre-existing %d messages skipped", len(seen))
        except Exception:  # noqa: BLE001
            pass

        last_msg_count = 0
        while asyncio.get_event_loop().time() < deadline:
            try:
                msgs = await self._fetch_messages(inbox)
            except Exception as e:  # noqa: BLE001
                log.debug("deepmails poll error: %s", e)
                msgs = []

            if len(msgs) > last_msg_count:
                last_msg_count = len(msgs)
                log.info("deepmails: %d message(s) in inbox", len(msgs))

            for m in msgs:
                mid = str(m.get("Code") or m.get("code")
                          or m.get("Id") or m.get("id")
                          or m.get("Subject") or m.get("subject") or "")
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                subject = str(m.get("Subject") or m.get("subject") or "")
                log.info("deepmails: new message subject=%r", subject)
                # First try the subject
                code = extract_otp(subject)
                if code:
                    log.info("deepmails OTP found in subject for %s: %s",
                             inbox.address, code)
                    return code
                # Otherwise fetch the email body HTML
                msg_code = str(m.get("Code") or m.get("code") or "")
                if msg_code:
                    html = await self._fetch_message_html(inbox, msg_code)
                    if html:
                        code = self._extract_otp_from_html(html)
                        if code:
                            log.info("deepmails OTP found in body for %s: %s",
                                     inbox.address, code)
                            return code
                        else:
                            log.debug("deepmails message %s: no OTP in HTML (len=%d)",
                                      msg_code, len(html))
            await asyncio.sleep(poll)
        raise ProviderError(
            f"deepmails: no OTP within {timeout}s for {inbox.address}"
        )

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw:
            await self._pw.stop()
            self._pw = None

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def release_inbox(self, inbox: Inbox) -> None:
        ctx = inbox.meta.get("context")
        if ctx:
            try:
                await ctx.close()
            except PlaywrightError:
                pass
            inbox.meta["context"] = None
            inbox.meta["page"] = None
