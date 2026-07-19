"""Automated Trae account registration via direct API.

Uses Bytedance passport APIs directly (no browser) for speed and reliability:
  1. ``POST /passport/web/email/send_code/``  → trigger OTP email
  2. poll temp-mail provider for OTP
  3. ``POST /passport/web/email/register_verify_login/`` → create account
  4. ``POST /cloudide/api/v3/trae/Login`` → domain reputation check
  5. ``POST /cloudide/api/v3/common/GetUserToken`` → JWT

Returns (and persists) :class:`Account` records with an AES-encrypted
``secrets_blob`` containing the JWT (if obtained), session cookies, and password.

.. note::
   As of 2026-07-18, the default mail pool uses :class:`EmailNatorProvider`
   first, which yields ``@gmail.com`` addresses via Gmail dot-trick. These
   are NOT on Trae's domain blocklist (verified: Trae Login returns
   ``FirstLogin=true``). The full register → Trae Login → GetUserToken flow
   is expected to succeed end-to-end. Other providers in the pool
   (TempMailPlus, MailTm, etc.) ARE blocked at Trae Login (error_code
   20116) and are kept only as OTP-receiving fallbacks for diagnostics.
"""
from __future__ import annotations

import asyncio
import logging
import random
import string
from dataclasses import dataclass, field
from typing import Optional

import httpx

from . import db, vault
from .config import get_proxy
from .mail.base import EmailProvider, Inbox, ProviderError
from .mail.pool import MailPool, default_pool
from .models import Account
from .trae_api import parse_jwt

log = logging.getLogger(__name__)

# --- API endpoints ---------------------------------------------------------
PASSPORT_BASE = "https://ug-normal.trae.ai"
API_SG = "https://api-sg-central.trae.ai"
AID = "677332"
SEND_CODE_URL = f"{PASSPORT_BASE}/passport/web/email/send_code/"
REGISTER_URL = f"{PASSPORT_BASE}/passport/web/email/register_verify_login/"
TRAE_LOGIN_URL = f"{PASSPORT_BASE}/cloudide/api/v3/trae/Login"
GET_USER_TOKEN_URL = f"{API_SG}/cloudide/api/v3/common/GetUserToken"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)

# How long to wait for the OTP email (seconds).
OTP_WAIT_TIMEOUT = 180.0

# How many times to retry send_code with a fresh email when the previous
# one was already registered (Trae error_code 1023). emailnator gives
# distinct base Gmails, so retrying usually succeeds within a few tries.
EMAIL_LINKED_RETRY_MAX = 8


# ---------------------------------------------------------------------------
@dataclass
class RegistrationResult:
    success: bool
    account: Optional[Account] = None
    email: str = ""
    error: str = ""
    raw_token_response: dict = field(default_factory=dict)
    cookies: list = field(default_factory=list)


def _gen_password(length: int = 14) -> str:
    """Strong-enough password satisfying Trae's complexity laws."""
    pool = string.ascii_letters + string.digits + "!@#$%^&*"
    must = [
        random.choice(string.ascii_lowercase),
        random.choice(string.ascii_uppercase),
        random.choice(string.digits),
        random.choice("!@#$%^&*"),
    ]
    rest = random.choices(pool, k=length - len(must))
    pwd = must + rest
    random.shuffle(pwd)
    return "".join(pwd)


# ---------------------------------------------------------------------------
class SendCodeRetryableError(ProviderError):
    """Raised when send_code returns a transient/risk error.

    Caller should obtain a fresh email and retry. Encompasses:
        - error_code 1023 "Email is linked to another account" — base Gmail
          already registered, retry with a different dot-trick variant.
        - error_code 17   "Couldn't log in. Try again." — risk control /
          rate limit, retry after a short backoff.
        - error_code 1206 "Maximum number of attempts reached" — per-IP
          send_code rate limit, retry after a LONG (30s+) backoff.
    """


# Keep the legacy name as an alias so external callers don't break.
EmailAlreadyLinkedError = SendCodeRetryableError


# ---------------------------------------------------------------------------
async def _send_code(client: httpx.AsyncClient, email: str) -> str:
    """Trigger OTP email. Returns email_ticket on success.

    Raises:
        SendCodeRetryableError: if Trae returns error_code 1023 or 17 (caller
            should obtain a new email and retry, optionally with backoff).
        ProviderError: for any other send_code failure.
    """
    r = await client.post(
        SEND_CODE_URL,
        data={
            "aid": AID,
            "email": email,
            "type": "1",
            "password": "",
            "email_logic_type": "2",
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://www.trae.ai",
            "Referer": "https://www.trae.ai/sign-up",
        },
    )
    j = r.json()
    if j.get("message") != "success":
        data = j.get("data") or {}
        desc = data.get("description", "")
        err_code = data.get("error_code")
        msg = f"send_code failed: error_code={err_code} {desc or r.text[:200]}"
        desc_lower = desc.lower()
        # 1023 = email already linked → fresh email, no wait.
        # 17 = risk control / "couldn't log in" → short backoff.
        # 1206 = "Maximum number of attempts" → LONG backoff (per-IP rate limit).
        if (
            err_code in (1023, 17, 1206)
            or "linked to another account" in desc_lower
            or "couldn't log in" in desc_lower
            or "try again" in desc_lower
            or "maximum number" in desc_lower
        ):
            raise SendCodeRetryableError(msg)
        raise ProviderError(msg)
    return j.get("data", {}).get("email_ticket", "")


