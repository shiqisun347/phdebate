from __future__ import annotations

from collections.abc import AsyncGenerator
from threading import Lock

from app.core.config import settings
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
_sqlite_lock_registry: dict[str, Lock] = {}
_sqlite_lock_registry_guard = Lock()


class TransactionLockTimeout(RuntimeError):
    pass


def acquire_transaction_locks(db: Session, *keys: str, timeout_seconds: float | None = None) -> None:
    """Provide SQLite with the keyed transaction locks PostgreSQL gets from FOR UPDATE.

    Production uses PostgreSQL row locks. SQLite ignores ``FOR UPDATE``, which
    previously allowed two local/test requests to mutate the same room sequence
    or assign one user to two active rooms. Locks live until the outer database
    transaction commits, rolls back, or the session closes.
    """

    timeout = float(timeout_seconds if timeout_seconds is not None else settings.database_lock_timeout_seconds)
    if timeout <= 0:
        raise ValueError("transaction lock timeout must be positive")
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        configured = float(db.info.get("postgresql_lock_timeout_seconds", 0))
        if timeout > configured:
            timeout_ms = max(1, round(timeout * 1000))
            db.execute(text(f"SET LOCAL lock_timeout = '{timeout_ms}ms'"))
            db.info["postgresql_lock_timeout_seconds"] = timeout
        return
    if dialect != "sqlite":
        return
    held: dict[str, Lock] = db.info.setdefault("sqlite_transaction_locks", {})
    # SQLite has one writer for the whole database. Acquiring the writer key
    # before opening the transaction prevents a request that is waiting on a
    # user lock from retaining a read transaction that blocks the winner's
    # commit. Entity keys remain useful for documenting lock order and for
    # avoiding duplicate acquisition inside one session.
    normalized_keys = {"-1:sqlite-writer", *keys}
    acquired_now: list[tuple[str, Lock]] = []
    for key in sorted(normalized_keys):
        if key in held:
            continue
        with _sqlite_lock_registry_guard:
            lock = _sqlite_lock_registry.setdefault(key, Lock())
        if not lock.acquire(timeout=timeout):
            for acquired_key, acquired_lock in reversed(acquired_now):
                held.pop(acquired_key, None)
                acquired_lock.release()
            raise TransactionLockTimeout("database transaction lock wait exceeded")
        held[key] = lock
        acquired_now.append((key, lock))


@event.listens_for(Session, "after_transaction_end")
def _release_transaction_locks(db: Session, transaction) -> None:
    if transaction.parent is not None:
        return
    db.info.pop("postgresql_lock_timeout_seconds", None)
    held: dict[str, Lock] = db.info.pop("sqlite_transaction_locks", {})
    for lock in reversed(list(held.values())):
        lock.release()


async def get_db() -> AsyncGenerator[Session, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_schema() -> None:
    from app.models import entities  # noqa: F401

    if settings.app_env == "production":
        return
    Base.metadata.create_all(bind=engine)
