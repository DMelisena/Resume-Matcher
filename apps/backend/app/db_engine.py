"""Postgres (Supabase) engine/session plumbing for the SQLAlchemy data layer.

Every ``Database`` instance owns its own engines (one async for the document
tables, one sync for the encrypted ``api_keys`` table read on the synchronous
LLM hot path) built from these factories. Keeping construction here lets tests
spin up fully isolated engines against a disposable Postgres database.

The app connects as the Postgres ``authenticated`` role (never the
service-role/superuser), so Row-Level Security policies are always enforced
by the database itself — see ``supabase/migrations/`` for the policies and
``app/database.py::Database._user_session`` for how the per-request
``request.jwt.claims`` GUC (which the policies' ``auth.uid()`` calls read) is
set for each transaction.
"""

from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.models import Base

__all__ = ["Base", "make_async_engine", "make_sync_engine", "init_models_sync"]


def make_async_engine(dsn: str) -> AsyncEngine:
    """Create the async engine (``asyncpg``) for the document tables.

    ``pool_pre_ping`` is deliberately off: pinging toggles the DBAPI
    connection's autocommit flag, which psycopg2/asyncpg refuse mid
    -transaction — and every checked-out connection here is mid-transaction
    almost immediately (``SET LOCAL ROLE`` + the RLS claim GUC). Pre-ping and
    this session model are mutually exclusive; a dead connection still surfaces
    normally as a query error instead.
    """
    engine = create_async_engine(dsn, future=True, pool_pre_ping=False)
    event.listen(engine.sync_engine, "checkin", _reset_role_on_return)
    return engine


def make_sync_engine(dsn: str) -> Engine:
    """Create the sync engine used for the encrypted api_keys table.

    Key reads happen synchronously (``get_llm_config`` → ``load_config_file`` →
    ``resolve_api_key``), so a sync engine avoids threading async through
    ``llm.py``. It points at the same Postgres database as the async engine.
    See ``make_async_engine`` for why ``pool_pre_ping`` stays off.
    """
    engine = create_engine(dsn, future=True, pool_pre_ping=False)
    event.listen(engine, "checkin", _reset_role_on_return)
    return engine


def _reset_role_on_return(dbapi_connection: Any, _connection_record: Any) -> None:
    """Defensive belt-and-suspenders: clear any leaked session-local GUCs.

    ``SET LOCAL`` / transaction-scoped ``set_config`` already resets at commit
    /rollback, but a pooled connection recycled after an aborted transaction
    could otherwise retain a stale ``request.jwt.claims``. Runs on checkin so
    the next borrower never inherits another request's identity.

    ``dbapi_connection`` can be ``None`` here — SQLAlchemy fires ``checkin``
    even when checkout itself failed (e.g. the connection never opened, or
    was already closed), so there is nothing to reset.
    """
    if dbapi_connection is None:
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("RESET request.jwt.claims")
    except Exception:  # noqa: BLE001 - best-effort cleanup only
        pass
    finally:
        cursor.close()


def init_models_sync(engine: Engine) -> None:
    """Create all tables (idempotent) using a sync engine connection.

    This is a dev/test convenience only — the authoritative schema (including
    RLS policies, which SQLAlchemy metadata cannot express) lives in
    ``supabase/migrations/*.sql`` and is applied via the Supabase CLI/dashboard
    in real deployments.
    """
    Base.metadata.create_all(engine)
