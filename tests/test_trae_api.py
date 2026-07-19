"""trae_api unit tests (no network)."""
import base64
import json

import pytest

from trae_account_manager import vault
from trae_account_manager.models import Account
from trae_account_manager.trae_api import (
    API_BASE_HOSTS,
    AUTH_SCHEME,
    TraeApiClient,
    api_base_for_region,
    cookies_to_header,
    detect_region_from_cookies,
    parse_entitlements_to_summary,
    parse_jwt,
)


def _b64url(d: dict) -> str:
    raw = json.dumps(d).encode()
    pad = (-len(raw)) % 4
    return base64.b64encode(raw).decode().replace("+", "-").replace("/", "_") + "=" * pad


# ---------------------------------------------------------------------------
def test_parse_jwt_extracts_user_and_tenant():
    payload = {"data": {"id": "user-123", "tenant_id": "t-456"}}
    token = f"hdr.{_b64url(payload)}.sig"
    p = parse_jwt(token)
    assert p.user_id == "user-123"
    assert p.tenant_id == "t-456"
    assert p.raw["data"]["id"] == "user-123"


def test_parse_jwt_rejects_bad_shape():
    with pytest.raises(ValueError):
        parse_jwt("only.two.three.four")
    with pytest.raises(ValueError):
        parse_jwt("not.a.jwt-payload!")


def test_cookies_to_header_from_string():
    s = "sessionid=abc; sid_tt=xyz\n"
    h = cookies_to_header(s)
    assert "sessionid=abc" in h
    assert "sid_tt=xyz" in h
    assert "\n" not in h


def test_cookies_to_header_from_playwright_list():
    cookies = [
        {"name": "sessionid", "value": "abc", "domain": ".trae.ai"},
        {"name": "sid_tt", "value": "xyz"},
    ]
    h = cookies_to_header(cookies)
    assert h == "sessionid=abc; sid_tt=xyz"


def test_cookies_to_header_empty():
    assert cookies_to_header("") == ""
    assert cookies_to_header([]) == ""
    assert cookies_to_header(None) == ""


def test_detect_region_from_cookies():
    assert detect_region_from_cookies("store-idc=useast; x=1") == "US"
    assert detect_region_from_cookies("trae-target-idc=alisg") == "SG"
    assert detect_region_from_cookies("nothing useful") == "SG"


def test_api_base_for_region():
    assert api_base_for_region("SG") == API_BASE_HOSTS["SG"]
    assert api_base_for_region("US") == API_BASE_HOSTS["US"]
    assert api_base_for_region("CN") == API_BASE_HOSTS["CN"]
    assert api_base_for_region("") == API_BASE_HOSTS["SG"]


# ---------------------------------------------------------------------------
def _entitlement_pack(product_type=1, product_id=0):
    """Build a fake entitlement pack mirroring Trae's response shape."""
    return {
        "entitlement_base_info": {
            "product_type": product_type,
            "product_id": product_id,
            "user_id": "u-1",
            "end_time": 2000000000,
            "quota": {
                "premium_model_fast_request_limit": 50,
                "premium_model_slow_request_limit": 500,
                "advanced_model_request_limit": 10,
                "auto_completion_limit": 1000,
            },
            "product_extra": {},
        },
        "usage": {
            "premium_model_fast_amount": 10.0,
            "premium_model_slow_amount": 100.0,
            "advanced_model_amount": 2.0,
            "auto_completion_amount": 250.0,
        },
    }


def test_parse_entitlements_free_plan():
    data = {"user_entitlement_pack_list": [_entitlement_pack(product_type=1, product_id=0)]}
    s = parse_entitlements_to_summary(data)
    assert s.plan_type == "Free"
    assert s.fast_request_limit == 50
    assert s.fast_request_used == 10.0
    assert s.fast_request_left == 40.0
    assert s.slow_request_left == 400.0
    assert s.advanced_model_left == 8.0
    assert s.autocomplete_left == 750.0
    assert s.reset_time == 2000000000
    assert s.extra_fast_request_limit == 0  # no gift pack


def test_parse_entitlements_pro_plan():
    data = {"user_entitlement_pack_list": [_entitlement_pack(product_type=1, product_id=7)]}
    s = parse_entitlements_to_summary(data)
    assert s.plan_type == "Pro"


def test_parse_entitlements_extra_pack():
    pack = _entitlement_pack(product_type=2, product_id=0)
    pack["entitlement_base_info"]["product_extra"] = {
        "package_extra": {"package_source_type": 6},
    }
    data = {"user_entitlement_pack_list": [pack]}
    s = parse_entitlements_to_summary(data)
    # extra package uses the same quota field names
    assert s.extra_fast_request_limit == 50
    assert s.extra_fast_request_used == 10.0
    assert s.extra_fast_request_left == 40.0
    assert s.extra_package_name == "2026 Anniversary Treat"
    # free-plan summary fields stay at defaults
    assert s.fast_request_limit == 0


def test_parse_entitlements_empty():
    s = parse_entitlements_to_summary({})
    assert s.plan_type == "Free"
    assert s.fast_request_limit == 0


# ---------------------------------------------------------------------------
def test_client_headers_with_jwt():
    c = TraeApiClient(cookies="sessionid=abc", jwt_token="tok-123", region="SG")
    h = c._headers(with_auth=True)
    assert h["Authorization"] == f"{AUTH_SCHEME} tok-123"
    assert h["Cookie"] == "sessionid=abc"
    assert h["Origin"] == "https://www.trae.ai"
    assert h["Content-Type"] == "application/json"