async def _register_verify_login(
    client: httpx.AsyncClient, email: str, password: str, code: str
) -> dict:
    """Submit registration. Returns parsed response on success."""
    r = await client.post(
        REGISTER_URL,
        data={
            "aid": AID,
            "email": email,
            "password": password,
            "code": code,
            "type": "1",
            "email_logic_type": "2",
            "mix_mode": "0",
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://www.trae.ai",
            "Referer": "https://www.trae.ai/sign-up",
        },
    )
    j = r.json()
    if j.get("message") != "success":
        desc = j.get("data", {}).get("description", "")
        raise ProviderError(f"register failed: {desc or r.text[:200]}")
    return j.get("data", {})


async def _trae_login(client: httpx.AsyncClient, cookies: dict) -> dict:
    """Try Trae Login. Returns parsed response. May fail with 20116."""
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    r = await client.post(
        TRAE_LOGIN_URL,
        headers={
            "Content-Type": "application/json",
            "Origin": "https://www.trae.ai",
            "Referer": "https://www.trae.ai/",
            "Cookie": cookie_str,
        },
        json={},
    )
    try:
        return r.json()
    except Exception:
        return {"status_code": r.status_code, "text": r.text[:200]}


async def _get_user_token(client: httpx.AsyncClient, cookies: dict) -> dict:
    """Try GetUserToken. Returns parsed response. May fail with 401.

    Pass ALL cookies (matches yam reference impl) — Trae's GetUserToken
    expects the full session bundle including passport_csrf_token_default,
    msToken, store-idc, etc. Filtering to just the "essential" named cookies
    caused 401 Unauthorized in testing.

    On HTTP 401 we synthesize a ``ResponseMetadata.Error.Code=401`` envelope
    so callers can detect the failure without catching exceptions.
    """
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    r = await client.post(
        GET_USER_TOKEN_URL,
        headers={
            "Content-Type": "application/json",
            "Origin": "https://www.trae.ai",
            "Referer": "https://www.trae.ai/",
            "Cookie": cookie_str,
        },
        json={},
    )
    if r.status_code == 401:
        return {
            "ResponseMetadata": {"Error": {"Code": 401, "Message": "Unauthorized"}},
            "status_code": 401,
            "text": r.text[:200],
        }
    try:
        return r.json()
    except Exception:
        return {"status_code": r.status_code, "text": r.text[:200]}


