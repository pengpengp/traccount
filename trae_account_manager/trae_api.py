"""Async HTTP client for the Trae cloud API.

Ported from ``Yang-505/Trae-Account-Manager`` (trae_api.rs).

Endpoints (all region-hosted except GetUserInfo which is on ug-normal):
  * ``POST {base}/cloudide/api/v3/common/GetUserToken``            (cookies)
  * ``POST https://ug-normal.trae.ai/cloudide/api/v3/trae/GetUserInfo`` (cookies)
  * ``POST {base}/trae/api/v1/pay/user_current_entitlement_list``  (JWT)
  * ``POST {base}/trae/api/v1/pay/query_user_usage_group_by_session`` (JWT)
  * ``POST {base}/trae/api/v1/pay/query_birthday_bonus``           (JWT)
  * ``POST {base}/trae/api/v1/pay/claim_birthday_bonus``            (JWT)

Auth header is ``Cloud-IDE-JWT <token>``.
"""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import REGION_HOSTS, get_proxy, host_for_region

log = logging.getLogger(__name__)

# Region -> host
API_BASE_HOSTS = {
    "SG": "https://api-sg-central.trae.ai",
    "US": "https://api-us-east.trae.ai",
    "CN": "https://api.trae.com.cn",
}
API_BASE_UG = "https://ug-normal.trae.ai"
DEFAULT_REGION = "SG"
AUTH_SCHEME = "Cloud-IDE-JWT"


