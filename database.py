"""
Database layer — SQLite with sqlite3.
"""

import sqlite3
import random
import string
from datetime import datetime

import os as _os
DB_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "links.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create / migrate tables."""
    with _connect() as conn:
        # ── links table ──────────────────────────────────────────────────────
        table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='links'"
        ).fetchone()

        if table_exists:
            cols_info = conn.execute("PRAGMA table_info(links)").fetchall()
            existing = {row[1]: row for row in cols_info}

            # Add legacy file columns if missing (kept for backward compat)
            for col, definition in [("file_id", "TEXT"), ("file_type", "TEXT")]:
                if col not in existing:
                    conn.execute(f"ALTER TABLE links ADD COLUMN {col} {definition}")

            # Recreate if target_url still has NOT NULL constraint
            target_notnull = existing.get("target_url")
            if target_notnull and target_notnull[3] == 1:
                conn.execute(
                    """
                    CREATE TABLE links_new (
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        key          TEXT      NOT NULL UNIQUE,
                        content_text TEXT      NOT NULL,
                        target_url   TEXT,
                        file_id      TEXT,
                        file_type    TEXT,
                        created_at   TIMESTAMP NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO links_new
                        (id, key, content_text, target_url, created_at)
                    SELECT id, key, content_text, target_url, created_at
                    FROM links
                    """
                )
                conn.execute("DROP TABLE links")
                conn.execute("ALTER TABLE links_new RENAME TO links")
        else:
            conn.execute(
                """
                CREATE TABLE links (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    key          TEXT      NOT NULL UNIQUE,
                    content_text TEXT      NOT NULL,
                    target_url   TEXT,
                    file_id      TEXT,
                    file_type    TEXT,
                    created_at   TIMESTAMP NOT NULL
                )
                """
            )

        # ── link_files table (multiple files per link) ───────────────────────
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS link_files (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                link_key   TEXT    NOT NULL,
                file_id    TEXT    NOT NULL,
                file_type  TEXT    NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (link_key) REFERENCES links(key) ON DELETE CASCADE
            )
            """
        )

        # Migrate existing single-file links into link_files
        rows = conn.execute(
            "SELECT key, file_id, file_type FROM links WHERE file_id IS NOT NULL"
        ).fetchall()
        for row in rows:
            already = conn.execute(
                "SELECT 1 FROM link_files WHERE link_key = ?", (row["key"],)
            ).fetchone()
            if not already:
                conn.execute(
                    "INSERT INTO link_files (link_key, file_id, file_type, sort_order) VALUES (?, ?, ?, 0)",
                    (row["key"], row["file_id"], row["file_type"]),
                )

        conn.commit()


# ── Key generation ────────────────────────────────────────────────────────────

def _generate_key(length: int = 8) -> str:
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choices(chars, k=length))


# ── CRUD ──────────────────────────────────────────────────────────────────────

def create_link(
    content_text: str,
    target_url: str | None = None,
    files: list[tuple[str, str]] | None = None,   # [(file_id, file_type), ...]
) -> str:
    """
    Insert a new link and return its key.
    Pass either target_url OR a non-empty files list.
    """
    if not target_url and not files:
        raise ValueError("Either target_url or files must be provided.")

    with _connect() as conn:
        for _ in range(10):
            key = _generate_key()
            try:
                conn.execute(
                    """
                    INSERT INTO links (key, content_text, target_url, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (key, content_text, target_url, datetime.utcnow()),
                )
                if files:
                    for i, (fid, ftype) in enumerate(files):
                        conn.execute(
                            "INSERT INTO link_files (link_key, file_id, file_type, sort_order) VALUES (?, ?, ?, ?)",
                            (key, fid, ftype, i),
                        )
                conn.commit()
                return key
            except sqlite3.IntegrityError as exc:
                if "UNIQUE" in str(exc):
                    continue
                raise
    raise RuntimeError("Failed to generate a unique key after 10 attempts.")


def get_link(key: str) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM links WHERE key = ?", (key,)
        ).fetchone()


def get_link_files(key: str) -> list[sqlite3.Row]:
    """Return all files for a link, ordered by sort_order."""
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM link_files WHERE link_key = ? ORDER BY sort_order",
            (key,),
        ).fetchall()


def update_link(
    key: str,
    content_text: str | None = None,
    target_url: str | None = None,
    files: list[tuple[str, str]] | None = None,   # replace all files
    clear_files: bool = False,
    clear_url: bool = False,
) -> bool:
    """
    Update a link. Pass files to replace all existing files for this link.
    Returns True if the key existed.
    """
    with _connect() as conn:
        sets, params = [], []
        if content_text is not None:
            sets.append("content_text = ?")
            params.append(content_text)
        if target_url is not None:
            sets.append("target_url = ?")
            params.append(target_url)
        if clear_url:
            sets.append("target_url = NULL")
        if sets:
            params.append(key)
            cur = conn.execute(
                f"UPDATE links SET {', '.join(sets)} WHERE key = ?", params
            )
            if cur.rowcount == 0:
                return False

        if clear_files or files is not None:
            conn.execute("DELETE FROM link_files WHERE link_key = ?", (key,))

        if files:
            for i, (fid, ftype) in enumerate(files):
                conn.execute(
                    "INSERT INTO link_files (link_key, file_id, file_type, sort_order) VALUES (?, ?, ?, ?)",
                    (key, fid, ftype, i),
                )

        conn.commit()
        return True


def delete_link(key: str) -> bool:
    with _connect() as conn:
        conn.execute("DELETE FROM link_files WHERE link_key = ?", (key,))
        cur = conn.execute("DELETE FROM links WHERE key = ?", (key,))
        conn.commit()
        return cur.rowcount > 0


def list_links() -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM links ORDER BY created_at DESC"
        ).fetchall()


def search_links(query: str, limit: int = 20) -> list[sqlite3.Row]:
    """Full-text search over content_text (case-insensitive LIKE)."""
    pattern = f"%{query}%"
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM links WHERE content_text LIKE ? ORDER BY created_at DESC LIMIT ?",
            (pattern, limit),
        ).fetchall()
