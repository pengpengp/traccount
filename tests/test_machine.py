import json
import uuid as _uuid

from trae_account_manager import machine
from trae_account_manager.config import get_trae_data_dir


def _tdir():
    from pathlib import Path
    return Path(get_trae_data_dir())


def test_machine_id_is_uuid():
    mid = machine.generate_machine_id()
    _uuid.UUID(mid)  # raises if invalid


def test_telemetry_machine_id_is_md5_hex():
    mid = machine.generate_machine_id()
    import hashlib
    assert machine.telemetry_machine_id(mid) == hashlib.md5(mid.encode()).hexdigest()
    assert len(machine.telemetry_machine_id(mid)) == 32


def test_telemetry_fields_shape():
    mid = machine.generate_machine_id()
    f = machine.telemetry_fields(mid)
    assert set(f) == {"telemetry.machineId", "telemetry.sqmId", "telemetry.devDeviceId"}
    assert f["telemetry.sqmId"].startswith("{") and f["telemetry.sqmId"].endswith("}")
    _uuid.UUID(f["telemetry.devDeviceId"])


def test_write_read_machineid(tmp_path):
    mid = machine.generate_machine_id()
    machine.write_machineid(tmp_path, mid)
    assert machine.read_machineid(tmp_path) == mid


def test_patch_storage_telemetry_clears_auth(tmp_path):
    # seed storage with existing auth keys
    machine.write_storage(tmp_path, {
        "iCubeAuthInfo://icube.cloudide": "old",
        "some.other.key": "keep",
    })
    mid = machine.generate_machine_id()
    machine.patch_storage_telemetry(mid, tmp_path, clear_auth=True)
    obj = machine.read_storage(tmp_path)
    assert obj["some.other.key"] == "keep"
    assert "iCubeAuthInfo://icube.cloudide" not in obj
    assert obj["telemetry.machineId"] == machine.telemetry_machine_id(mid)


def test_write_login_info(tmp_path):
    info = machine.TraeLoginInfo(
        token="tok", refresh_token="rt", user_id="u1",
        email="a@uuf.me", username="A", region="SG",
    )
    machine.write_storage(tmp_path, {})
    machine.write_login_info(tmp_path, info)
    obj = machine.read_storage(tmp_path)
    auth = json.loads(obj["iCubeAuthInfo://icube.cloudide"])
    assert auth["token"] == "tok"
    assert auth["userId"] == "u1"
    assert auth["host"] == "https://api-sg-central.trae.ai"
    assert auth["account"]["email"] == "a@uuf.me"
    ent = json.loads(obj["iCubeEntitlementInfo://icube.cloudide"])
    assert ent["identityStr"] == "Free"


def test_clear_runtime_cache(tmp_path):
    td = tmp_path
    # create files
    (td / "User/globalStorage").mkdir(parents=True)
    (td / "User/globalStorage/state.vscdb").write_text("x")
    (td / "User/globalStorage/state.vscdb.backup").write_text("x")
    (td / "Local State").write_text("x")
    (td / "IndexedDB").mkdir()
    (td / "Local Storage").mkdir()
    (td / "Session Storage").mkdir()
    (td / "Network").mkdir()
    (td / "Network/Cookies").write_text("x")
    (td / "Network/Cookies-journal").write_text("x")
    removed = machine.clear_runtime_cache(td)
    assert "User/globalStorage/state.vscdb" in removed
    assert "Network/Cookies" in removed
    assert not (td / "Network/Cookies").exists()
    assert not (td / "IndexedDB").exists()


def test_backup_trae_dir(tmp_path):
    machine.write_machineid(tmp_path, machine.generate_machine_id())
    machine.write_storage(tmp_path, {"telemetry.machineId": "abc"})
    b = machine.backup_trae_dir(tmp_path, tag="t1")
    assert (b / "machineid").exists()
    assert (b / "User/globalStorage/storage.json").exists()


def test_region_host():
    from trae_account_manager.config import host_for_region
    assert host_for_region("SG") == "https://api-sg-central.trae.ai"
    assert host_for_region("CN") == "https://api.trae.com.cn"
