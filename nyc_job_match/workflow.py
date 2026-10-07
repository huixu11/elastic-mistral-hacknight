"""Local single-user application history and notifications.

Only minimal job metadata and self-reported application states are stored.
Opening a link cannot verify an application on a third-party website.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


DEFAULT_APPLY_URL = "https://cityjobs.nyc.gov/"
OPENED = "opened"
SUBMITTED = "submitted_self_reported"


def _apply_url(job: dict[str, Any]) -> str:
    url = job.get("apply_url")
    if url is None or url == "":
        return DEFAULT_APPLY_URL
    if not isinstance(url, str):
        raise ValueError("Invalid official application URL.")
    url = url.strip()
    if not url:
        return DEFAULT_APPLY_URL
    if "\\" in url or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in url):
        raise ValueError("The application URL must be a valid HTTPS URL.")
    try:
        parts = urlsplit(url)
        valid = parts.scheme == "https" and bool(parts.hostname) and parts.username is None and parts.password is None
        # Accessing port validates malformed or out-of-range port values.
        parts.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("The application URL must use HTTPS and contain no username or password.")
    return url


def _job_metadata(job: dict[str, Any]) -> dict[str, str]:
    if not isinstance(job, dict):
        raise ValueError("Invalid job record.")
    job_id = str(job.get("job_id") or "").strip()
    key = job.get("application_key")
    if key is not None and not isinstance(key, str):
        raise ValueError("Invalid application identifier.")
    key = (key or "").strip() or ("nyc:" + job_id if job_id else "")
    if not key:
        raise ValueError("A job ID is required to record application status.")
    return {
        "job_key": key,
        "job_id": job_id,
        "business_title": str(job.get("business_title") or "").strip(),
        "agency": str(job.get("agency") or "").strip(),
        "provider": str(job.get("provider") or "nyc").strip() or "nyc",
    }


class WorkflowStore:
    """SQLite store with one connection per operation, safe for server threads."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS applications (
                    job_key TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    business_title TEXT NOT NULL,
                    agency TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('opened', 'submitted_self_reported')),
                    updated_at TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(job_key, status),
                    FOREIGN KEY(job_key) REFERENCES applications(job_key)
                )
            """)
            connection.execute("CREATE TABLE IF NOT EXISTS application_confirmations (job_key TEXT PRIMARY KEY REFERENCES applications(job_key), receipt_url TEXT NOT NULL, confirmed_at TEXT NOT NULL)")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def list(self) -> dict[str, list[dict[str, Any]]]:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN")
            applications = [dict(row) for row in connection.execute(
                "SELECT a.job_key, a.job_id, a.business_title, a.agency, a.provider, "
                "CASE WHEN c.job_key IS NOT NULL THEN 'submitted_confirmed' ELSE a.status END AS status, "
                "COALESCE(c.confirmed_at,a.updated_at) AS updated_at "
                "FROM applications a LEFT JOIN application_confirmations c ON a.job_key=c.job_key ORDER BY updated_at DESC, a.job_key ASC"
            )]
            notifications = [dict(row) for row in connection.execute(
                "SELECT n.id, n.message, n.created_at, n.status, a.business_title FROM notifications n "
                "JOIN applications a ON a.job_key=n.job_key ORDER BY n.id DESC"
            )]
            for notification in notifications:
                title = notification.pop("business_title")
                status = notification.pop("status")
                if status == "submitted_confirmed":
                    notification["message"] = "The employer site confirmed your application: " + title + "."
                elif status == SUBMITTED:
                    notification["message"] = "You marked this job applied: " + title + "."
                else:
                    notification["message"] = "Application link opened: " + title + ". Complete your application on the employer site."
        return {"applications": applications, "notifications": notifications}

    def submitted_keys(self) -> set[str]:
        with closing(self._connect()) as connection:
            return {row["job_key"] for row in connection.execute(
                "SELECT job_key FROM applications WHERE status = ? UNION SELECT job_key FROM application_confirmations", (SUBMITTED,)
            )}

    def confirm(self, job, receipt_url):
        """Called by the local browser helper after an explicit official receipt."""
        metadata = _job_metadata(job)
        url = _apply_url({"apply_url": receipt_url})
        parts = urlsplit(url)
        expected = f"/{job.get('board')}/jobs/{job.get('job_id')}"
        if parts.hostname not in ("job-boards.greenhouse.io", "boards.greenhouse.io") or not (parts.path == expected or parts.path.startswith(expected + "/")):
            raise ValueError("The receipt URL does not match this job.")
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT OR IGNORE INTO applications VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (*metadata.values(), OPENED, now))
            connection.execute("INSERT OR IGNORE INTO application_confirmations VALUES (?, ?, ?)", (metadata["job_key"], url, now))
            connection.execute("INSERT OR IGNORE INTO notifications (job_key,status,message,created_at) VALUES (?,?,?,?)",
                               (metadata["job_key"], "submitted_confirmed", "The employer site confirmed your application: " + metadata["business_title"] + ".", now))

    def record(self, job: dict[str, Any], action: str) -> dict[str, Any]:
        if action not in {"open", "mark_submitted"}:
            raise ValueError("Supported actions are opening the employer site or marking a self-reported submission.")
        metadata = _job_metadata(job)
        apply_url = _apply_url(job)
        desired_status = OPENED if action == "open" else SUBMITTED
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT job_key, job_id, business_title, agency, provider, status, updated_at "
                "FROM applications WHERE job_key = ?", (metadata["job_key"],)
            ).fetchone()
            confirmation = connection.execute("SELECT confirmed_at FROM application_confirmations WHERE job_key=?", (metadata["job_key"],)).fetchone()
            if confirmation is not None and existing is not None:
                application = dict(existing, status="submitted_confirmed", updated_at=confirmation["confirmed_at"])
                return {"application": application, "apply_url": apply_url, "message": "An employer-site receipt has already been recorded for this job."}
            # A repeated event is a read of the existing state, including its
            # original timestamp.  A reopened link never clears a self-report.
            if existing is not None and (existing["status"] == desired_status or existing["status"] == SUBMITTED):
                application = dict(existing)
                if existing["status"] == SUBMITTED:
                    message = "You already marked this job applied. The original record was kept." if action == "mark_submitted" else "You marked this job applied. Reopening the site keeps that status."
                else:
                    message = "The application link was already opened. Complete your application on the employer site."
                return {"application": application, "apply_url": apply_url, "message": message}

            now = datetime.now(timezone.utc).isoformat()
            application = {**metadata, "status": desired_status, "updated_at": now}
            values = tuple(application[name] for name in (
                "job_key", "job_id", "business_title", "agency", "provider", "status", "updated_at"
            ))
            connection.execute(
                "INSERT INTO applications (job_key, job_id, business_title, agency, provider, status, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(job_key) DO UPDATE SET job_id = excluded.job_id, "
                "business_title = excluded.business_title, agency = excluded.agency, "
                "provider = excluded.provider, status = excluded.status, updated_at = excluded.updated_at",
                values,
            )
            title = application["business_title"] or application["job_id"] or application["job_key"]
            message = f"You marked this job applied: {title}." if desired_status == SUBMITTED else f"Application link opened: {title}. Complete your application on the employer site."
            connection.execute(
                "INSERT OR IGNORE INTO notifications (job_key, status, message, created_at) VALUES (?, ?, ?, ?)",
                (application["job_key"], desired_status, message, now),
            )
        return {"application": application, "apply_url": apply_url, "message": message}
