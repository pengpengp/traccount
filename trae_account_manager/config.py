"""Path & runtime configuration.

All Trae-related paths are overridable via environment variables so the
switching logic can be unit-tested on Linux against a fake Trae data dir.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "trae_account_manager"
APP_AUTHOR = "tam"

# Region -> Trae API host (mirrors Yang-505/Trae-Account-Manager).
REGION_HOSTS = {
    "SG": "https://api-sg-central.trae.ai",
    "CN": "https://api.trae.com.cn",
}
DEFAULT_REGION = "SG"

# Trae web endpoints used by the registration flow.
TRAE_SIGNUP_URL = "https://www.trae.ai/sign-up"
TRAE_LOGIN_URL = "https://www.trae.ai/login"
TRAE_ACCOUNT_SETTING_URL = "https://www.trae.ai/account-setting#account"
TRAE_GIFT_URL = "https://www.trae.ai/2026-anniversary-gift"

# Default proxy used by the HTTP client / browser when set.
DEFAULT_PROXY = os.environ.get("TAM_PROXY", "http://127.0.0.1:10808")


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_macos() -> bool:
    return sys.platform == "darwin"


def get_app_data_dir() -> Path:
    """Directory where TAM stores its own data (db, logs, backups)."""
    override = os.environ.get("TAM_DATA_DIR")
    if override:
        p = Path(override)
    else:
        p = Path(user_data_dir(APP_NAME, APP_AUTHOR))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _is_trae_cn() -> bool:
    """Check if the configured Trae executable is the CN version."""
    try:
        from .process_ctl import get_trae_exe_path
        exe = get_trae_exe_path()
        return bool(exe and "trae cn" in exe.lower())
    except Exception:
        return False


def get_trae_data_dir() -> Path:
    """Trae IDE user-data directory.

    Auto-detects Trae CN (``Trae CN`` subfolder) based on the configured
    Trae executable path. Override with ``TRAE_DATA_DIR`` for testing
    or non-standard installs.
    """
    override = os.environ.get("TRAE_DATA_DIR")
    if override:
        return Path(override)

    if is_windows():
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise RuntimeError("APPDATA env var not set")
        suffix = "Trae CN" if _is_trae_cn() else "Trae"
        return Path(appdata) / suffix
    if is_macos():
        home = os.environ.get("HOME", str(Path.home()))
        return Path(home) / "Library" / "Application Support" / "Trae"
    # Linux: Trae IDE does not run here, but allow override (tests/CI).
    raise RuntimeError(
        "Trae IDE is not supported on this OS; set TRAE_DATA_DIR to a "
        "Trae data directory (e.g. a copy from a Windows host)."
    )


def get_trae_config_dir() -> Path:
    """Trae IDE config directory (where ``license.dat`` lives).

    Separate from :func:`get_trae_data_dir`: Trae stores its license in
    the Electron ``app.getPath("appData")`` directory (on Windows this
    is ``%APPDATA%\\Trae`` or ``%APPDATA%\\Trae CN``, NOT the user-data
    dir under ``%APPDATA%``).

    Detects Trae vs Trae CN based on the configured exe path (same logic
    as ``get_trae_data_dir``). Override with ``TRAE_CONFIG_DIR``.
    """
    override = os.environ.get("TRAE_CONFIG_DIR")
    if override:
        return Path(override)
    if is_windows():
        appdata = os.environ.get("APPDATA")
        if appdata:
            suffix = "Trae CN" if _is_trae_cn() else "Trae"
            return Path(appdata) / suffix
    home = os.environ.get("HOME") or str(Path.home())
    # On Windows, HOME may be unset; fall back to USERPROFILE.
    if is_windows() and not os.environ.get("HOME"):
        home = os.environ.get("USERPROFILE", str(Path.home()))
    return Path(home) / ".config" / "TraeAI"


def get_license_dat_path() -> Path:
    """Path to Trae's encrypted ``license.dat`` file."""
    return get_trae_config_dir() / "license.dat"


def get_db_path() -> Path:
    return get_app_data_dir() / "accounts.db"


def get_logs_dir() -> Path:
    p = get_app_data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_backups_dir() -> Path:
    p = get_app_data_dir() / "backups"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_proxy() -> str | None:
    """Proxy URL for outbound HTTP (registration/API). None to disable."""
    val = os.environ.get("TAM_PROXY", DEFAULT_PROXY)
    if val and val.lower() in ("none", "off", "disabled", ""):
        return None
    return val


def host_for_region(region: str) -> str:
    return REGION_HOSTS.get((region or "").upper(), REGION_HOSTS[DEFAULT_REGION])
