from trae_account_manager import vault


def test_encrypt_decrypt_str_roundtrip():
    s = "trae-token-very-secret-数据中文"
    blob = vault.encrypt_str(s)
    assert blob != s
    assert vault.decrypt_str(blob) == s


def test_encrypt_obj_roundtrip():
    obj = {"password": "p@ss", "jwt_token": "tok", "cookies": [{"a": 1}]}
    blob = vault.encrypt_obj(obj)
    out = vault.decrypt_obj(blob)
    assert out == obj


def test_ciphertext_changes_per_call():
    s = "same"
    a = vault.encrypt_str(s)
    b = vault.encrypt_str(s)
    assert a != b  # random nonce
    assert vault.decrypt_str(a) == vault.decrypt_str(b) == s


def test_decrypt_empty():
    assert vault.decrypt_obj("") == {}
