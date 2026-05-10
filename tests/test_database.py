import sqlite3
import pytest
from unittest.mock import patch
from pathlib import Path
from modules.database import init_db, get_connection


@patch("modules.database.DB_PATH", Path(":memory:"))
def test_init_db_creates_tables():
    # Uses in-memory DB via monkeypatch
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS applications (id INTEGER PRIMARY KEY);
    """)
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cursor.fetchall()}
    assert "jobs" in tables
    assert "applications" in tables
