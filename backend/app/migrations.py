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

    if "users" in tables:
        columns = _column_names(inspector, "users")
        dialect = engine.dialect.name
        bool_default = "FALSE" if dialect == "postgresql" else "0"
        additions: list[tuple[str, str]] = []
        if "role" not in columns:
            additions.append(("role", "ADD COLUMN role VARCHAR(16) DEFAULT 'member'"))
        if "email" not in columns:
            additions.append(("email", "ADD COLUMN email VARCHAR(255)"))
        if "disabled" not in columns:
            additions.append(("disabled", f"ADD COLUMN disabled BOOLEAN DEFAULT {bool_default}"))
        if "last_login_at" not in columns:
            additions.append(("last_login_at", "ADD COLUMN last_login_at TIMESTAMP"))
        if additions:
            with engine.begin() as conn:
                for name, clause in additions:
                    conn.execute(text(f"ALTER TABLE users {clause}"))
                    applied.append(f"users.{name}")
            logger.info("schema migration: added users columns %s", [name for name, _ in additions])

    # Create new tables (shares, imported_files, ...). Safe / idempotent.
    from . import models  # noqa: F401  (ensure all mappings are registered)

    Base.metadata.create_all(bind=engine)

    if "users" in set(inspect(engine).get_table_names()):
        from .models import User

        with SessionLocal() as db:
            db.execute(update(User).where(User.is_admin.is_(True)).values(role="admin"))
            db.execute(update(User).where(User.role.is_(None)).values(role="member"))
            db.execute(update(User).where(User.disabled.is_(None)).values(disabled=False))
            db.commit()

    return applied
