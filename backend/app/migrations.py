"""Lightweight, idempotent schema migrations.

Echoic upgrades existing installations in place on startup: ``init_db()`` creates any
missing tables via SQLAlchemy metadata, then this module adds columns that were
introduced after a table already existed (SQLAlchemy ``create_all`` never alters
existing tables). Designed to work on both SQLite and PostgreSQL.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text, update

from .db import Base, SessionLocal, engine

logger = logging.getLogger(__name__)


def _column_names(inspector, table: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table)}


def run_migrations() -> list[str]:
    """Apply pending migrations. Returns a list of human-readable migration names."""
    applied: list[str] = []
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    dialect = engine.dialect.name
    bool_default = "FALSE" if dialect == "postgresql" else "0"

    add_columns: dict[str, list[tuple[str, str]]] = {
        "users": [
            ("role", "ADD COLUMN role VARCHAR(16) DEFAULT 'member'"),
            ("email", "ADD COLUMN email VARCHAR(255)"),
            ("disabled", f"ADD COLUMN disabled BOOLEAN DEFAULT {bool_default}"),
            ("last_login_at", "ADD COLUMN last_login_at TIMESTAMP"),
        ],
        "recordings": [
            ("deleted_at", "ADD COLUMN deleted_at TIMESTAMP"),
            ("deleted_by", "ADD COLUMN deleted_by INTEGER"),
        ],
        "chunks": [
            ("embedding_model", "ADD COLUMN embedding_model VARCHAR(120)"),
        ],
    }

    for table, specs in add_columns.items():
        if table not in tables:
            continue
        columns = _column_names(inspector, table)
        additions = [(name, clause) for name, clause in specs if name not in columns]
        if not additions:
            continue
        with engine.begin() as conn:
            for name, clause in additions:
                conn.execute(text(f"ALTER TABLE {table} {clause}"))
                applied.append(f"{table}.{name}")
        logger.info("schema migration: added %s columns %s", table, [name for name, _ in additions])

    # Create new tables (shares, imported_files, live_sessions, live_segments, ...).
    from . import models  # noqa: F401  (ensure all mappings are registered)

    Base.metadata.create_all(bind=engine)

    # Indexes introduced after v0.2 (idempotent).
    if "recordings" in set(inspect(engine).get_table_names()):
        try:
            with engine.begin() as conn:
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recordings_deleted_at ON recordings (deleted_at)"))
        except Exception:  # pragma: no cover - very old SQLite; index is an optimization only
            logger.warning("could not create recordings.deleted_at index", exc_info=True)

    if "users" in set(inspect(engine).get_table_names()):
        from .models import User

        with SessionLocal() as db:
            db.execute(update(User).where(User.is_admin.is_(True)).values(role="admin"))
            db.execute(update(User).where(User.role.is_(None)).values(role="member"))
            db.execute(update(User).where(User.disabled.is_(None)).values(disabled=False))
            db.commit()

    return applied