# ---------------------------------------------------------------------------
async def register_one(
    *,
    pool: MailPool | None = None,
    headless: bool = True,
    proxy: str | None = None,
    launch_browser: bool = True,
    persist: bool = True,
    password: str | None = None,
) -> RegistrationResult:
    """Register a single Trae account via direct API.

    Args:
        pool: pre-started mail pool. A default pool is created/closed if None.
        headless: deprecated (kept for backward compat; direct API has no UI).
        proxy: proxy URL override; defaults to :func:`config.get_proxy`.
        launch_browser: when False, short-circuits (used by tests).
        persist: when True, upsert the resulting account into the DB.
        password: optional explicit password; otherwise a strong random one.

    Returns:
        :class:`RegistrationResult` with ``success`` set.
    """
    if not launch_browser:
        return RegistrationResult(success=False, error="launch_browser=False")

    pool_owned = False
    if pool is None:
        pool = default_pool()
        pool_owned = True
    try:
        await pool.start()
    except Exception as e:  # noqa: BLE001
        return RegistrationResult(success=False, error=f"mail pool start failed: {e}")

    pw = password or _gen_password()
    proxy_url = proxy if proxy is not None else get_proxy()

    provider = None
    inbox = None
    email = ""
    email_ticket = ""

    jwt_token = ""
    refresh_token = ""
    user_id = ""
    region = "SG"
    token_data: dict = {}
    token_expired_at: str = ""
    cookies_list: list = []
    cookies_dict: dict = {}

    try:
        async with httpx.AsyncClient(
            proxy=proxy_url,
            headers={"User-Agent": UA},
            timeout=30.0,
        ) as client:
            # 1. Get a fresh inbox and send OTP. Retry on error_code 1023
            #    ("Email is linked to another account") or 17 (risk control).
            #    emailnator gives distinct base Gmails each time, so a retry
            #    usually lands an unregistered one within a few attempts.
            for attempt in range(EMAIL_LINKED_RETRY_MAX):
                try:
                    provider, inbox = await pool.create_inbox()
                except ProviderError as e:
                    if pool_owned:
                        await pool.close()
                    return RegistrationResult(success=False, error=str(e))
                email = inbox.address
                log.info("inbox ready: %s (provider=%s, attempt=%d/%d)",
                         email, provider.name, attempt + 1, EMAIL_LINKED_RETRY_MAX)
                try:
                    email_ticket = await _send_code(client, email)
                    break  # success
                except SendCodeRetryableError as e:
                    # Backoff strategy:
                    # - 1023 "email linked": no wait, just get a fresh email.
                    # - 17 "couldn't log in" (risk control): short backoff.
                    # - 1206 "Maximum number of attempts" (rate limit):
                    #   LONGER backoff (30-60s) — Trae enforces a per-IP
                    #   send_code rate limit that needs time to clear.
                    err_str = str(e)
                    if "1023" in err_str:
                        wait = 0.0
                    elif "1206" in err_str or "maximum number" in err_str.lower():
                        # 1206 rate limit: needs 30s+ to clear
                        wait = 30.0 + 15.0 * attempt
                    else:
                        # 17 risk control: short backoff
                        wait = min(2.0 * (attempt + 1), 10.0)
                    log.info("send_code retryable for %s: %s (wait=%.1fs)",
                             email, err_str[:120], wait)
                    if attempt + 1 == EMAIL_LINKED_RETRY_MAX:
                        if pool_owned:
                            await pool.close()
                        return RegistrationResult(
                            success=False, email=email,
                            error=f"send_code failed after {EMAIL_LINKED_RETRY_MAX} retries: {e}",
                        )
                    if wait:
                        await asyncio.sleep(wait)
                    continue  # fresh inbox on next iteration
            else:  # for-else: loop never broke (shouldn't happen, but defensive)
                if pool_owned:
                    await pool.close()
                return RegistrationResult(success=False, error="send_code exhausted retries")

            log.info("OTP sent for %s (ticket=%s...)", email, email_ticket[:16])

            # 2. wait for OTP
            otp = await pool.wait_for_otp(provider, inbox, timeout=OTP_WAIT_TIMEOUT)
            log.info("OTP received for %s", email)

            # 3. register
            reg_data = await _register_verify_login(client, email, pw, otp)
            user_id = str(reg_data.get("user_id_str") or reg_data.get("user_id") or "")
            log.info("registered %s (user_id=%s)", email, user_id)

            # Collect cookies from the client
            cookies_dict = dict(client.cookies)
            cookies_list = [
                {"name": k, "value": v, "domain": "trae.ai"}
                for k, v in cookies_dict.items()
            ]

            # 4. Try Trae Login (may fail for temp-mail domains)
            login_resp = await _trae_login(client, cookies_dict)
            # Trae Login may Set-Cookie (e.g. refreshed sessionid or new
            # cloudide-session); re-read the client's cookie jar so the next
            # call sees the updated cookies. Without this, GetUserToken was
            # reliably returning 401 because it saw stale pre-login cookies.
            cookies_dict = dict(client.cookies)
            cookies_list = [
                {"name": k, "value": v, "domain": "trae.ai"}
                for k, v in cookies_dict.items()
            ]
            login_err = (
                login_resp.get("ResponseMetadata", {}).get("Error", {})
                if isinstance(login_resp, dict)
                else {}
            )
            if login_err:
                log.warning(
                    "Trae Login failed for %s: code=%s msg=%s",
                    email,
                    login_err.get("Code"),
                    login_err.get("Message", "")[:100],
                )
            else:
                log.info("Trae Login OK for %s (cookies: %d)",
                         email, len(cookies_dict))

            # 5. Try GetUserToken (needs Trae Login to succeed first).
            #    The X-Cloudide-Session cookie set by Trae Login is what
            #    authenticates this call — without it, we get 401.
            if not login_err:
                token_data = await _get_user_token(client, cookies_dict)
                # If still 401, try one more call after a short delay —
                # Trae's session propagation is eventually consistent.
                gut_err = (
                    token_data.get("ResponseMetadata", {}).get("Error", {})
                    if isinstance(token_data, dict)
                    else {}
                )
                if gut_err and gut_err.get("Code") == 401:
                    log.info("GetUserToken 401, retrying after 2s...")
                    await asyncio.sleep(2.0)
                    cookies_dict = dict(client.cookies)
                    token_data = await _get_user_token(client, cookies_dict)
                # Note: Trae API uses PascalCase field names: "Token",
                # "RefreshToken", "ExpiredAt" — NOT lowercase. Reading with
                # lowercase keys silently returns empty strings.
                result_obj = token_data.get("result") or token_data.get("Result") or {}
                jwt_token = result_obj.get("Token") or result_obj.get("token", "")
                refresh_token = (
                    result_obj.get("RefreshToken")
                    or result_obj.get("refreshToken", "")
                )
                token_expired_at = (
                    result_obj.get("ExpiredAt")
                    or result_obj.get("expiredAt", "")
                )
                if jwt_token:
                    try:
                        jp = parse_jwt(jwt_token)
                        user_id = user_id or jp.user_id
                    except ValueError:
                        pass
                    log.info("JWT obtained for %s (len=%d, expires=%s)",
                             email, len(jwt_token), token_expired_at)
                else:
                    log.warning("GetUserToken returned no JWT for %s: %s",
                                email, str(token_data)[:200])

    except ProviderError as e:
        if pool_owned:
            await pool.close()
        return RegistrationResult(success=False, email=email, error=str(e))
    except httpx.HTTPError as e:
        if pool_owned:
            await pool.close()
        return RegistrationResult(success=False, email=email, error=f"http: {e}")
    finally:
        if pool_owned:
            await pool.close()

    # Build secrets blob — JWT may be empty if Trae Login blocked the domain
    has_jwt = bool(jwt_token)
    secrets = {
        "password": pw,
        "jwt_token": jwt_token,
        "refresh_token": refresh_token,
        "token_expired_at": token_expired_at,
        "cookies": cookies_list,
        "session_cookies": cookies_dict,
        "login_info": {
            "token": jwt_token,
            "refresh_token": refresh_token,
            "user_id": user_id,
            "email": email,
            "username": email.split("@", 1)[0],
            "avatar_url": "",
            "host": "",
            "region": region,
        },
    }

    account = Account(
        email=email,
        name=email.split("@", 1)[0],
        user_id=user_id,
        region=region,
        plan_type="Free",
        status="active" if has_jwt else "registered",
        machine_id="",
    )
    account.secrets_blob = vault.encrypt_obj(secrets)

    if persist:
        try:
            account = db.upsert_account(account)
        except Exception as e:  # noqa: BLE001
            log.warning("db persist failed: %s", e)

    log.info(
        "registration %s for %s (jwt=%s)",
        "complete" if has_jwt else "partial",
        email,
        "yes" if has_jwt else "no",
    )
    return RegistrationResult(
        success=True,
        account=account,
        email=email,
        raw_token_response=token_data,
        cookies=cookies_list,
    )


