"""Switcher end-to-end tests against a fake TRAE_DATA_DIR."""
import json
from pathlib import Path

import pytest

from trae_account_manager import db, machine, vault
from trae_account_manager.config import get_trae_data_dir
from trae_account_manager.models import Account
from trae_account_manager.process_ctl import DryRunProcessController
from trae_account_manager.switcher import Switcher


def _trae_dir() -> Path:
    return Path(get_trae_data_dir())


def _seed_trae_dir(old_machine_id: str = "00000000-0000-0000-0000-000000000000") -> None:
    """Populate the fake Trae data dir with stale state."""
    td = _trae_dir()
    # stale machineid
    machine.write_machineid(td, old_machine_id)
    # storage.json with old auth + telemetry
    machine.write_storage(td, {
        "iCubeAuthInfo://icube.cloudide": json.dumps({"token": "OLD", "userId": "old"}),
        "iCubeEntitlementInfo://icube.cloudide": "{}",
        "some.user.setting": "preserve-me",
        "telemetry.machineId": "0" * 32,
        "telemetry.sqmId": "{OLD-UPPER}",
        "telemetry.devDeviceId": "00000000-0000-0000-0000-000000000000",
    })
    # runtime caches that should be deleted
    gs = td / "User" / "globalStorage"
    gs.mkdir(parents=True, exist_ok=True)
    (gs / "state.vscdb").write_text("stale")
    (gs / "state.vscdb.backup").write_text("stale")
    (td / "Local State").write_text("stale")
    (td / "IndexedDB").mkdir(exist_ok=True)
    (td / "Local Storage").mkdir(exist_ok=True)
    (td / "Session Storage").mkdir(exist_ok=True)
    (td / "Network").mkdir(exist_ok=True)
    (td / "Network" / "Cookies").write_text("stale")
    (td / "Network" / "Cookies-journal").write_text("stale")