def test_client_headers_token_only_drops_cookie():
    c = TraeApiClient(cookies="sessionid=abc", jwt_token="tok-123", region="SG")
    h = c._headers(with_auth=True, use_token_only=True)
    assert "Cookie" not in h
    assert h["Authorization"] == f"{AUTH_SCHEME} tok-123"


def test_client_for_account_decrypts_secrets():
    acc = Account(
        email="api@uuf.me", name="X", user_id="u-1", region="US",
    )
    secrets = {
        "jwt_token": "tok-xyz",
        "cookies": "sessionid=abc; sid_tt=def",
        "login_info": {"token": "tok-xyz"},
    }
    acc.secrets_blob = vault.encrypt_obj(secrets)
    c = TraeApiClient.for_account(acc)
    assert c.jwt_token == "tok-xyz"
    assert "sessionid=abc" in c.cookies
    assert c.region == "US"
    assert c.api_base == API_BASE_HOSTS["US"]


def test_client_region_inferred_from_cookies_when_default():
    c = TraeApiClient(cookies="store-idc=useast", region="SG")
    # even though region=SG was passed, the useast cookie should override
    # (detect_region_from_cookies returns US)
    # NOTE: TraeApiClient only auto-sniffs when region==DEFAULT_REGION ("SG")
    assert c.region == "US"
    assert c.api_base == API_BASE_HOSTS["US"]


def test_client_proxy_default():
    # get_proxy() defaults to http://127.0.0.1:10808 (TAM_PROXY env)
    c = TraeApiClient(cookies="", region="SG")
    assert c.proxy is not None


# ---------------------------------------------------------------------------
# JWT expiry detection & refresh tracking
# ---------------------------------------------------------------------------
import time as _time


def test_is_jwt_expired_no_token():
    c = TraeApiClient(cookies="", region="SG")
    assert c._is_jwt_expired() is True


def test_is_jwt_expired_no_expiry_info():
    """JWT without exp claim and without token_expired_at → refresh."""
    payload = {"data": {"id": "u-1"}}
    token = f"hdr.{_b64url(payload)}.sig"
    c = TraeApiClient(cookies="", jwt_token=token, region="SG")
    # No exp in payload, no token_expired_at → conservative True
    assert c._is_jwt_expired() is True


def test_is_jwt_expired_via_token_expired_at_future():
    payload = {"data": {"id": "u-1"}}
    token = f"hdr.{_b64url(payload)}.sig"
    # Expire 1 hour in the future
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    c = TraeApiClient(
        cookies="", jwt_token=token, region="SG", token_expired_at=future,
    )
    assert c._is_jwt_expired() is False


def test_is_jwt_expired_via_token_expired_at_past():
    payload = {"data": {"id": "u-1"}}
    token = f"hdr.{_b64url(payload)}.sig"
    from datetime import datetime, timedelta, timezone
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    c = TraeApiClient(
        cookies="", jwt_token=token, region="SG", token_expired_at=past,
    )
    assert c._is_jwt_expired() is True


def test_is_jwt_expired_via_jwt_exp_claim():
    """Fallback path: parse JWT exp claim when token_expired_at is missing."""
    exp = int(_time.time()) + 3600  # 1h future
    payload = {"data": {"id": "u-1"}, "exp": exp}
    token = f"hdr.{_b64url(payload)}.sig"
    c = TraeApiClient(cookies="", jwt_token=token, region="SG")
    # No token_expired_at, but JWT has exp → should read it
    assert c._is_jwt_expired() is False


def test_jwt_refreshed_flag_defaults_false():
    c = TraeApiClient(cookies="", region="SG")
    assert c.jwt_refreshed is False


def test_get_refreshed_secrets_delta_empty_when_not_refreshed():
    c = TraeApiClient(cookies="sessionid=abc", region="SG")
    assert c.get_refreshed_secrets_delta() == {}


def test_get_refreshed_secrets_delta_populated_after_manual_refresh():
    """Simulate get_user_token having populated the new fields."""
    c = TraeApiClient(cookies="sessionid=abc", region="SG")
    c.jwt_token = "new-jwt"
    c.refresh_token = "new-rt"
    c.token_expired_at = "2026-12-31T23:59:59Z"
    c.jwt_refreshed = True
    delta = c.get_refreshed_secrets_delta()
    assert delta["jwt_token"] == "new-jwt"
    assert delta["refresh_token"] == "new-rt"
    assert delta["token_expired_at"] == "2026-12-31T23:59:59Z"
    # Cookies should be re-serialised from the cookie header
    assert any(c["name"] == "sessionid" for c in delta["cookies"])
    assert delta["session_cookies"]["sessionid"] == "abc"
    # login_info should carry the new tokens
    assert delta["login_info"]["token"] == "new-jwt"
    assert delta["login_info"]["region"] == "SG"


def test_for_account_reads_token_expired_at():
    acc = Account(email="x@uuf.me", name="X", user_id="u-1", region="SG")
    secrets = {
        "jwt_token": "tok-xyz",
        "token_expired_at": "2026-12-31T23:59:59Z",
        "cookies": "sessionid=abc",
    }
    acc.secrets_blob = vault.encrypt_obj(secrets)
    c = TraeApiClient.for_account(acc)
    assert c.token_expired_at == "2026-12-31T23:59:59Z"
