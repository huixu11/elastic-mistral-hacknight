"""Persist the current resume locally and reuse its parsed text on re-upload.

The original document and text remain in the local single-user SQLite database.
Original bytes and cache hashes are never included in the public response.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .clients import Settings
from .resume import (
    SUPPORTED_EXTENSIONS,
    ResumeError,
    _clean_filename,
    _decode_content,
    _validated_text,
    parse_resume,
)


_LOCKS_GUARD = threading.Lock()
_UPLOAD_LOCKS: dict[str, Any] = {}
_SELECT_CURRENT = (
    "SELECT filename, text, original_text, parser, content_hash, updated_at, edited "
    "FROM app_resume WHERE id = 1"
)


def _lock_for(path: Path):
    key = os.path.normcase(str(path.resolve()))
    with _LOCKS_GUARD:
        return _UPLOAD_LOCKS.setdefault(key, threading.RLock())


def _public(row: sqlite3.Row | dict[str, Any], *, cached: bool = True) -> dict[str, Any]:
    return {
        "filename": row["filename"],
        "text": row["text"],
        "parser": row["parser"],
        "updated_at": row["updated_at"],
        "cached": cached,
        "edited": bool(row["edited"]),
    }


class ResumeStore:
    """One current resume, with independent connections for server threads.

    A shared per-database lock serializes uploads across instances in this
    process.  Parsing happens before starting the SQLite write transaction,
    so a slow OCR request does not block application-history writes.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.upload_lock = _lock_for(self.path)
        with self.upload_lock, closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS app_resume (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    filename TEXT NOT NULL,
                    text TEXT NOT NULL,
                    original_text TEXT NOT NULL,
                    parser TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    original BLOB NOT NULL,
                    updated_at TEXT NOT NULL,
                    edited INTEGER NOT NULL DEFAULT 0 CHECK (edited IN (0, 1))
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        # Scrub deleted/overwritten SQLite cells rather than retaining their
        # payload in free space.  Filesystem backups remain outside this store.
        connection.execute("PRAGMA secure_delete = ON")
        return connection

    def _current(self) -> sqlite3.Row | None:
        with closing(self._connect()) as connection:
            return connection.execute(_SELECT_CURRENT).fetchone()

    def get(self) -> dict[str, dict[str, Any] | None]:
        row = self._current()
        return {"resume": _public(row) if row is not None else None}

    def get_original(self) -> tuple[str, bytes] | None:
        """Internal access only; callers must not put these bytes in JSON."""
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT filename, original FROM app_resume WHERE id = 1").fetchone()
        return (row["filename"], bytes(row["original"])) if row is not None else None

    def upload(self, filename: str, content_base64: str, settings: Settings) -> dict[str, Any]:
        filename = _clean_filename(filename)
        extension = Path(filename).suffix.casefold()
        if extension not in SUPPORTED_EXTENSIONS:
            raise ResumeError("Supported resume formats are PDF, TXT, Markdown (.md), and DOCX. Select one of these formats.")
        content = _decode_content(content_base64)
        content_hash = hashlib.sha256(content + b"\x00" + extension.encode("ascii")).hexdigest()
        with self.upload_lock:
            current = self._current()
            if current is not None and current["content_hash"] == content_hash:
                if current["filename"] == filename:
                    return _public(current, cached=True)
                now = datetime.now(timezone.utc).isoformat()
                with closing(self._connect()) as connection, connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("UPDATE app_resume SET filename = ?, updated_at = ? WHERE id = 1", (filename, now))
                    renamed = connection.execute(_SELECT_CURRENT).fetchone()
                return _public(renamed, cached=True)

            # A failed parse/OCR leaves the previous resume and original bytes
            # intact.  Never replace the cached resume before parsing succeeds.
            parsed = parse_resume(filename, content_base64, settings)
            text = _validated_text(parsed["text"])
            now = datetime.now(timezone.utc).isoformat()
            row = {
                "filename": filename, "text": text, "original_text": text,
                "parser": parsed["parser"], "content_hash": content_hash,
                "original": content, "updated_at": now, "edited": 0,
            }
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO app_resume (id, filename, text, original_text, parser, content_hash, original, updated_at, edited) "
                    "VALUES (1, ?, ?, ?, ?, ?, ?, ?, 0) "
                    "ON CONFLICT(id) DO UPDATE SET filename = excluded.filename, text = excluded.text, "
                    "original_text = excluded.original_text, parser = excluded.parser, content_hash = excluded.content_hash, "
                    "original = excluded.original, updated_at = excluded.updated_at, edited = 0",
                    (filename, text, text, row["parser"], content_hash, sqlite3.Binary(content), now),
                )
            return _public(row, cached=False)

    def update_text(self, text: str) -> dict[str, Any]:
        if not isinstance(text, str):
            raise ResumeError("Resume content must be text.")
        text = _validated_text(text)
        with self.upload_lock:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(_SELECT_CURRENT).fetchone()
                if current is None:
                    raise ResumeError("Upload a resume before editing its text.")
                if current["text"] == text:
                    return _public(current)
                edited = int(text != current["original_text"])
                now = datetime.now(timezone.utc).isoformat()
                connection.execute("UPDATE app_resume SET text = ?, edited = ?, updated_at = ? WHERE id = 1", (text, edited, now))
                row = connection.execute(_SELECT_CURRENT).fetchone()
            return _public(row)

    def clear(self) -> dict[str, None]:
        with self.upload_lock, closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM app_resume WHERE id = 1")
        return {"resume": None}
