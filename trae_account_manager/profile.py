"""Per-account profile storage for Trae IDE state isolation.

Each account gets its own ``profile`` directory under the TAM app data
dir. Switching accounts = backup current Trae state to the current
account's profile, then restore the target account's profile back into
the Trae user-data dir.

This keeps chat history, workspace state, cookies, license.dat, etc.
fully isolated per account, so switching accounts no longer wipes the
session history (which the previous ``clear_runtime_cache`` approach
did).

Override the profile root with ``TAM_PROFILE_DIR`` for testing.
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import get_app_data_dir, get_license_dat_path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paths inside a Trae user-data dir that are account-specific and therefore
# need to be backed up + restored on switch (NOT cleared).
# Relative to the Trae user-data dir.
# ---------------------------------------------------------------------------
ACCOUNT_STATE_FILES: tuple[str, ...] = (
    "User/globalStorage/state.vscdb",
    "User/globalStorage/state.vscdb.backup",
    "User/globalStorage/storage.json",
    "User/settings.json",
    "User/keybindings.json",
    "Local State",
    "machineid",
)

ACCOUNT_STATE_DIRS: tuple[str, ...] = (
    "User/workspaceStorage",
    "User/history",
    "User/snippets",
    "Network",          # Chromium cookies live here
    "Local Storage",    # Chromium localStorage
    "Session Storage",  # Chromium sessionStorage
    "IndexedDB",        # Chromium IndexedDB
)


@dataclass
class ProfileMeta:
    """Lightweight metadata stored alongside each profile."""
    account_id: str
    email: str = ""
    last_backup_at: int = 0
    last_restore_at: int = 0
    trae_version: str = ""
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "email": self.email,
            "last_backup_at": self.last_backup_at,
            "last_restore_at": self.last_restore_at,
            "trae_version": self.trae_version,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ProfileMeta":
        return cls(
            account_id=d.get("account_id", ""),
            email=d.get("email", ""),
            last_backup_at=int(d.get("last_backup_at", 0)),
            last_restore_at=int(d.get("last_restore_at", 0)),
            trae_version=d.get("trae_version", ""),
            notes=d.get("notes", ""),
        )


@dataclass
class ProfilePaths:
    """Resolved filesystem paths for a single account profile."""
    root: Path
    meta_file: Path
    state_files: dict[str, Path] = field(default_factory=dict)
    state_dirs: dict[str, Path] = field(default_factory=dict)
    license_dat: Path = field(default_factory=Path)
    machineid: Path = field(default_factory=Path)


# ---------------------------------------------------------------------------
def get_profile_root() -> Path:
    """Root dir containing all per-account profiles.

    Override with ``TAM_PROFILE_DIR`` for testing.
    """
    import os
    override = os.environ.get("TAM_PROFILE_DIR")
    if override:
        p = Path(override)
    else:
        p = get_app_data_dir() / "profiles"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_profile_dir(account_id: str) -> Path:
    """Return (creating if needed) the profile dir for an account."""
    if not account_id:
        raise ValueError("account_id is required")
    p = get_profile_root() / account_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_profile_paths(account_id: str) -> ProfilePaths:
    """Compute all filesystem paths for a profile (without touching them)."""
    root = get_profile_dir(account_id)
    state_files = {rel: root / rel for rel in ACCOUNT_STATE_FILES}
    state_dirs = {rel: root / rel for rel in ACCOUNT_STATE_DIRS}
    return ProfilePaths(
        root=root,
        meta_file=root / "meta.json",
        state_files=state_files,
        state_dirs=state_dirs,
        license_dat=root / "license.dat",
        machineid=root / "machineid",
    )


def read_meta(account_id: str) -> ProfileMeta:
    p = resolve_profile_paths(account_id)
    if not p.meta_file.exists():
        return ProfileMeta(account_id=account_id)
    try:
        return ProfileMeta.from_dict(json.loads(p.meta_file.read_text("utf-8")))
    except Exception:  # noqa: BLE001
        return ProfileMeta(account_id=account_id)


def write_meta(account_id: str, meta: ProfileMeta) -> None:
    p = resolve_profile_paths(account_id)
    p.meta_file.write_text(
        json.dumps(meta.to_dict(), indent=2, ensure_ascii=False), "utf-8"
    )


# ---------------------------------------------------------------------------
# Backup / restore
# ---------------------------------------------------------------------------
def _copy_file(src: Path, dst: Path) -> bool:
    """Copy a file if it exists. Returns True if copied."""
    if not src.exists() or not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, dst)
        return True
    except OSError as e:
        log.warning("copy failed %s -> %s: %s", src, dst, e)
        return False


def _copy_dir(src: Path, dst: Path) -> bool:
    """Copy a directory tree if it exists. Returns True if copied."""
    if not src.exists() or not src.is_dir():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        # Wipe destination first to make a clean copy.
        shutil.rmtree(dst, ignore_errors=True)
    try:
        shutil.copytree(src, dst, symlinks=False, dirs_exist_ok=False)
        return True
    except OSError as e:
        log.warning("copytree failed %s -> %s: %s", src, dst, e)
        return False


def backup_profile(trae_dir: Path, account_id: str, *, email: str = "") -> dict:
    """Backup current Trae state into the given account's profile dir.

    Returns a dict with the list of files/dirs that were actually copied.
    Skips paths that don't exist (e.g. fresh Trae install). Safe to call
    repeatedly — the destination is overwritten.
    """
    paths = resolve_profile_paths(account_id)
    copied_files: list[str] = []
    copied_dirs: list[str] = []

    for rel, dst in paths.state_files.items():
        if _copy_file(trae_dir / rel, dst):
            copied_files.append(rel)

    for rel, dst in paths.state_dirs.items():
        if _copy_dir(trae_dir / rel, dst):
            copied_dirs.append(rel)

    # license.dat lives outside the Trae user-data dir
    lic_src = get_license_dat_path()
    if lic_src.exists():
        _copy_file(lic_src, paths.license_dat)
        copied_files.append(str(lic_src))

    meta = read_meta(account_id)
    meta.account_id = account_id
    if email:
        meta.email = email
    meta.last_backup_at = int(time.time())
    write_meta(account_id, meta)

    log.info(
        "backed up profile for account %s: %d files + %d dirs",
        account_id, len(copied_files), len(copied_dirs),
    )
    return {
        "account_id": account_id,
        "copied_files": copied_files,
        "copied_dirs": copied_dirs,
    }


def restore_profile(account_id: str, trae_dir: Path) -> dict:
    """Restore an account's profile back into the Trae user-data dir.

    Returns a dict with the list of files/dirs that were actually restored.
    Missing profile paths are silently skipped (the caller should have
    patched storage.json / machineid / license.dat separately).
    """
    paths = resolve_profile_paths(account_id)
    restored_files: list[str] = []
    restored_dirs: list[str] = []

    # Directories first (they may include parent dirs the files need).
    for rel, src in paths.state_dirs.items():
        if _copy_dir(src, trae_dir / rel):
            restored_dirs.append(rel)

    for rel, src in paths.state_files.items():
        if _copy_file(src, trae_dir / rel):
            restored_files.append(rel)

    # Restore license.dat to its real location outside the Trae user-data dir.
    if paths.license_dat.exists():
        lic_dst = get_license_dat_path()
        lic_dst.parent.mkdir(parents=True, exist_ok=True)
        if _copy_file(paths.license_dat, lic_dst):
            restored_files.append("license.dat")

    meta = read_meta(account_id)
    meta.last_restore_at = int(time.time())
    write_meta(account_id, meta)

    log.info(
        "restored profile for account %s: %d files + %d dirs",
        account_id, len(restored_files), len(restored_dirs),
    )
    return {
        "account_id": account_id,
        "restored_files": restored_files,
        "restored_dirs": restored_dirs,
    }


def has_profile(account_id: str) -> bool:
    """True if a profile exists for the given account (has at least meta)."""
    paths = resolve_profile_paths(account_id)
    return paths.meta_file.exists()


def delete_profile(account_id: str) -> bool:
    """Remove an account's profile entirely. Returns True if removed.

    Note: ``resolve_profile_paths`` auto-creates the dir, so we check the
    meta file's existence instead to determine if the profile actually
    had content worth deleting.
    """
    import os
    root = get_profile_root() / account_id
    if not root.exists() or not any(root.iterdir()):
        return False
    shutil.rmtree(root, ignore_errors=True)
    return True


def list_profiles() -> list[dict]:
    """List all profiles with their metadata."""
    root = get_profile_root()
    out: list[dict] = []
    if not root.exists():
        return out
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        meta = read_meta(child.name)
        out.append({
            "account_id": child.name,
            "email": meta.email,
            "last_backup_at": meta.last_backup_at,
            "last_restore_at": meta.last_restore_at,
            "size_bytes": _dir_size(child),
        })
    return out


def _dir_size(p: Path) -> int:
    total = 0
    if p.is_file():
        return p.stat().st_size
    for f in p.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total
