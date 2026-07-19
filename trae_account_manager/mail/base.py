"""Email provider abstractions."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol


class ProviderError(Exception):
    """Raised when a provider cannot create an inbox or fetch OTP."""


@dataclass
class Inbox:
    address: str
    token: str = ""
    provider: str = ""
    meta: dict = field(default_factory=dict)


# Common OTP patterns: 6-digit standalone code, or labelled code.
_OTP_PATTERNS = [
    re.compile(r"\b(\d{6})\b"),
    re.compile(r"code[:\s]*(\d{4,8})", re.IGNORECASE),
    re.compile(r"verification(?:\s*code)?[:\s]*(\d{4,8})", re.IGNORECASE),
    re.compile(r"otp[:\s]*(\d{4,8})", re.IGNORECASE),
]


def extract_otp(text: str) -> str | None:
    if not text:
        return None
    for pat in _OTP_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    return None


class EmailProvider(Protocol):
    name: str

    async def start(self) -> None: ...

    async def create_inbox(self, prefix: str | None = None) -> Inbox: ...

    async def wait_for_otp(
        self, inbox: Inbox, *, timeout: float = 180.0, poll: float = 4.0
    ) -> str: ...

    async def close(self) -> None: ...
