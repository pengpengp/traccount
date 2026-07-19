"""Tests for the per-account profile backup/restore module."""
import json
import os
from pathlib import Path

import pytest

from trae_account_manager import profile


@pytest.fixture
def isolated_profile_root(tmp_path, monkeypatch):
    """Redirect TAM_PROFILE_DIR + TRAE_CONFIG_DIR to a tmp dir."""
    prof_root = tmp_path / "profiles"
    cfg_root = tmp_path / "trae_config"
    monkeypatch.setenv("TAM_PROFILE_DIR", str(prof_root))
    monkeypatch.setenv("TRAE_CONFIG_DIR", str(cfg_root))
    return prof_root, cfg_root


@pytest.fixture
def seeded_trae_dir(tmp_path):
    """Fake a Trae user-data dir with account-bound state."""
    td = tmp_path / "trae_data"
    td.mkdir()
    # state files
    (td / "User" / "globalStorage").mkdir(parents=True)
    (td / "User" / "globalStorage" / "state.vscdb").write_text("vscdb-content")
    (td / "User" / "globalStorage" / "state.vscdb.backup").write_text("backup")
    (td / "User" / "globalStorage" / "storage.json").write_text("{}")
    (td / "User" / "settings.json").write_text('{"theme":"dark"}')
    (td / "machineid").write_text("machine-aaa")
    (td / "Local State").write_text("local-state")
    # state dirs
    (td / "User" / "workspaceStorage").mkdir(parents=True)
    (td / "User" / "workspaceStorage" / "ws1.json").write_text("{}")
    (td / "User" / "history").mkdir(parents=True)
    (td / "User" / "history" / "file1").write_text("hist")
    (td / "Network").mkdir()
    (td / "Network" / "Cookies").write_text("cookie-jar")
    (td / "Local Storage").mkdir()
    (td / "Local Storage" / "leveldb").mkdir()
    (td / "Local Storage" / "leveldb" / "data").write_text("ls")
    (td / "Session Storage").mkdir()
    (td / "IndexedDB").mkdir()
    return td


# ---------------------------------------------------------------------------
def test_get_profile_dir_creates(isolated_profile_root):
    prof_root, _ = isolated_profile_root
    p = profile.get_profile_dir("acc-1")
    assert p == prof_root / "acc-1"
    assert p.exists()


def test_resolve_profile_paths_layout(isolated_profile_root):
    paths = profile.resolve_profile_paths("acc-1")
    assert paths.root.name == "acc-1"
    # state files exist as keys
    assert "User/globalStorage/state.vscdb" in paths.state_files
    assert "machineid" in paths.state_files
    # state dirs exist as keys
    assert "Network" in paths.state_dirs
    assert "Local Storage" in paths.state_dirs
    # license.dat lives under profile root, not state_files
    assert paths.license_dat.name == "license.dat"
    assert paths.meta_file.name == "meta.json"


def test_backup_profile_copies_files_and_dirs(
    isolated_profile_root, seeded_trae_dir
):
    res = profile.backup_profile(seeded_trae_dir, "acc-1", email="a@x.com")
    # Files we expect to be copied
    assert "User/globalStorage/state.vscdb" in res["copied_files"]
    assert "machineid" in res["copied_files"]
    assert "User/settings.json" in res["copied_files"]
    # Dirs we expect to be copied
    assert "Network" in res["copied_dirs"]
    assert "Local Storage" in res["copied_dirs"]
    assert "User/workspaceStorage" in res["copied_dirs"]

    # Actual files exist at the destination
    paths = profile.resolve_profile_paths("acc-1")
    assert (paths.state_files["User/globalStorage/state.vscdb"]).read_text() == "vscdb-content"
    assert (paths.state_files["machineid"]).read_text() == "machine-aaa"
    assert (paths.state_dirs["Network"] / "Cookies").read_text() == "cookie-jar"

    # Meta was written with email + backup timestamp
    meta = profile.read_meta("acc-1")
    assert meta.email == "a@x.com"
    assert meta.last_backup_at > 0


def test_backup_profile_handles_missing_paths(
    isolated_profile_root, tmp_path
):
    """Backup should not crash on a sparse Trae dir."""
    td = tmp_path / "empty_trae"
    td.mkdir()
    res = profile.backup_profile(td, "acc-2", email="b@x.com")
    assert res["copied_files"] == []
    assert res["copied_dirs"] == []
    # Meta is still written
    assert profile.has_profile("acc-2")


