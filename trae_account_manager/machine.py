"""Trae machine-id & storage.json management.

Ported from ``Yang-505/Trae-Account-Manager`` (src-tauri/src/machine.rs).

Key facts learned from the reference:
  * Trae binds a device to the contents of the ``machineid`` file (a plain
    UUID) located at the root of the Trae user-data directory.
  * ``storage.json`` (under ``User/globalStorage``) holds VS Code-style
    telemetry ids and the iCube auth/entitlement blobs:
        - ``telemetry.machineId``  : md5(machineid) hex
        - ``telemetry.sqmId``      : ``{UUID-UPPERCASE}``
        - ``telemetry.devDeviceId``: UUID
        - ``iCubeAuthInfo://icube.cloudide``        : JSON string (auth)
        - ``iCubeEntitlementInfo://icube.cloudide`` : JSON string (entitlement)
  * Switching also requires deleting runtime caches (``state.vscdb``,
    ``Local State``, ``IndexedDB``, ``Local Storage``, ``Session Storage``,
    ``Network/Cookies``).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import uuid as _uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import get_backups_dir, get_trae_data_dir, host_for_region

# storage.json keys that carry the Trae login state.
AUTH_KEYS = (
    "iCubeAuthInfo://icube.cloudide",
    "iCubeEntitlementInfo://icube.cloudide",
    "iCubeServerData://icube.cloudide",
    "iCubeAuthInfo://usertag",
)

# Runtime caches that must be cleared on switch (path relative to Trae data dir).
# NOTE: This list intentionally does NOT include ``User/globalStorage/state.vscdb``
# or the Chromium cookie/localStorage/sessionStorage/IndexedDB trees — those
# contain account-bound state (chat history, workspace state, X-Cloudide-Session
# cookies) that must be preserved via profile backup/restore (see profile.py),
# not wiped on every switch. Wiping them was the previous behavior and caused
# every switch to reset the IDE (losing all chat history).
RUNTIME_CACHE_PATHS = (
    # Pure runtime caches (safe to delete; Trae will regenerate on next launch)
    "Cache",
    "CachedData",
    "CachedExtensions",
    "CachedExtensionVSIXs",
    "Code Cache",
    "GPUCache",
    "logs",
    # VS Code service worker / compilation cache
    "User/Service Worker",
    "User/workspaceStorage/.tmp",  # only the tmp subdir, real wsStorage is preserved
)


@dataclass
class TraeLoginInfo:
    token: str
    refresh_token: str = ""
    user_id: str = ""
    email: str = ""
    username: str = ""
    avatar_url: str = ""
    host: str = ""
    region: str = "SG"


# ---------------------------------------------------------------------------
# id generation
# ---------------------------------------------------------------------------
def generate_machine_id() -> str:
    """A fresh Trae ``machineid`` (UUID v4)."""
    return str(_uuid.uuid4())


def telemetry_machine_id(machine_id: str) -> str:
    """``telemetry.machineId`` = md5(machineid) hex."""
    return hashlib.md5(machine_id.encode("utf-8")).hexdigest()


def telemetry_sqm_id() -> str:
    """``telemetry.sqmId`` = ``{UUID-UPPERCASE}``."""
    return "{" + str(_uuid.uuid4()).upper() + "}"


def telemetry_dev_device_id() -> str:
    return str(_uuid.uuid4())


def telemetry_fields(machine_id: str) -> dict:
    return {
        "telemetry.machineId": telemetry_machine_id(machine_id),
        "telemetry.sqmId": telemetry_sqm_id(),
        "telemetry.devDeviceId": telemetry_dev_device_id(),
    }


# ---------------------------------------------------------------------------
# machineid file
# ---------------------------------------------------------------------------
def _machineid_path(trae_dir: Path) -> Path:
    return trae_dir / "machineid"


def read_machineid(trae_dir: Path | None = None) -> str | None:
    trae_dir = trae_dir or get_trae_data_dir()
    p = _machineid_path(trae_dir)
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8").strip()


def write_machineid(trae_dir: Path | None, machine_id: str) -> None:
    trae_dir = trae_dir or get_trae_data_dir()
    trae_dir.mkdir(parents=True, exist_ok=True)
    _machineid_path(trae_dir).write_text(machine_id, encoding="utf-8")


# ---------------------------------------------------------------------------
# storage.json
# ---------------------------------------------------------------------------
def _storage_path(trae_dir: Path) -> Path:
    return trae_dir / "User" / "globalStorage" / "storage.json"


def read_storage(trae_dir: Path | None = None) -> dict:
    trae_dir = trae_dir or get_trae_data_dir()
    p = _storage_path(trae_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return {}


def write_storage(trae_dir: Path | None, obj: dict) -> None:
    trae_dir = trae_dir or get_trae_data_dir()
    p = _storage_path(trae_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def patch_storage_telemetry(
    machine_id: str,
    trae_dir: Path | None = None,
    clear_auth: bool = True,
) -> dict:
    """Update telemetry ids in storage.json and optionally drop iCube auth keys."""
    obj = read_storage(trae_dir)
    if clear_auth:
        for k in AUTH_KEYS:
            obj.pop(k, None)
    obj.update(telemetry_fields(machine_id))
    write_storage(trae_dir, obj)
    return obj


# ---------------------------------------------------------------------------
# login info (iCubeAuthInfo / iCubeEntitlementInfo)
# ---------------------------------------------------------------------------
def _utc(fmt: str = "%Y-%m-%dT%H:%M:%S.000Z", dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime(fmt)


def build_auth_info(info: TraeLoginInfo) -> dict:
    host = info.host or host_for_region(info.region)
    now = datetime.now(timezone.utc)
    return {
        "token": info.token,
        "refreshToken": info.refresh_token,
        "expiredAt": _utc(dt=now + timedelta(days=14)),
        "refreshExpiredAt": _utc(dt=now + timedelta(days=180)),
        "tokenReleaseAt": _utc(dt=now),
        "userId": info.user_id,
        "host": host,
        "userRegion": {
            "region": (info.region or "").upper(),
            "_aiRegion": (info.region or "").upper(),
        },
        "account": {
            "username": info.username,
            "iss": "",
            "iat": 0,
            "organization": "",
            "work_country": "",
            "email": info.email,
            "avatar_url": info.avatar_url,
            "description": "",
            "scope": "marscode",
            "loginScope": "trae",
            "storeCountryCode": "cn",
            "storeCountrySrc": "uid",
            "storeRegion": (info.region or "").upper(),
            "userTag": "row",
        },
    }


def build_entitlement_info() -> dict:
    return {
        "identityStr": "Free",
        "identity": 0,
        "isPayFreshman": False,
        "isSupportCommercialization": True,
        "hasPackage": False,
        "enableEntitlement": True,
        "detail": {
            "can_gen_solo_code": False,
            "fast_request_per": 1,
            "in_wait": False,
            "permission": 1,
            "toast_read": False,
            "toastRead": False,
            "canGenSoloCode": False,
            "fastRequestPer": 1,
            "inWaitlist": False,
        },
    }


def write_login_info(trae_dir: Path | None, info: TraeLoginInfo) -> None:
    obj = read_storage(trae_dir)
    obj["iCubeAuthInfo://icube.cloudide"] = json.dumps(
        build_auth_info(info), ensure_ascii=False
    )
    obj["iCubeEntitlementInfo://icube.cloudide"] = json.dumps(
        build_entitlement_info(), ensure_ascii=False
    )
    write_storage(trae_dir, obj)


# ---------------------------------------------------------------------------
# runtime cache clearing
# ---------------------------------------------------------------------------
def clear_runtime_cache(trae_dir: Path | None = None) -> list[str]:
    """Delete runtime caches. Returns the list of paths actually removed."""
    trae_dir = trae_dir or get_trae_data_dir()
    removed: list[str] = []
    for rel in RUNTIME_CACHE_PATHS:
        p = trae_dir / rel
        if not p.exists():
            continue
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink()
            removed.append(rel)
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------
def backup_trae_dir(trae_dir: Path | None = None, tag: str = "") -> Path:
    trae_dir = trae_dir or get_trae_data_dir()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    name = f"trae-{tag}-{ts}" if tag else f"trae-{ts}"
    dest = get_backups_dir() / name
    # copy only the small config files we care about (avoid GBs of cache)
    dest.mkdir(parents=True, exist_ok=True)
    for rel in ("machineid", "User/globalStorage/storage.json"):
        src = trae_dir / rel
        if src.exists():
            d = dest / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, d)
    return dest


# ---------------------------------------------------------------------------
# Windows registry MachineGuid (optional, Windows-only)
# ---------------------------------------------------------------------------
def get_windows_machine_guid() -> str | None:
    if not __import__("sys").platform.startswith("win"):
        return None
    try:
        import winreg  # type: ignore
    except ImportError:
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
        ) as k:
            guid, _ = winreg.QueryValueEx(k, "MachineGuid")
            return str(guid)
    except OSError:
        return None


def set_windows_machine_guid(new_guid: str) -> None:
    """Set HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid (needs admin)."""
    if not __import__("sys").platform.startswith("win"):
        raise RuntimeError("MachineGuid reset is Windows-only")
    import winreg  # type: ignore

    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\Cryptography",
        0,
        winreg.KEY_SET_VALUE,
    ) as k:
        winreg.SetValueEx(k, "MachineGuid", 0, winreg.REG_SZ, new_guid)


def login_info_from_dict(d: dict) -> TraeLoginInfo:
    return TraeLoginInfo(
        token=d.get("token", ""),
        refresh_token=d.get("refresh_token", d.get("refreshToken", "")),
        user_id=d.get("user_id", d.get("userId", "")),
        email=d.get("email", ""),
        username=d.get("username", d.get("name", "")),
        avatar_url=d.get("avatar_url", d.get("avatarUrl", "")),
        host=d.get("host", ""),
        region=d.get("region", "SG"),
    )


def login_info_to_dict(info: TraeLoginInfo) -> dict:
    return asdict(info)
