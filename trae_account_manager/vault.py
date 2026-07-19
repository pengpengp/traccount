"""Encrypted credential vault (AES-256-GCM).

Master key resolution order:
  1. ``TAM_MASTER_KEY`` env var (hex, for tests).
  2. OS keyring (``keyring``) when a usable backend exists.
  3. Key file ``master.key`` in the app data dir (mode 0600) as a portable
     fallback for headless / CI boxes without a secret service.

The ciphertext blob format is ``base64(nonce || ciphertext_and_tag)``.
"""
from __future__ import annotations

import base64
import json
import os
import secrets as _secrets
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import get_app_data_dir


_KEY_SERVICE = "trae-account-manager"
_KEY_USER = "master-key"


def _try_keyring_get() -> bytes | None:
    try:
        import keyring  # type: ignore
    except Exception:
        return None
    try:
        val = keyring.get_password(_KEY_SERVICE, _KEY_USER)
    except Exception:
        return None
    if not val:
        return None
    try:
        return bytes.fromhex(val)
    except ValueError:
        return None


def _try_keyring_set(key: bytes) -> bool:
    try:
        import keyring  # type: ignore
    except Exception:
        return False
    try:
        keyring.set_password(_KEY_SERVICE, _KEY_USER, key.hex())
        return True
    except Exception:
        return False


def _key_file_path() -> "Any":
    return get_app_data_dir() / "master.key"


def _load_key_file() -> bytes | None:
    p = _key_file_path()
    if p.exists():
        try:
            return bytes.fromhex(p.read_text().strip())
        except Exception:
            return None
    return None


def _save_key_file(key: bytes) -> None:
    p = _key_file_path()
    p.write_text(key.hex())
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def get_master_key() -> bytes:
    env = os.environ.get("TAM_MASTER_KEY")
    if env:
        try:
            k = bytes.fromhex(env)
            if len(k) == 32:
                return k
        except ValueError:
            pass

    k = _try_keyring_get()
    if k and len(k) == 32:
        return k

    k = _load_key_file()
    if k and len(k) == 32:
        # opportunistically mirror to keyring
        if not _try_keyring_set(k):
            pass
        return k

    k = _secrets.token_bytes(32)
    if not _try_keyring_set(k):
        _save_key_file(k)
    return k


def encrypt_str(plaintext: str) -> str:
    """Encrypt a UTF-8 string, returning base64(nonce||ct+tag)."""
    key = get_master_key()
    aes = AESGCM(key)
    nonce = os.urandom(12)
    ct = aes.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt_str(blob: str) -> str:
    key = get_master_key()
    aes = AESGCM(key)
    raw = base64.b64decode(blob.encode("ascii"))
    nonce, ct = raw[:12], raw[12:]
    pt = aes.decrypt(nonce, ct, None)
    return pt.decode("utf-8")


def encrypt_obj(obj: dict) -> str:
    return encrypt_str(json.dumps(obj, ensure_ascii=False))


def decrypt_obj(blob: str) -> dict:
    if not blob:
        return {}
    return json.loads(decrypt_str(blob))
