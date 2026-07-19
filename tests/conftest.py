"""Pytest config: isolate all paths into a per-test tmp dir."""
import os
import secrets
import tempfile

_tmp = tempfile.mkdtemp(prefix="tam_test_")
os.environ["TAM_DATA_DIR"] = _tmp
os.environ["TRAE_DATA_DIR"] = os.path.join(_tmp, "trae_data")
os.makedirs(os.environ["TRAE_DATA_DIR"], exist_ok=True)
# deterministic master key so tests never touch the OS keyring
os.environ["TAM_MASTER_KEY"] = secrets.token_bytes(32).hex()

import pytest


@pytest.fixture
def tmp_trae_dir():
    d = os.environ["TRAE_DATA_DIR"]
    os.makedirs(d, exist_ok=True)
    return d
