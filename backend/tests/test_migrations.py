"""Schema migration tests for the v0.1 -> v0.2 upgrade path (Lead-owned)."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app import migrations


def _make_old_v01_schema(db_path):
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username VARCHAR(64) UNIQUE,
            password_hash VARCHAR(255),
            display_name VARCHAR(120),
            is_admin BOOLEAN,
            created_at DATETIME
        )
        """
    )
    con.execute(
        "INSERT INTO users (username, password_hash, display_name, is_admin, created_at) "
        "VALUES ('admin', 'hash', '管理员', 1, '2026-01-01 00:00:00')"
    )
    con.commit()
    con.close()


@pytest.fixture()
def isolated_engine(tmp_path, monkeypatch):
    db_file = tmp_path / "old.db"
    _make_old_v01_schema(db_file)
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    session_local = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(migrations, "engine", engine)
    monkeypatch.setattr(migrations, "SessionLocal", session_local)
    yield engine
    engine.dispose()


def test_migrates_v01_users_and_creates_v02_tables(isolated_engine):
    applied = migrations.run_migrations()
    assert "users.role" in applied
    assert "users.disabled" in applied

    inspector = inspect(isolated_engine)
    columns = {column["name"] for column in inspector.get_columns("users")}
    assert {"role", "email", "disabled", "last_login_at"} <= columns
    tables = set(inspector.get_table_names())
    assert {"shares", "imported_files"} <= tables

    with isolated_engine.connect() as conn:
        row = conn.execute(text("SELECT username, role, disabled FROM users WHERE username='admin'")).one()
    assert row[0] == "admin"
    assert row[1] == "admin"  # backfilled from is_admin
    assert not row[2]


def test_migration_is_idempotent(isolated_engine):
    first = migrations.run_migrations()
    assert first
    second = migrations.run_migrations()
    assert second == []


def test_migrates_v02_to_v03_columns_and_tables(tmp_path, monkeypatch):
    """v0.2 -> v0.3 upgrade: trash columns, embedding_model, live tables."""
    db_path = tmp_path / "v02.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, username VARCHAR(64) UNIQUE, password_hash VARCHAR(255),
            display_name VARCHAR(120), is_admin BOOLEAN, role VARCHAR(16), email VARCHAR(255),
            disabled BOOLEAN, last_login_at TIMESTAMP, created_at DATETIME
        );
        INSERT INTO users (username,password_hash,display_name,is_admin,role,disabled,created_at)
        VALUES ('admin','h','Admin',1,'admin',0,'2026-01-01');
        CREATE TABLE recordings (
            id INTEGER PRIMARY KEY, uuid VARCHAR(36), title VARCHAR(255), status VARCHAR(24),
            owner_id INTEGER, created_at DATETIME
        );
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY, recording_id INTEGER, idx INTEGER, text TEXT,
            embedding BLOB, dim INTEGER
        );
        """
    )
    con.close()
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    session_local = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(migrations, "engine", engine)
    monkeypatch.setattr(migrations, "SessionLocal", session_local)
    try:
        applied = migrations.run_migrations()
        assert "recordings.deleted_at" in applied
        assert "recordings.deleted_by" in applied
        assert "chunks.embedding_model" in applied
        inspector = inspect(engine)
        recording_cols = {c["name"] for c in inspector.get_columns("recordings")}
        assert {"deleted_at", "deleted_by"} <= recording_cols
        chunk_cols = {c["name"] for c in inspector.get_columns("chunks")}
        assert "embedding_model" in chunk_cols
        tables = set(inspector.get_table_names())
        assert {"live_sessions", "live_segments"} <= tables
    finally:
        engine.dispose()
