"""SQLite persistence layer."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlmodel import Session, SQLModel, create_engine, select

from .config import get_db_path
from .models import Account, AccountStoreMeta

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(f"sqlite:///{get_db_path()}", echo=False)
        SQLModel.metadata.create_all(_engine)
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    eng = get_engine()
    with Session(eng, expire_on_commit=False) as s:
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise


def upsert_account(acc: Account) -> Account:
    with session_scope() as s:
        existing = s.get(Account, acc.id)
        if existing:
            for f in acc.__class__.model_fields:
                setattr(existing, f, getattr(acc, f))
            s.add(existing)
            s.flush()
            s.refresh(existing)
            return existing
        s.add(acc)
        s.flush()
        s.refresh(acc)
        return acc


def list_accounts(only_active: bool = False) -> list[Account]:
    with session_scope() as s:
        stmt = select(Account).order_by(Account.created_at.desc())
        if only_active:
            stmt = stmt.where(Account.is_active == True)  # noqa: E712
        return list(s.exec(stmt).all())


def get_account(account_id: str) -> Account | None:
    with session_scope() as s:
        return s.get(Account, account_id)


def get_account_by_email(email: str) -> Account | None:
    with session_scope() as s:
        stmt = select(Account).where(Account.email == email)
        return s.exec(stmt).first()


def delete_account(account_id: str) -> bool:
    with session_scope() as s:
        acc = s.get(Account, account_id)
        if acc:
            s.delete(acc)
            return True
        return False


def get_current_account_id() -> str | None:
    with session_scope() as s:
        meta = s.get(AccountStoreMeta, 1)
        return meta.current_account_id if meta else None


def set_current_account(account_id: str | None) -> None:
    with session_scope() as s:
        # clear is_current flags
        for acc in s.exec(select(Account)).all():
            acc.is_current = acc.id == account_id
            s.add(acc)
        meta = s.get(AccountStoreMeta, 1)
        if meta is None:
            meta = AccountStoreMeta(id=1, current_account_id=account_id)
        else:
            meta.current_account_id = account_id
        s.add(meta)
