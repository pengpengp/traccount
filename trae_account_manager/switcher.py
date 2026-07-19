"""One-click account switching.

Orchestrates the Trae IDE state swap, porting
``Yang-505/Trae-Account-Manager`` switch_trae_account into Python:
  kill Trae -> backup current profile -> restore target profile ->
  write machineid -> clear runtime caches -> patch storage.json
  telemetry -> write iCubeAuthInfo -> (Windows) reset MachineGuid -> launch Trae.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from pathlib import Path

from . import db, machine, profile
from .config import get_license_dat_path, get_trae_data_dir
from .machine import TraeLoginInfo, login_info_from_dict
from .process_ctl import DefaultProcessController, ProcessController
from .vault import decrypt_obj, encrypt_obj

log = logging.getLogger(__name__)


class Switcher:
    def __init__(self, ctl: ProcessController | None = None):
        self.ctl = ctl or DefaultProcessController()

    # ------------------------------------------------------------------
    @staticmethod
    def _account_login_info(account) -> TraeLoginInfo:
        secrets = decrypt_obj(account.secrets_blob)
        li = secrets.get("login_info") or {}
        # merge top-level fields captured during registration
        info = TraeLoginInfo(
            token=li.get("token") or secrets.get("jwt_token", ""),
            refresh_token=li.get("refresh_token") or secrets.get("refresh_token", ""),
            user_id=li.get("user_id") or account.user_id,
            email=li.get("email") or account.email,
            username=li.get("username") or account.name,
            avatar_url=li.get("avatar_url") or account.avatar_url,
            host=li.get("host", ""),
            region=li.get("region") or account.region,
        )
        if not info.token:
            raise ValueError(f"account {account.email} has no token; cannot switch")
        return info

    # ------------------------------------------------------------------
    def switch_to_account(self, account, *, launch: bool = True,
                          reset_registry: bool = False) -> dict:
        """Apply ``account`` to the local Trae IDE.

        Profile flow: backup current Trae state to the *currently-active*
        account's profile (so we don't lose its chat history), then restore
        the target account's profile (if one exists) before patching
        identity (machineid / iCubeAuthInfo / license.dat).
        """
        trae_dir = Path(get_trae_data_dir())
        info = self._account_login_info(account)
        machine_id = account.machine_id or machine.generate_machine_id()

        log.info("switching Trae -> %s (machineid=%s)", account.email, machine_id)

        # snapshot current state for safety
        try:
            machine.backup_trae_dir(trae_dir, tag="pre-switch")
        except Exception as e:  # noqa: BLE001
            log.warning("backup failed: %s", e)

        # 1. stop Trae
        self.ctl.kill()

        # 2. Backup the *current* account's state to its profile (if we know
        # which account is currently active). This preserves chat history,
        # workspace state, and cookies for the account we're leaving.
        backup_result = {"copied_files": [], "copied_dirs": []}
        current_id = db.get_current_account_id()
        if current_id and current_id != account.id:
            cur_acc = db.get_account(current_id)
            cur_email = cur_acc.email if cur_acc else ""
            try:
                backup_result = profile.backup_profile(
                    trae_dir, current_id, email=cur_email,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("profile backup for %s failed: %s", current_id, e)

        # 3. Restore the target account's profile (if one exists). On first
        # switch (no profile yet), we leave Trae's existing state alone and
        # just patch identity below.
        restore_result = {"restored_files": [], "restored_dirs": []}
        if profile.has_profile(account.id):
            try:
                restore_result = profile.restore_profile(account.id, trae_dir)
            except Exception as e:  # noqa: BLE001
                log.warning("profile restore for %s failed: %s", account.id, e)

        # 4. write machineid
        machine.write_machineid(trae_dir, machine_id)

        # 5. clear runtime caches (no longer touches state.vscdb / cookies)
        removed = machine.clear_runtime_cache(trae_dir)

        # 6. patch telemetry + clear old auth keys
        machine.patch_storage_telemetry(machine_id, trae_dir, clear_auth=True)

        # 7. write new login info (iCubeAuthInfo / iCubeEntitlementInfo)
        machine.write_login_info(trae_dir, info)

        # 8. handle license.dat — if the target account's profile has one,
        # it's already been restored by step 3. If not (first switch), we
        # delete the existing one so Trae doesn't auto-login as the old
        # account on next launch.
        lic_path = get_license_dat_path()
        if not lic_path.exists() and not profile.has_profile(account.id):
            # Nothing to do — no license.dat in either place.
            pass
        elif not profile.has_profile(account.id):
            # First switch for this account: drop the existing license.dat
            # so Trae doesn't restore the previous account's session.
            try:
                if lic_path.exists():
                    lic_path.unlink()
                    log.info("removed license.dat (no profile for new account)")
            except OSError as e:
                log.warning("could not remove license.dat: %s", e)

        # 9. (Windows) optionally reset registry MachineGuid
        reg_reset = False
        if reset_registry and __import__("sys").platform.startswith("win"):
            try:
                machine.set_windows_machine_guid(machine.generate_machine_id())
                reg_reset = True
            except Exception as e:  # noqa: BLE001
                log.warning("registry reset failed (need admin?): %s", e)

        # 10. persist machine_id on the account record + mark current
        account.machine_id = machine_id
        account.last_used_at = int(time.time())
        db.upsert_account(account)
        db.set_current_account(account.id)

        # 11. launch Trae
        launched = False
        if launch:
            try:
                self.ctl.launch()
                launched = True
            except Exception as e:  # noqa: BLE001
                log.warning("launch failed: %s", e)

        return {
            "account_id": account.id,
            "email": account.email,
            "machine_id": machine_id,
            "cleared": removed,
            "registry_reset": reg_reset,
            "launched": launched,
            "profile_backed_up": current_id or "",
            "profile_restored": profile.has_profile(account.id),
            "backup_count": len(backup_result.get("copied_files", []))
                             + len(backup_result.get("copied_dirs", [])),
            "restore_count": len(restore_result.get("restored_files", []))
                              + len(restore_result.get("restored_dirs", [])),
        }

    # ------------------------------------------------------------------
    def clear_login_state(self) -> dict:
        """Reset Trae to a fresh-install state with a new machine id."""
        trae_dir = Path(get_trae_data_dir())
        mid = machine.generate_machine_id()
        try:
            machine.backup_trae_dir(trae_dir, tag="pre-clear")
        except Exception:
            pass
        self.ctl.kill()
        machine.write_machineid(trae_dir, mid)
        machine.clear_runtime_cache(trae_dir)
        machine.patch_storage_telemetry(mid, trae_dir, clear_auth=True)
        # Drop license.dat so Trae doesn't auto-restore the previous account.
        lic_path = get_license_dat_path()
        try:
            if lic_path.exists():
                lic_path.unlink()
        except OSError as e:
            log.warning("could not remove license.dat: %s", e)
        db.set_current_account(None)
        return {"machine_id": mid}

    # ------------------------------------------------------------------
    def capture_current(self, name: str = "", email: str = "") -> object | None:
        """Snapshot the currently-logged-in Trae IDE into a new Account.

        Reads the live ``iCubeAuthInfo`` from storage.json so a manually
        logged-in account can be imported without re-authenticating.
        """
        from .models import Account
        import time

        trae_dir = Path(get_trae_data_dir())
        storage = machine.read_storage(trae_dir)
        raw = storage.get("iCubeAuthInfo://icube.cloudide")
        if not raw:
            return None
        try:
            auth = json.loads(raw)
        except json.JSONDecodeError:
            return None
        acct_info = auth.get("account", {})
        machine_id = machine.read_machineid(trae_dir) or machine.generate_machine_id()
        acc = Account(
            email=email or acct_info.get("email", ""),
            name=name or acct_info.get("username", ""),
            avatar_url=acct_info.get("avatar_url", ""),
            user_id=auth.get("userId", ""),
            region=(auth.get("userRegion") or {}).get("region", "SG"),
            machine_id=machine_id,
        )
        secrets = {
            "login_info": {
                "token": auth.get("token", ""),
                "refresh_token": auth.get("refreshToken", ""),
                "user_id": auth.get("userId", ""),
                "email": acct_info.get("email", ""),
                "username": acct_info.get("username", ""),
                "avatar_url": acct_info.get("avatar_url", ""),
                "host": auth.get("host", ""),
                "region": (auth.get("userRegion") or {}).get("region", "SG"),
            }
        }
        acc.secrets_blob = encrypt_obj(secrets)
        acc = db.upsert_account(acc)
        log.info("captured current Trae session as account %s", acc.email)
        return acc
