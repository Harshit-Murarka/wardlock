"""Database engine and sessions: SQLite locally, Postgres on Render (SPEC §9).

Sync SQLAlchemy on purpose: it is simpler to read, and SQLite calls are fast enough here.
There are no migrations. After a schema change, delete wardlock.db and run
`python scripts/seed_sandbox.py --restore-db`, which needs no PayPal calls.
"""

from collections.abc import Iterator
from functools import lru_cache
from typing import Any

from fastapi import Request
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from wardlock.config import get_settings
from wardlock.models import Base


def postgres_url(url: str) -> str:
    """Render (and Heroku-style hosts) give `postgres://...`; SQLAlchemy needs a driver name.

    # VERIFY: Postgres is not tested locally (only SQLite is); check the first Render deploy.
    """
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def make_engine(url: str) -> Engine:
    if not url.startswith("sqlite"):
        return create_engine(postgres_url(url), pool_pre_ping=True)

    kwargs: dict[str, Any] = {
        # FastAPI runs sync work on worker threads, so connections may cross threads.
        "connect_args": {"check_same_thread": False},
    }
    if url in ("sqlite://", "sqlite+pysqlite://") or ":memory:" in url:
        # Every new connection to an in-memory database gets its own EMPTY database,
        # so tests must share one connection.
        kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def _enable_sqlite_foreign_keys(dbapi_connection: Any, _connection_record: Any) -> None:
    # SQLite silently ignores FOREIGN KEY constraints unless this is switched on,
    # and the setting only lasts for one connection.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@lru_cache
def get_engine() -> Engine:
    """The app's engine, built once from DATABASE_URL."""
    return make_engine(get_settings().database_url)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False lets callers keep reading objects after a commit.
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    """Create any missing tables. Existing tables are left as they are."""
    Base.metadata.create_all(engine)


def get_db(request: Request) -> Iterator[Session]:
    """FastAPI dependency: one database session per request, closed afterwards.

    Nothing is committed automatically. A route that changes data commits itself, so a
    failed request leaves nothing half-written.
    """
    with request.app.state.session_factory() as session:
        yield session