def test_restore_profile_round_trip(isolated_profile_root, seeded_trae_dir):
    """Backup → wipe Trae dir → restore should yield identical content."""
    profile.backup_profile(seeded_trae_dir, "acc-1", email="a@x.com")

    # Wipe the Trae dir's account-bound state
    (seeded_trae_dir / "Network" / "Cookies").unlink()
    (seeded_trae_dir / "User" / "globalStorage" / "state.vscdb").unlink()
    (seeded_trae_dir / "machineid").unlink()

    # Restore
    res = profile.restore_profile("acc-1", seeded_trae_dir)
    assert "Network" in res["restored_dirs"]
    assert "User/globalStorage/state.vscdb" in res["restored_files"]
    assert "machineid" in res["restored_files"]

    # Content matches original
    assert (seeded_trae_dir / "Network" / "Cookies").read_text() == "cookie-jar"
    assert (seeded_trae_dir / "User" / "globalStorage" / "state.vscdb").read_text() == "vscdb-content"
    assert (seeded_trae_dir / "machineid").read_text() == "machine-aaa"


def test_restore_profile_skips_missing(isolated_profile_root, tmp_path):
    """Restoring a profile that has no files should be a no-op."""
    td = tmp_path / "fresh_trae"
    td.mkdir()
    # Create the profile by writing meta only (no state files)
    profile.write_meta("acc-empty", profile.ProfileMeta(account_id="acc-empty"))
    res = profile.restore_profile("acc-empty", td)
    assert res["restored_files"] == []
    assert res["restored_dirs"] == []


def test_has_profile(isolated_profile_root):
    assert not profile.has_profile("acc-new")
    profile.write_meta("acc-new", profile.ProfileMeta(account_id="acc-new"))
    assert profile.has_profile("acc-new")


def test_delete_profile(isolated_profile_root, seeded_trae_dir):
    profile.backup_profile(seeded_trae_dir, "acc-del", email="d@x.com")
    assert profile.has_profile("acc-del")
    assert profile.delete_profile("acc-del")
    assert not profile.has_profile("acc-del")
    # Second delete returns False
    assert not profile.delete_profile("acc-del")


def test_list_profiles(isolated_profile_root, seeded_trae_dir):
    profile.backup_profile(seeded_trae_dir, "acc-a", email="a@x.com")
    profile.backup_profile(seeded_trae_dir, "acc-b", email="b@x.com")
    items = profile.list_profiles()
    assert len(items) == 2
    ids = {p["account_id"] for p in items}
    assert ids == {"acc-a", "acc-b"}
    # Each entry has size_bytes > 0 (we copied real files)
    for p in items:
        assert p["size_bytes"] > 0


def test_license_dat_backup_restore(isolated_profile_root, seeded_trae_dir):
    """license.dat lives outside the Trae user-data dir but should still
    be backed up + restored."""
    _, cfg_root = isolated_profile_root
    lic_dir = cfg_root  # TRAE_CONFIG_DIR
    lic_dir.mkdir(exist_ok=True)
    (lic_dir / "license.dat").write_text("ENCRYPTED-LICENSE-BLOB")

    res = profile.backup_profile(seeded_trae_dir, "acc-lic", email="l@x.com")
    assert any("license.dat" in f for f in res["copied_files"])

    # Delete the live license.dat
    (lic_dir / "license.dat").unlink()
    assert not (lic_dir / "license.dat").exists()

    # Restore it
    res = profile.restore_profile("acc-lic", seeded_trae_dir)
    assert "license.dat" in res["restored_files"]
    assert (lic_dir / "license.dat").read_text() == "ENCRYPTED-LICENSE-BLOB"


def test_meta_round_trip(isolated_profile_root):
    m = profile.ProfileMeta(
        account_id="acc-x",
        email="x@y.com",
        last_backup_at=1700000000,
        last_restore_at=1700000100,
        trae_version="1.2.3",
        notes="test",
    )
    profile.write_meta("acc-x", m)
    loaded = profile.read_meta("acc-x")
    assert loaded.account_id == "acc-x"
    assert loaded.email == "x@y.com"
    assert loaded.last_backup_at == 1700000000
    assert loaded.last_restore_at == 1700000100
    assert loaded.trae_version == "1.2.3"
    assert loaded.notes == "test"
