"""Mail layer tests.

The create_inbox tests hit the real mail.cx / mail.tm APIs through the
configured proxy (TAM_PROXY / default 127.0.0.1:10808). They are network
tests; skip gracefully if unreachable.
"""
import asyncio
import os
import socket

import pytest

from trae_account_manager.mail import MailTmProvider, TempMailLolProvider, MailPool
from trae_account_manager.mail.base import extract_otp


def test_extract_otp():
    assert extract_otp("your code is 123456 valid") == "123456"
    assert extract_otp("verification code: 998877") == "998877"
    assert extract_otp("OTP 442211") == "442211"
    assert extract_otp("no code here") is None
    assert extract_otp("") is None


def _proxy_ok() -> bool:
    """Quick check that the proxy / direct net is up."""
    try:
        s = socket.create_connection(("127.0.0.1", 10808), timeout=2)
        s.close()
        return True
    except OSError:
        return False


needs_proxy = pytest.mark.skipif(
    os.environ.get("TAM_SKIP_NET") or not _proxy_ok(),
    reason="no proxy / network (set TAM_PROXY and ensure it is up)",
)


@needs_proxy
def test_mailtm_create_inbox():
    async def go():
        p = MailTmProvider()
        await p.start()
        try:
            inbox = await p.create_inbox()
            assert "@" in inbox.address
            assert inbox.token
            return inbox.provider
        finally:
            await p.close()

    prov = asyncio.run(asyncio.wait_for(go(), timeout=50))
    assert prov == "mail.tm"


@needs_proxy
def test_tempmail_create_inbox():
    async def go():
        p = TempMailLolProvider()
        await p.start()
        try:
            inbox = await p.create_inbox()
            assert "@" in inbox.address
            assert inbox.token
            return inbox.provider
        finally:
            await p.close()

    prov = asyncio.run(asyncio.wait_for(go(), timeout=40))
    assert prov == "tempmail.lol"


@needs_proxy
def test_pool_failover_shape():
    async def go():
        pool = MailPool([MailTmProvider(), TempMailLolProvider()])
        await pool.start()
        try:
            provider, inbox = await pool.create_inbox()
            assert inbox.address
            assert provider.name in ("mail.tm", "tempmail.lol")
        finally:
            await pool.close()

    asyncio.run(asyncio.wait_for(go(), timeout=60))
