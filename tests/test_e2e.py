"""End-to-end integration test exercising all TAM layers together.

This test wires the full pipeline against a fake Trae data dir:
  register (mocked) -> Account + secrets_blob -> db -> switcher ->
  trae_api -> storage.json state verification -> capture_current round-trip.

It does NOT call any real network or browser.
"""
import asyncio
import json
from pathlib import Path

import pytest

from trae_account_manager import db, machine, vault
from trae_account_manager.config import get_trae_data_dir
from trae_account_manager.models import Account
from trae_account_manager.process_ctl import DryRunProcessController
from trae_account_manager.switcher import Switcher
from trae_account_manager.trae_api import (
    TraeApiClient,
    cookies_to_header,
    parse_jwt,
)


# ---------------------------------------------------------------------------
def _trae_dir() -> Path:
    return Path(get_trae_data_dir())


def _seed_fake_trae_state(old_machine_id="old-old-old-old"):
    """Populate TRAE_DATA_DIR with stale state."""
    td = _trae_dir()
    machine.write_machineid(td, old_machine_id)
    machine.write_storage(td, {
        "iCubeAuthInfo://icube.cloudide": json.dumps({
            "token": "OLD-TOKEN",
            "userId": "old-user",
            "account": {"email": "old@uuf.me", "username": "old"},
            "userRegion": {"region": "SG"},
            "host": "https://api-sg-central.trae.ai",
        }),
        "iCubeEntitlementInfo://icube.cloudide": "{}",
        "telemetry.machineId": "0" * 32,
        "telemetry.sqmId": "{OLD}",
        "telemetry.devDeviceId": "00000000-0000-0000-0000-000000000000",
        "ui.theme": "dark",
    })
    gs = td / "User" / "globalStorage"
    gs.mkdir(parents=True, exist_ok=True)
    (gs / "state.vscdb").write_text("stale")
    (td / "Local State").write_text("stale")
    (td / "Network").mkdir(exist_ok=True)
    (td / "Network" / "Cookies").write_text("stale")


def _fake_register(email="e2e@uuf.me", region="SG") -> Account:
    """Simulate the register.py output: an Account with full secrets_blob."""
    acc = Account(
        email=email,
        name=email.split("@", 1)[0],
        user_id="u-e2e",
        region=region,
        plan_type="Free",
        status="active",
    )
    fake_jwt = (
        "eyJhbGciOiJIUzI1NiJ9."
        + base64url({"data": {"id": "u-e2e", "tenant_id": "t-1"}})
        + ".sig"
    )
    secrets = {
        "password": "P@ssw0rd!",
        "jwt_token": fake_jwt,
        "refresh_token": "rt-e2e",
        "cookies": [
            {"name": "sessionid", "value": "sid-e2e", "domain": ".trae.ai"},
            {"name": "sid_tt", "value": "tt-e2e", "domain": ".trae.ai"},
        ],
        "login_info": {
            "token": fake_jwt,
            "refresh_token": "rt-e2e",
            "user_id": "u-e2e",
            "email": email,
            "username": acc.name,
            "avatar_url": "",
            "host": "",
            "region": region,
        },
    }
    acc.secrets_blob = vault.encrypt_obj(secrets)
    return acc


def base64url(d: dict) -> str:
    import base64
    raw = json.dumps(d).encode()
    pad = (-len(raw)) % 4
    return base64.b64encode(raw).decode().replace("+", "-").replace("/", "_") + "=" * pad