# ---------------------------------------------------------------------------
@dataclass
class UsageSummary:
    plan_type: str = "Free"
    # main quota
    fast_request_limit: int = 0
    fast_request_used: float = 0.0
    fast_request_left: float = 0.0
    slow_request_limit: int = 0
    slow_request_used: float = 0.0
    slow_request_left: float = 0.0
    advanced_model_limit: int = 0
    advanced_model_used: float = 0.0
    advanced_model_left: float = 0.0
    autocomplete_limit: int = 0
    autocomplete_used: float = 0.0
    autocomplete_left: float = 0.0
    reset_time: int = 0
    # extra package (anniversary gift etc.)
    extra_fast_request_limit: int = 0
    extra_fast_request_used: float = 0.0
    extra_fast_request_left: float = 0.0
    extra_expire_time: int = 0
    extra_package_name: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class JwtPayload:
    user_id: str = ""
    tenant_id: str = ""
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
def parse_jwt(token: str) -> JwtPayload:
    """Decode a JWT payload (no signature verification)."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("invalid JWT (expected 3 parts)")
    payload_b64 = parts[1]
    # base64url -> base64 with padding
    pad = (-len(payload_b64)) % 4
    standard = payload_b64.replace("-", "+").replace("_", "/") + ("=" * pad)
    try:
        raw_bytes = base64.b64decode(standard)
        raw = json.loads(raw_bytes.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"invalid JWT payload: {e}") from e
    data = raw.get("data") or {}
    return JwtPayload(
        user_id=str(data.get("id", "")),
        tenant_id=str(data.get("tenant_id", "")),
        raw=raw,
    )


def cookies_to_header(cookies: Any) -> str:
    """Accept either a cookie header string or a Playwright cookies list."""
    if not cookies:
        return ""
    if isinstance(cookies, str):
        # collapse newlines / double spaces
        return " ".join(
            " ".join(line.strip().split()) for line in cookies.splitlines()
        ).replace("  ", " ").strip()
    # list of {name, value, ...}
    parts = []
    for c in cookies:
        if isinstance(c, dict):
            n, v = c.get("name"), c.get("value", "")
            if n:
                parts.append(f"{n}={v}")
        elif isinstance(c, (tuple, list)) and len(c) == 2:
            parts.append(f"{c[0]}={c[1]}")
    return "; ".join(parts)


def detect_region_from_cookies(cookie_header: str) -> str:
    """Region sniffing mirroring trae_api.rs detect_api_base_from_cookies."""
    if "store-idc=useast" in cookie_header or "trae-target-idc=useast" in cookie_header:
        return "US"
    if "store-idc=alisg" in cookie_header or "trae-target-idc=alisg" in cookie_header:
        return "SG"
    return DEFAULT_REGION


def api_base_for_region(region: str) -> str:
    return API_BASE_HOSTS.get((region or "").upper(), API_BASE_HOSTS[DEFAULT_REGION])


# ---------------------------------------------------------------------------
class TraeApiClient:
    """Async Trae cloud API client (httpx-based, proxy-aware)."""

    def __init__(
        self,
        cookies: Any = None,
        jwt_token: str | None = None,
        region: str = DEFAULT_REGION,
        proxy: str | None = None,
        timeout: float = 30.0,
        token_expired_at: str = "",
    ) -> None:
        cookie_header = cookies_to_header(cookies)
        # If region not provided, sniff from cookies
        if not region or region == DEFAULT_REGION:
            region = detect_region_from_cookies(cookie_header) or DEFAULT_REGION
        self.region = region
        self.api_base = api_base_for_region(region)
        self.cookies = cookie_header
        self.jwt_token = jwt_token
        self.token_expired_at = token_expired_at
        # Refresh token is populated by get_user_token() when it refreshes the JWT.
        self.refresh_token = ""
        self.proxy = proxy if proxy is not None else get_proxy()
        self._client: httpx.AsyncClient | None = None
        # Set to True whenever get_user_token() obtains a fresh JWT, so callers
        # (e.g. `tam usage`) know they should persist the new credentials.
        self.jwt_refreshed = False

    # ------------------------------------------------------------------
    @classmethod
    def for_account(cls, account, secrets: dict | None = None) -> "TraeApiClient":
        """Build a client from a stored ``Account`` (decrypts secrets_blob)."""
        from .vault import decrypt_obj
        secrets = secrets or decrypt_obj(account.secrets_blob)
        cookies = secrets.get("cookies", "")
        if not cookies and "cookies_list" in secrets:
            cookies = secrets["cookies_list"]
        jwt = secrets.get("jwt_token") or (secrets.get("login_info") or {}).get("token", "")
        expired_at = secrets.get("token_expired_at", "")
        region = account.region or DEFAULT_REGION
        return cls(
            cookies=cookies, jwt_token=jwt, region=region,
            token_expired_at=expired_at,
        )

    # ------------------------------------------------------------------
    async def __aenter__(self) -> "TraeApiClient":
        self._client = httpx.AsyncClient(
            proxy=self.proxy or None,
            timeout=self.timeout,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def timeout(self) -> float:
        return 30.0

    def _client_or_raise(self) -> httpx.AsyncClient:
        if self._client is None:
            # ad-hoc client (caller did not use `async with`)
            self._client = httpx.AsyncClient(
                proxy=self.proxy or None,
                timeout=self.timeout,
                follow_redirects=True,
            )
        return self._client

    # ------------------------------------------------------------------
    def _headers(self, with_auth: bool, use_token_only: bool = False) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.trae.ai",
            "Referer": "https://www.trae.ai/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        if not use_token_only and self.cookies:
            h["Cookie"] = self.cookies
        if with_auth and self.jwt_token:
            h["Authorization"] = f"{AUTH_SCHEME} {self.jwt_token}"
        return h

    async def _post(self, url: str, json_body: dict | None = None,
                    with_auth: bool = False, use_token_only: bool = False) -> dict:
        client = self._client_or_raise()
        resp = await client.post(
            url,
            headers=self._headers(with_auth=with_auth, use_token_only=use_token_only),
            json=json_body or {},
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"{url} returned {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()

    # ------------------------------------------------------------------
    # endpoints
    # ------------------------------------------------------------------
    async def trae_login(self) -> dict:
        """Call Trae Login endpoint to obtain/refresh the X-Cloudide-Session
        cookie that GetUserToken requires for authentication.

        Without this call, GetUserToken returns 401 with error_code 20310
        ("get session empty"). The Trae Login response sets the
        ``X-Cloudide-Session`` cookie via ``Set-Cookie`` header, which we
        merge into ``self.cookies`` so subsequent requests carry it.

        Safe to call repeatedly — Trae Login is idempotent (returns
        ``FirstLogin: false`` for accounts that have logged in before).
        """
        url = f"{API_BASE_UG}/cloudide/api/v3/trae/Login"
        client = self._client_or_raise()
        resp = await client.post(
            url,
            headers=self._headers(with_auth=False),
            json={},
        )
        # Merge any Set-Cookie response (especially X-Cloudide-Session)
        # into self.cookies so subsequent calls carry it. httpx's cookie
        # jar stores them automatically, but self.cookies is our source
        # of truth for the explicit Cookie header.
        new_cookies = dict(client.cookies)
        if new_cookies:
            existing = self._cookie_dict()
            existing.update(new_cookies)
            self.cookies = "; ".join(f"{k}={v}" for k, v in existing.items())
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return {"status_code": resp.status_code, "text": resp.text[:200]}

    def _cookie_dict(self) -> dict:
        """Parse self.cookies (header string) into a dict."""
        out: dict[str, str] = {}
        if not self.cookies:
            return out
        for part in self.cookies.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip()
        return out

    async def get_user_token(self) -> dict:
        """Exchange cookies for a JWT. Stores the token on this client.

        Automatically calls :meth:`trae_login` first if we don't already
        have an ``X-Cloudide-Session`` cookie (otherwise GetUserToken
        returns 401 with ``error_code 20310``).

        Note: GetUserToken response uses PascalCase field names
        (``Result.Token``, ``Result.RefreshToken``, ``Result.ExpiredAt``),
        NOT lowercase. Callers must read with capitalised keys.

        On success, sets :attr:`jwt_refreshed` = True so callers (e.g.
        ``tam usage``) know to persist the refreshed credentials back to
        the database. Also updates :attr:`token_expired_at` and
        :attr:`refresh_token` from the response.
        """
        # Ensure X-Cloudide-Session cookie is present; without it,
        # GetUserToken returns 401. Trae Login sets this cookie.
        if "X-Cloudide-Session" not in self.cookies:
            log.debug("no X-Cloudide-Session cookie, calling Trae Login first")
            await self.trae_login()
        url = f"{self.api_base}/cloudide/api/v3/common/GetUserToken"
        data = await self._post(url, with_auth=False)
        result = data.get("result") or data.get("Result") or {}
        token = result.get("Token") or result.get("token", "")
        if token:
            self.jwt_token = token
            self.token_expired_at = (
                result.get("ExpiredAt") or result.get("expiredAt", "")
            )
            self.refresh_token = (
                result.get("RefreshToken") or result.get("refreshToken", "")
            )
            self.jwt_refreshed = True
        return data

    # ------------------------------------------------------------------
    def _is_jwt_expired(self, leeway_seconds: int = 60) -> bool:
        """Check if the JWT is expired or about to expire.

        Uses ``self.token_expired_at`` (ISO 8601 string from GetUserToken)
        when available; falls back to parsing the JWT ``exp`` claim.
        Returns True when in doubt (so callers will refresh).
        """
        if not self.jwt_token:
            return True
        # Prefer the explicit ExpiredAt returned by GetUserToken.
        if self.token_expired_at:
            try:
                from datetime import datetime, timezone
                ts = self.token_expired_at.replace("Z", "+00:00")
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                return (dt - now).total_seconds() < leeway_seconds
            except Exception:  # noqa: BLE001
                pass
        # Fallback: parse the JWT ``exp`` claim.
        try:
            payload = parse_jwt(self.jwt_token)
            exp = payload.raw.get("exp") or payload.raw.get("Exp")
            if exp:
                now = int(__import__("time").time())
                return int(exp) - now < leeway_seconds
        except Exception:  # noqa: BLE001
            pass
        # No expiry info — be conservative and refresh.
        return True

    async def _ensure_fresh_jwt(self) -> None:
        """Refresh the JWT via Trae Login + GetUserToken if it's expired.

        No-op when the JWT is still valid. Safe to call before any
        JWT-authenticated request.
        """
        if self._is_jwt_expired():
            log.info("JWT expired or missing, refreshing via GetUserToken")
            await self.get_user_token()

    def get_refreshed_secrets_delta(self) -> dict:
        """Return fields that should be persisted if the JWT was refreshed.

        Call this after :meth:`get_usage_summary_by_token` (or any method
        that may invoke :meth:`_ensure_fresh_jwt`). Returns an empty dict
        when nothing changed.
        """
        if not self.jwt_refreshed:
            return {}
        # Rebuild cookies list from the current cookie header so the
        # persisted store matches what the client is actually using.
        cookies_dict = self._cookie_dict()
        cookies_list = [
            {"name": k, "value": v, "domain": "trae.ai"}
            for k, v in cookies_dict.items()
        ]
        return {
            "jwt_token": self.jwt_token,
            "refresh_token": self.refresh_token,
            "token_expired_at": self.token_expired_at,
            "cookies": cookies_list,
            "session_cookies": cookies_dict,
            "login_info": {
                "token": self.jwt_token,
                "refresh_token": self.refresh_token,
                "user_id": "",
                "email": "",
                "username": "",
                "avatar_url": "",
                "host": "",
                "region": self.region,
            },
        }

    async def get_user_info(self) -> dict:
        url = f"{API_BASE_UG}/cloudide/api/v3/trae/GetUserInfo"
        data = await self._post(url, json_body={"IfWebPage": True}, with_auth=False)
        return data

    async def get_entitlement_list(self) -> dict:
        url = f"{self.api_base}/trae/api/v1/pay/user_current_entitlement_list"
        return await self._post(
            url, json_body={"require_usage": True}, with_auth=True
        )

    async def query_usage(self, start_time: int, end_time: int,
                          page_size: int = 20, page_num: int = 1) -> dict:
        url = f"{self.api_base}/trae/api/v1/pay/query_user_usage_group_by_session"
        return await self._post(
            url,
            json_body={
                "start_time": start_time,
                "end_time": end_time,
                "page_size": page_size,
                "page_num": page_num,
            },
            with_auth=True,
        )

    async def query_birthday_bonus(self) -> bool:
        url = f"{self.api_base}/trae/api/v1/pay/query_birthday_bonus"
        data = await self._post(url, with_auth=True, use_token_only=True)
        return bool(data.get("bonus_claimed", False))

    async def claim_birthday_bonus(self) -> dict:
        url = f"{self.api_base}/trae/api/v1/pay/claim_birthday_bonus"
        return await self._post(url, with_auth=True, use_token_only=True)

    # ------------------------------------------------------------------
    async def get_usage_summary(self) -> UsageSummary:
        """High-level summary: ensures we have a JWT, then queries entitlements."""
        if not self.jwt_token:
            await self.get_user_token()
        ent = await self.get_entitlement_list()
        return parse_entitlements_to_summary(ent)

    async def get_usage_summary_by_token(self) -> UsageSummary:
        """Try multiple regional endpoints when we already have a JWT.

        Automatically refreshes the JWT via :meth:`_ensure_fresh_jwt` if
        it's missing or expired — without this, the Trae backend returns
        an entitlement pack with empty ``usage`` fields, causing ``tam
        usage`` to always show full quota even for accounts that have
        been used for hours.
        """
        await self._ensure_fresh_jwt()
        last_err: Exception | None = None
        for base in (self.api_base, API_BASE_HOSTS["SG"], API_BASE_HOSTS["US"]):
            url = f"{base}/trae/api/v1/pay/user_current_entitlement_list"
            try:
                data = await self._post(
                    url,
                    json_body={"require_usage": True},
                    with_auth=True,
                    use_token_only=True,
                )
                return parse_entitlements_to_summary(data)
            except Exception as e:  # noqa: BLE001
                log.debug("endpoint %s failed: %s", base, e)
                last_err = e
        raise RuntimeError(f"all API endpoints failed: {last_err}")


# ---------------------------------------------------------------------------
def parse_entitlements_to_summary(entitlements: dict) -> UsageSummary:
    """Collapse the entitlement pack list into a single UsageSummary."""
    summary = UsageSummary()
    packs = entitlements.get("user_entitlement_pack_list") or []
    for pack in packs:
        base = pack.get("entitlement_base_info") or {}
        usage = pack.get("usage") or {}
        quota = (base.get("quota") or {})
        if base.get("product_type") == 2:
            # extra / gift package
            summary.extra_fast_request_limit = quota.get(
                "premium_model_fast_request_limit", 0
            )
            summary.extra_fast_request_used = usage.get(
                "premium_model_fast_amount", 0.0
            )
            summary.extra_fast_request_left = (
                summary.extra_fast_request_limit - summary.extra_fast_request_used
            )
            summary.extra_expire_time = base.get("end_time", 0)
            prod_extra = (base.get("product_extra") or {})
            pkg_extra = prod_extra.get("package_extra")
            if isinstance(pkg_extra, dict) and pkg_extra.get("package_source_type") == 6:
                summary.extra_package_name = "2026 Anniversary Treat"
        else:
            # Free / Pro plan
            summary.plan_type = "Pro" if base.get("product_id", 0) != 0 else "Free"
            summary.reset_time = base.get("end_time", 0)
            summary.fast_request_limit = quota.get(
                "premium_model_fast_request_limit", 0
            )
            summary.fast_request_used = usage.get("premium_model_fast_amount", 0.0)
            summary.fast_request_left = (
                summary.fast_request_limit - summary.fast_request_used
            )
            summary.slow_request_limit = quota.get(
                "premium_model_slow_request_limit", 0
            )
            summary.slow_request_used = usage.get("premium_model_slow_amount", 0.0)
            summary.slow_request_left = (
                summary.slow_request_limit - summary.slow_request_used
            )
            summary.advanced_model_limit = quota.get(
                "advanced_model_request_limit", 0
            )
            summary.advanced_model_used = usage.get("advanced_model_amount", 0.0)
            summary.advanced_model_left = (
                summary.advanced_model_limit - summary.advanced_model_used
            )
            summary.autocomplete_limit = quota.get("auto_completion_limit", 0)
            summary.autocomplete_used = usage.get("auto_completion_amount", 0.0)
            summary.autocomplete_left = (
                summary.autocomplete_limit - summary.autocomplete_used
            )
    return summary
