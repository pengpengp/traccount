"""FastAPI app smoke tests using FastAPI TestClient."""
from fastapi.testclient import TestClient


def test_info_endpoint():
    from trae_account_manager.web.app import app
    with TestClient(app) as c:
        r = c.get("/api/info")
        assert r.status_code == 200
        data = r.json()
        assert "version" in data
        assert "trae_data_dir" in data


def test_accounts_empty_then_add_then_list():
    """Add + list + delete a unique account (db is shared across tests)."""
    import secrets as _s
    email = "webtest-%s@uuf.me" % _s.token_hex(4)
    from trae_account_manager.web.app import app
    with TestClient(app) as c:
        # add
        r = c.post("/api/accounts", json={
            "email": email,
            "token": "tok-test",
            "user_id": "u-test",
            "region": "SG",
            "name": "TestUser",
        })
        assert r.status_code == 200
        aid = r.json()["id"]
        # list contains the new account
        r = c.get("/api/accounts")
        accounts = r.json()["accounts"]
        matching = [a for a in accounts if a["id"] == aid]
        assert len(matching) == 1
        assert matching[0]["email"] == email
        # delete
        r = c.delete(f"/api/accounts/{aid}")
        assert r.status_code == 200
        assert r.json()["deleted"] is True
        # confirm gone
        r = c.get(f"/api/accounts/{aid}")
        assert r.status_code == 404


def test_get_account_404():
    from trae_account_manager.web.app import app
    with TestClient(app) as c:
        r = c.get("/api/accounts/nonexistent-id")
        assert r.status_code == 404


def test_delete_account_404():
    from trae_account_manager.web.app import app
    with TestClient(app) as c:
        r = c.delete("/api/accounts/nonexistent-id")
        assert r.status_code == 404


def test_set_get_path(tmp_path):
    # path must exist on disk (process_ctl validates)
    fake_exe = tmp_path / "Trae.exe"
    fake_exe.write_text("fake")
    from trae_account_manager.web.app import app
    with TestClient(app) as c:
        r = c.post("/api/path", json={"path": str(fake_exe)})
        assert r.status_code == 200
        r = c.get("/api/path")
        assert r.status_code == 200
        assert r.json()["path"] == str(fake_exe)


def test_index_html_served():
    from trae_account_manager.web.app import app
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "Trae Account Manager" in r.text


def test_switch_404_for_unknown():
    from trae_account_manager.web.app import app
    with TestClient(app) as c:
        r = c.post("/api/switch/nonexistent-id")
        assert r.status_code == 404


def test_usage_404_for_unknown():
    from trae_account_manager.web.app import app
    with TestClient(app) as c:
        r = c.get("/api/usage/nonexistent-id")
        assert r.status_code == 404
