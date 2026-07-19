"""Data models (SQLModel/SQLite)."""
from __future__ import annotations

import time
import uuid as _uuid
from typing import Optional

from sqlmodel import Field, SQLModel


def _now() -> int:
    return int(time.time())


def _uuid4() -> str:
    return str(_uuid.uuid4())


class Account(SQLModel, table=True):
    """A registered Trae account.

    Sensitive fields (password, tokens, cookies, full login payload) live in
    ``secrets_blob`` (AES-256-GCM ciphertext, see :mod:`vault`). Plaintext
    columns are limited to data needed for indexing / display.
    """

    __tablename__ = "accounts"

    id: str = Field(default_factory=_uuid4, primary_key=True)
    email: str = Field(index=True)
    name: str = Field(default="")
    avatar_url: str = Field(default="")
    user_id: str = Field(default="")
    tenant_id: str = Field(default="")
    region: str = Field(default="SG")
    plan_type: str = Field(default="Free")
    status: str = Field(default="active")  # active|expired|banned|pending
    machine_id: str = Field(default="")  # Trae machineid bound to this account
    is_active: bool = Field(default=True)  # enabled in TAM list
    is_current: bool = Field(default=False)  # currently driving the Trae IDE
    created_at: int = Field(default_factory=_now)
    updated_at: int = Field(default_factory=_now)
    last_used_at: Optional[int] = Field(default=None)

    # Encrypted JSON blob: {password, jwt_token, refresh_token, token_expired_at,
    # host, cookies, login_info}
    secrets_blob: str = Field(default="")

    def touch(self) -> None:
        self.updated_at = _now()


class AccountStoreMeta(SQLModel, table=True):
    """Single-row table holding store-wide state."""

    __tablename__ = "store_meta"

    id: int = Field(default=1, primary_key=True)
    current_account_id: Optional[str] = Field(default=None)
