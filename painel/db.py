import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "/data/djdarr.db")

# Estados válidos (CHECK constraint no schema)
STATES = ("pending", "approved", "downloading", "ready", "failed", "played", "rejected")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Dois processos (internal:8001 e api:8501) compartilham este banco;
    # espera até 5s por um lock em vez de falhar com "database is locked".
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def get_db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def init_db() -> None:
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS requests (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                query               TEXT    NOT NULL,
                submitted_at        TEXT    NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                submitter_ip        TEXT,
                status              TEXT    NOT NULL DEFAULT 'pending'
                    CHECK(status IN (
                        'pending','approved','downloading',
                        'ready','failed','played','rejected'
                    )),
                approved_at         TEXT,
                download_started_at TEXT,
                download_finished_at TEXT,
                file_path           TEXT,
                error_message       TEXT,
                matched_title       TEXT,
                source              TEXT,
                retry_count         INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_requests_status
                ON requests(status);
            CREATE INDEX IF NOT EXISTS idx_requests_submitted
                ON requests(submitted_at);

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)

        # Migração incremental: colunas adicionadas depois do schema inicial.
        for column, decl in (
            ("title", "TEXT"),
            ("artist", "TEXT"),
            ("thumbnail_path", "TEXT"),
        ):
            if not _column_exists(conn, "requests", column):
                conn.execute(f"ALTER TABLE requests ADD COLUMN {column} {decl}")


def get_setting(key: str, default: str) -> str:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT INTO settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, value),
        )