def _make_account(email: str = "switch@uuf.me", with_token: bool = True) -> Account:
    acc = Account(
        email=email,
        name="SwitcherUser",
        user_id="u-switch",
        region="SG",
        machine_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    secrets = {
        "login_info": {
            "token": "tok-xyz" if with_token else "",
            "refresh_token": "rt-xyz",
            "user_id": "u-switch",
            "email": email,
            "username": "SwitcherUser",
            "avatar_url": "",
            "host": "",
            "region": "SG",
        }
    }
    acc.secrets_blob = vault.encrypt_obj(secrets)
    return acc


def test_switch_writes_full_state():
    _seed_trae_dir()
    acc = db.upsert_account(_make_account("switch1@uuf.me"))
    ctl = DryRunProcessController()
    sw = Switcher(ctl)

    result = sw.switch_to_account(acc, launch=False)

    td = _trae_dir()
    # 1. machineid written
    assert machine.read_machineid(td) == acc.machine_id
    # 2. telemetry.machineId == md5(machineid)
    storage = machine.read_storage(td)
    assert storage["telemetry.machineId"] == machine.telemetry_machine_id(acc.machine_id)
    # 3. old auth keys replaced (not the old token)
    auth = json.loads(storage["iCubeAuthInfo://icube.cloudide"])
    assert auth["token"] == "tok-xyz"
    assert auth["userId"] == "u-switch"
    assert auth["account"]["email"] == "switch1@uuf.me"
    # 4. user setting preserved
    assert storage["some.user.setting"] == "preserve-me"
    # 5. runtime caches deleted — but account-bound state (cookies,
    #    localStorage, state.vscdb) preserved by profile.py.
    #    Pre-existing runtime caches that we DO clear (Cache/, logs/, etc.)
    #    aren't seeded here, so result["cleared"] may be empty.
    assert "Network/Cookies" not in result["cleared"]
    assert "User/globalStorage/state.vscdb" not in result["cleared"]
    # Account-bound paths must still exist (profile.py preserves them)
    assert (td / "Network" / "Cookies").exists()
    assert (td / "IndexedDB").exists()
    # 6. account marked current in db
    assert db.get_current_account_id() == acc.id
    persisted = db.get_account(acc.id)
    assert persisted.machine_id == acc.machine_id
    assert persisted.is_current is True
    # 7. no launch when launch=False
    assert ctl.events == ["kill"]
    assert result["launched"] is False
    assert result["machine_id"] == acc.machine_id


def test_switch_launch_invokes_controller():
    _seed_trae_dir()
    acc = db.upsert_account(_make_account("switch2@uuf.me"))
    ctl = DryRunProcessController()
    sw = Switcher(ctl)
    sw.switch_to_account(acc, launch=True)
    assert ctl.events == ["kill", "launch"]


def test_switch_rejects_account_without_token():
    _seed_trae_dir()
    acc = db.upsert_account(_make_account("notoken@uuf.me", with_token=False))
    sw = Switcher(DryRunProcessController())
    with pytest.raises(ValueError, match="no token"):
        sw.switch_to_account(acc, launch=False)


def test_switch_auto_generates_machine_id_when_missing():
    _seed_trae_dir()
    acc = db.upsert_account(_make_account("autom@uuf.me"))
    acc.machine_id = ""  # no machine_id bound
    sw = Switcher(DryRunProcessController())
    result = sw.switch_to_account(acc, launch=False)
    # an auto-generated machine id should have been written
    import uuid as _uuid
    _uuid.UUID(result["machine_id"])  # raises if invalid
    td = _trae_dir()
    assert machine.read_machineid(td) == result["machine_id"]


def test_clear_login_state_resets_trae():
    _seed_trae_dir()
    # mark a current account first
    acc = db.upsert_account(_make_account("clear@uuf.me"))
    db.set_current_account(acc.id)
    assert db.get_current_account_id() == acc.id

    sw = Switcher(DryRunProcessController())
    result = sw.clear_login_state()

    td = _trae_dir()
    # machineid exists (fresh)
    mid = machine.read_machineid(td)
    assert mid == result["machine_id"]
    # auth keys removed
    storage = machine.read_storage(td)
    assert "iCubeAuthInfo://icube.cloudide" not in storage
    assert "iCubeEntitlementInfo://icube.cloudide" not in storage
    # telemetry patched to match new machine id
    assert storage["telemetry.machineId"] == machine.telemetry_machine_id(mid)
    # no current account
    assert db.get_current_account_id() is None


def test_capture_current_imports_live_session():
    _seed_trae_dir()
    # simulate a live logged-in Trae by writing a fresh iCubeAuthInfo
    td = _trae_dir()
    machine.write_storage(td, {
        "iCubeAuthInfo://icube.cloudide": json.dumps({
            "token": "live-token",
            "refreshToken": "live-rt",
            "userId": "live-user-42",
            "host": "https://api-sg-central.trae.ai",
            "userRegion": {"region": "SG", "_aiRegion": "SG"},
            "account": {
                "username": "LiveUser",
                "email": "live@uuf.me",
                "avatar_url": "https://x/a.png",
            },
        }),
    })
    machine.write_machineid(td, "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

    sw = Switcher(DryRunProcessController())
    acc = sw.capture_current()

    assert acc is not None
    assert acc.email == "live@uuf.me"
    assert acc.name == "LiveUser"
    assert acc.user_id == "live-user-42"
    assert acc.region == "SG"
    assert acc.machine_id == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    # secrets_blob should decrypt back to the live token
    secrets = vault.decrypt_obj(acc.secrets_blob)
    assert secrets["login_info"]["token"] == "live-token"
    assert secrets["login_info"]["refresh_token"] == "live-rt"
    # stored in db
    fetched = db.get_account_by_email("live@uuf.me")
    assert fetched is not None
    assert fetched.id == acc.id


def test_capture_current_returns_none_when_no_auth():
    _seed_trae_dir()
    # storage has auth keys removed (already cleared)
    machine.write_storage(_trae_dir(), {"telemetry.machineId": "abc"})
    sw = Switcher(DryRunProcessController())
    assert sw.capture_current() is None