# ---------------------------------------------------------------------------
async def register_batch(
    total: int,
    concurrency: int = 2,
    *,
    pool: MailPool | None = None,
    headless: bool = True,
    proxy: str | None = None,
    stop_event: asyncio.Event | None = None,
    launch_browser: bool = True,
    persist: bool = True,
) -> list[RegistrationResult]:
    """Run ``total`` registrations with bounded concurrency.

    Args:
        total: number of accounts to register.
        concurrency: max parallel registrations.
        pool: shared mail pool (must already be started). A default pool is
            created/closed when None.
        headless: deprecated (direct API has no UI).
        proxy: pass through to :func:`register_one`.
        stop_event: when set, no new tasks are started; running ones finish.
        launch_browser: when False, workers short-circuit immediately (tests).
        persist: pass through to :func:`register_one`.
    """
    if total <= 0 or concurrency <= 0:
        return []
    concurrency = min(concurrency, total)
    pool_owned = False
    if pool is None:
        pool = default_pool()
        pool_owned = True
        await pool.start()

    sem = asyncio.Semaphore(concurrency)
    results: list[RegistrationResult] = [None] * total  # type: ignore[list-item]

    async def _worker(i: int) -> None:
        async with sem:
            if stop_event is not None and stop_event.is_set():
                results[i] = RegistrationResult(success=False, error="stopped")
                return
            log.info("[worker %d] starting registration %d/%d", i, i + 1, total)
            try:
                r = await register_one(
                    pool=pool, headless=headless, proxy=proxy,
                    launch_browser=launch_browser, persist=persist,
                )
            except Exception as e:  # noqa: BLE001
                r = RegistrationResult(success=False, error=str(e))
            results[i] = r
            log.info(
                "[worker %d] done %d/%d success=%s",
                i, i + 1, total, r.success,
            )

    await asyncio.gather(*(_worker(i) for i in range(total)))

    if pool_owned:
        await pool.close()
    return results


# ---------------------------------------------------------------------------
def register_one_sync(**kwargs) -> RegistrationResult:
    """Sync wrapper around :func:`register_one`."""
    return asyncio.run(register_one(**kwargs))


def register_batch_sync(total: int, concurrency: int = 2, **kwargs) -> list[RegistrationResult]:
    """Sync wrapper around :func:`register_batch`."""
    return asyncio.run(register_batch(total, concurrency, **kwargs))