# ---------------------------------------------------------------------------
def test_e2e_full_pipeline():
    """register -> db -> switcher -> trae_api -> capture_current."""
    _seed_fake_trae_state()

    # 1. simulate registration result: store an Account
    acc = db.upsert_account(_fake_register("e2e1@uuf.me"))
    assert acc.id

    # 2. verify secrets_blob decrypts back to the original
    secrets = vault.decrypt_obj(acc.secrets_blob)
    assert secrets["jwt_token"].startswith("eyJ")
    assert "sessionid=sid-e2e" in cookies_to_header(secrets["cookies"])

    # 3. switcher applies this account to the (fake) Trae data dir
    sw = Switcher(DryRunProcessController())
    res = sw.switch_to_account(acc, launch=False)
    assert res["account_id"] == acc.id
    assert res["launched"] is False

    # 4. verify Trae data dir now reflects the account
    td = _trae_dir()
    machine_id_in_dir = machine.read_machineid(td)
    assert machine_id_in_dir == acc.machine_id

    storage = machine.read_storage(td)
    auth = json.loads(storage["iCubeAuthInfo://icube.cloudide"])
    assert auth["account"]["email"] == "e2e1@uuf.me"
    assert auth["userId"] == "u-e2e"
    assert auth["token"].startswith("eyJ")
    # user setting preserved
    assert storage["ui.theme"] == "dark"
    # telemetry patched to match machineid
    assert storage["telemetry.machineId"] == machine.telemetry_machine_id(machine_id_in_dir)
    # runtime caches cleared: account-bound state (cookies, localStorage,
    # state.vscdb) is now preserved by profile.py, NOT cleared. So these
    # paths should still exist after a switch.
    assert (td / "Network" / "Cookies").exists()
    # "Local State" is also account-bound (Chromium global state) and is
    # preserved now; the old "stale" content from _seed_trae_dir may have
    # been overwritten by a profile restore, but if no profile exists
    # for the account on first switch, the file is left in place.
    assert (td / "Local State").exists()

    # 5. db has the account marked current
    assert db.get_current_account_id() == acc.id
    fetched = db.get_account(acc.id)
    assert fetched.is_current is True
    assert fetched.machine_id == machine_id_in_dir

    # 6. trae_api client can be built from the stored account
    client = TraeApiClient.for_account(fetched)
    assert client.jwt_token.startswith("eyJ")
    assert "sessionid=sid-e2e" in client.cookies
    assert client.region == "SG"
    # JWT payload is parseable
    payload = parse_jwt(client.jwt_token)
    assert payload.user_id == "u-e2e"
    assert payload.tenant_id == "t-1"

    # 7. capture_current round-trips: read the live Trae state back as an Account
    sw2 = Switcher(DryRunProcessController())
    captured = sw2.capture_current()
    assert captured is not None
    assert captured.email == "e2e1@uuf.me"
    cap_secrets = vault.decrypt_obj(captured.secrets_blob)
    assert cap_secrets["login_info"]["token"].startswith("eyJ")


def test_e2e_multi_account_switching():
    """Switching between two accounts should fully swap the Trae state."""
    _seed_fake_trae_state()
    a = db.upsert_account(_fake_register("e2e_a@uuf.me"))
    b = db.upsert_account(_fake_register("e2e_b@uuf.me"))

    sw = Switcher(DryRunProcessController())
    # switch to A
    sw.switch_to_account(a, launch=False)
    storage_a = machine.read_storage(_trae_dir())
    auth_a = json.loads(storage_a["iCubeAuthInfo://icube.cloudide"])
    assert auth_a["account"]["email"] == "e2e_a@uuf.me"
    assert db.get_current_account_id() == a.id

    # switch to B - should overwrite A's state
    sw.switch_to_account(b, launch=False)
    storage_b = machine.read_storage(_trae_dir())
    auth_b = json.loads(storage_b["iCubeAuthInfo://icube.cloudide"])
    assert auth_b["account"]["email"] == "e2e_b@uuf.me"
    assert db.get_current_account_id() == b.id
    # machineid changed between accounts
    assert machine.read_machineid(_trae_dir()) == b.machine_id
    assert a.machine_id != b.machine_id


def test_e2e_clear_then_capture():
    """clear_login_state then capture_current returns None until re-login."""
    _seed_fake_trae_state()
    sw = Switcher(DryRunProcessController())
    sw.clear_login_state()

    # storage.json no longer has auth keys
    storage = machine.read_storage(_trae_dir())
    assert "iCubeAuthInfo://icube.cloudide" not in storage
    assert "iCubeEntitlementInfo://icube.cloudide" not in storage

    # capture_current returns None when there's no live session
    assert sw.capture_current() is None
    assert db.get_current_account_id() is None


def test_e2e_jwt_round_trip_with_pydantic():
    """The JWT emitted by our fake register is parseable + matches user_id."""
    acc = _fake_register("e2e_jwt@uuf.me")
    secrets = vault.decrypt_obj(acc.secrets_blob)
    jwt = secrets["jwt_token"]
    p = parse_jwt(jwt)
    assert p.user_id == "u-e2e"
    assert p.tenant_id == "t-1"


# ---------------------------------------------------------------------------
def test_e2e_backup_created_on_switch(tmp_path):
    """Switcher snapshots the Trae dir before applying changes."""
    _seed_fake_trae_state()
    from trae_account_manager.config import get_backups_dir
    backups_before = list(get_backups_dir().glob("trae-pre-switch-*"))
    acc = db.upsert_account(_fake_register("backup@uuf.me"))
    sw = Switcher(DryRunProcessController())
    sw.switch_to_account(acc, launch=False)
    backups_after = list(get_backups_dir().glob("trae-pre-switch-*"))
    assert len(backups_after) == len(backups_before) + 1
    # backup should contain a copy of the old machineid + storage.json
    latest = max(backups_after, key=lambda p: p.stat().st_mtime)
    assert (latest / "machineid").exists()
    assert (latest / "User" / "globalStorage" / "storage.json").exists()
