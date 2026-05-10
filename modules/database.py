"""SQLite database layer for tracking jobs and applications."""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "resumeai.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT NOT NULL,
                company     TEXT NOT NULL,
                url         TEXT UNIQUE NOT NULL,
                location    TEXT DEFAULT '',
                salary      TEXT DEFAULT '',
                date_posted TEXT DEFAULT '',
                source      TEXT DEFAULT '',
                description TEXT DEFAULT '',
                status      TEXT DEFAULT 'new',
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS applications (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id         INTEGER REFERENCES jobs(id),
                resume_path    TEXT,
                cover_letter   TEXT,
                applied_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
                outcome        TEXT DEFAULT 'pending'
            );
        """)
        # Migrate existing databases that predate the extra columns
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after initial schema — safe to run repeatedly."""
    job_cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    for col, defn in {
        "location":    "TEXT DEFAULT ''",
        "salary":      "TEXT DEFAULT ''",
        "date_posted": "TEXT DEFAULT ''",
        "source":      "TEXT DEFAULT ''",
    }.items():
        if col not in job_cols:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {defn}")

    app_cols = {row[1] for row in conn.execute("PRAGMA table_info(applications)")}
    if "match_score" not in app_cols:
        conn.execute("ALTER TABLE applications ADD COLUMN match_score INTEGER DEFAULT 0")
