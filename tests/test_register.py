"""register.py unit tests that do not launch a browser."""
import asyncio

import pytest

from trae_account_manager import register
from trae_account_manager.register import (
    RegistrationResult,
    _gen_password,
    register_batch,
    register_one,
)


def test_gen_password_meets_complexity():
    pw = _gen_password(14)
    assert len(pw) == 14
    assert any(c.islower() for c in pw)
    assert any(c.isupper() for c in pw)
    assert any(c.isdigit() for c in pw)
    assert any(c in "!@#$%^&*" for c in pw)


def test_gen_password_is_random():
    a = _gen_password()
    b = _gen_password()
    assert a != b


def test_registration_result_defaults():
    r = RegistrationResult(success=False, error="x")
    assert r.account is None
    assert r.email == ""
    assert r.raw_token_response == {}
    assert r.cookies == []


def test_register_one_no_browser():
    """When launch_browser=False, returns a non-success result."""
    r = asyncio.run(register_one(launch_browser=False))
    assert r.success is False
    assert "launch_browser" in r.error


def test_register_batch_zero():
    out = asyncio.run(register_batch(0, 1))
    assert out == []


def test_register_batch_negative_concurrency():
    out = asyncio.run(register_batch(5, 0))
    assert out == []


def test_register_batch_sync_stopped_event():
    """With a pre-set stop_event, all workers should short-circuit."""
    async def go():
        ev = asyncio.Event()
        ev.set()
        results = await register_batch(3, 2, stop_event=ev, launch_browser=False)
        return results
    out = asyncio.run(go())
    assert len(out) == 3
    for r in out:
        assert r.success is False
        assert r.error == "stopped"
