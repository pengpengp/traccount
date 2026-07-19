from trae_account_manager import db, vault
from trae_account_manager.models import Account


def _make(email="a@uuf.me"):
    acc = Account(
        email=email,
        name="user",
        user_id="u123",
        region="SG",
        machine_id="11111111-1111-1111-1111-111111111111",
    )
    acc.secrets_blob = vault.encrypt_obj({"password": "pw", "jwt_token": "tok"})
    return acc


def test_upsert_list_delete():
    acc = db.upsert_account(_make())
    assert acc.id

    fetched = db.get_account(acc.id)
    assert fetched is not None
    assert fetched.email == "a@uuf.me"
    assert vault.decrypt_obj(fetched.secrets_blob)["password"] == "pw"

    assert any(a.id == acc.id for a in db.list_accounts())

    by_email = db.get_account_by_email("a@uuf.me")
    assert by_email.id == acc.id

    assert db.delete_account(acc.id) is True
    assert db.get_account(acc.id) is None


def test_current_account_state():
    a = db.upsert_account(_make("x@uuf.me"))
    db.set_current_account(a.id)
    assert db.get_current_account_id() == a.id
    assert db.get_account(a.id).is_current is True

    db.set_current_account(None)
    assert db.get_current_account_id() is None
