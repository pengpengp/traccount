"""Cross-platform Trae IDE process control.

Windows-focused (per user target), with macOS support and a Linux no-op
fallback so the switching logic can be unit-tested on a headless box.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Protocol

from .config import get_app_data_dir


def _exe_config_path() -> Path:
    return get_app_data_dir() / "trae_exe.json"


def get_trae_exe_path() -> str | None:
    """Stored Trae executable path, or auto-scanned common install paths."""
    env = os.environ.get("TAM_TRAE_EXE")
    if env:
        return env
    p = _exe_config_path()
    if p.exists():
        try:
            v = json.loads(p.read_text(encoding="utf-8")).get("path")
            if v and Path(v).exists():
                return v
        except Exception:
            pass
    # auto-scan common install locations
    candidates: list[str] = []
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA", "")
        for c in (
            rf"{local}\Programs\Trae\Trae.exe",
            rf"{local}\Programs\Trae CN\Trae CN.exe",
            rf"{local}\Trae\Trae.exe",
            r"C:\Program Files\Trae\Trae.exe",
            r"C:\Program Files\Trae CN\Trae CN.exe",
        ):
            candidates.append(c)
    elif sys.platform == "darwin":
        candidates += ["/Applications/Trae.app", os.path.expanduser("~/Applications/Trae.app")]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


def set_trae_exe_path(path: str) -> None:
    p = _exe_config_path()
    p.write_text(json.dumps({"path": path}), encoding="utf-8")


class ProcessController(Protocol):
    def is_running(self) -> bool: ...
    def kill(self) -> None: ...
    def launch(self) -> None: ...


class DefaultProcessController:
    """Real process controller (Windows/macOS). Linux = no-op for tests."""

    @staticmethod
    def _trae_image_names() -> list[str]:
        """Return the Trae process image names to look for (Trae CN uses "Trae CN.exe")."""
        return ["Trae.exe", "Trae CN.exe"]

    def is_running(self) -> bool:
        if sys.platform.startswith("win"):
            try:
                out = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq Trae.exe", "/NH"],
                    capture_output=True, text=True, timeout=10,
                )
                if "Trae.exe" in out.stdout:
                    return True
                # Also check for Trae CN.exe
                out2 = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq Trae CN.exe", "/NH"],
                    capture_output=True, text=True, timeout=10,
                )
                return "Trae CN.exe" in out2.stdout
            except Exception:
                return False
        if sys.platform == "darwin":
            try:
                r = subprocess.run(
                    ["pgrep", "-f", "Trae.app/Contents/MacOS"],
                    capture_output=True, timeout=10,
                )
                return r.returncode == 0
            except Exception:
                return False
        return False  # Linux: not applicable

    def kill(self) -> None:
        if sys.platform.startswith("win"):
            for img in self._trae_image_names():
                subprocess.run(["taskkill", "/IM", img], capture_output=True)
            time.sleep(0.5)
            if self.is_running():
                for img in self._trae_image_names():
                    subprocess.run(["taskkill", "/F", "/IM", img], capture_output=True)
            time.sleep(1.0)
            return
        if sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e", 'tell application "Trae" to quit'],
                capture_output=True,
            )
            time.sleep(1.5)
            if self.is_running():
                subprocess.run(["pkill", "-9", "-f", "Trae.app/Contents/MacOS"], capture_output=True)
                time.sleep(1.0)
            return
        # Linux: nothing to kill (Trae not installed)

    def launch(self) -> None:
        exe = get_trae_exe_path()
        if not exe:
            raise RuntimeError(
                "Trae executable path not configured. Set it via "
                "`tam set-path <path>` or TAM_TRAE_EXE env."
            )
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-a", exe])
        else:
            subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class DryRunProcessController:
    """Never touches real processes; records calls. For tests/dry-run."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def is_running(self) -> bool:
        return False

    def kill(self) -> None:
        self.events.append("kill")

    def launch(self) -> None:
        self.events.append("launch")
