"""SQLite connection, session factory, and one-time schema initialization.

Spec §7.1: SQLite + sqlite-vec. The vector extension is loaded on every
connection so that the vec0 virtual table is available for semantic retrieval
(Phase 2+ populates it; Phase 1 just ensures it loads cleanly).
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import structlog
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

# Side-effect import: pulls every SQLModel subclass into `SQLModel.metadata`
# so `init_schema()` creates all tables. The `noqa` is not for the import
# being unused — it's to silence the "imported but unused" warning which is
# misleading here.
from . import models as _models  # noqa: F401

log = structlog.get_logger(__name__)


def _resolve_db_path() -> Path:
    """Resolve the SQLite DB path. Honors LAWSCHOOL_DB_PATH for tests;
    defaults to ``<storage_root>/lawschool.db`` (spec §7.2 — storage root
    is repo-local in dev, ``~/Library/Application Support/cLAWd/`` in the
    bundled .app)."""

    override = os.environ.get("LAWSCHOOL_DB_PATH")
    if override:
        return Path(override)

    from paths import storage_root

    return storage_root() / "lawschool.db"


# Module-level state: a single engine per process.
_engine: Engine | None = None


def get_engine() -> Engine:
    """Lazy-singleton engine. Tests can override via LAWSCHOOL_DB_PATH and
    call `reset_engine()` between test runs."""
    global _engine
    if _engine is None:
        db_path = _resolve_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)

        # `check_same_thread=False` for FastAPI's threaded access.
        # `uri=False` — we give a plain path, not a sqlite URI.
        connect_args = {"check_same_thread": False}
        _engine = create_engine(
            f"sqlite:///{db_path}",
            echo=False,
            connect_args=connect_args,
        )
        _register_sqlite_hooks(_engine)
        log.info("engine_created", path=str(db_path))
    return _engine


def reset_engine() -> None:
    """Test hook: drop the cached engine so the next `get_engine()` rebuilds."""
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None


def _register_sqlite_hooks(engine: Engine) -> None:
    """Per-connection setup: WAL mode, foreign keys, sqlite-vec extension."""

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: sqlite3.Connection, _conn_record: object) -> None:
        # Foreign keys are off by default in SQLite — turn them on.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        # WAL: better concurrency for read-while-write (common in a background
        # ingestion + foreground retrieval workload).
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.close()

        # Load sqlite-vec. If the user hasn't installed the optional dep we
        # warn but keep going — Phase 1 doesn't use vector search.
        try:
            import sqlite_vec

            dbapi_connection.enable_load_extension(True)
            sqlite_vec.load(dbapi_connection)
            dbapi_connection.enable_load_extension(False)
        except ImportError:
            log.warning("sqlite_vec_not_installed")
        except sqlite3.OperationalError as exc:
            # Some sqlite builds ship without extension-loading support.
            log.warning("sqlite_vec_load_failed", error=str(exc))


def init_schema() -> None:
    """Create all tables defined in `data.models`. Idempotent — safe to call
    on every startup. For Phase 1 we use `create_all`; alembic comes later
    (see SPEC_QUESTIONS.md Q7)."""
    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    log.info("schema_initialized", table_count=len(SQLModel.metadata.tables))


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Context manager for a single session: commits on clean exit, rolls back
    on exception. Used by non-FastAPI call sites (ingestion workers, CLI)."""
    session = Session(get_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency — yields one session per request, closes cleanly.

    Usage:
        from fastapi import Depends
        from data.db import get_session

        @app.get("/books")
        def list_books(session: Session = Depends(get_session)):
            ...
    """
    with Session(get_engine()) as session:
        yield session


__all__ = [
    "get_engine",
    "get_session",
    "init_schema",
    "reset_engine",
    "session_scope",
]
