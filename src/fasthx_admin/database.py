"""
Database configuration helpers.

Call ``init_db(url)`` once at startup to create the SQLAlchemy engine and
session factory.  Then use ``Base`` for your models and ``get_db`` as a
FastAPI dependency.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, scoped_session, declarative_base, Session
from starlette.middleware.base import BaseHTTPMiddleware

Base = declarative_base()

_engine: Engine | None = None
_SessionLocal: scoped_session | None = None


def init_db(database_url: str, **engine_kwargs) -> Engine:
    """Initialise the database engine and session factory.

    Parameters
    ----------
    database_url:
        SQLAlchemy connection string, e.g. ``"sqlite:///./app.db"``.
    **engine_kwargs:
        Extra keyword arguments forwarded to ``create_engine``
        (e.g. ``connect_args={"check_same_thread": False}`` for SQLite).

    Returns
    -------
    Engine
        The newly created SQLAlchemy engine.
    """
    global _engine, _SessionLocal
    _engine = create_engine(database_url, **engine_kwargs)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
    _SessionLocal = scoped_session(session_factory)
    return _engine


def get_engine() -> Engine:
    """Return the current engine (raises if ``init_db`` was not called)."""
    if _engine is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")
    return _engine


def get_db():
    """FastAPI dependency that yields a database session."""
    if _SessionLocal is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")
    db: Session = _SessionLocal()
    try:
        yield db
    finally:
        db.close()


class DBSessionCleanupMiddleware(BaseHTTPMiddleware):
    """Middleware that removes scoped sessions after each request.

    This replicates Flask-SQLAlchemy's ``teardown_appcontext`` behavior,
    ensuring sessions are fully cleaned up and connections returned to the
    pool at the end of every request.
    """

    async def dispatch(self, request, call_next):
        try:
            response = await call_next(request)
            return response
        finally:
            if _SessionLocal is not None:
                _SessionLocal.remove()
