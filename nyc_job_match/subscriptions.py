"""Local saved search subscriptions and deterministic new-job notifications."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from .career import stage_matches
from .engine import application_key, eligible_jobs, valid_salary


STAGES = {"any", "new_grad", "entry_level", "senior", "internship"}


def _keywords(value, name):
    if not isinstance(value, list) or len(value) > 12:
        raise ValueError(f"{name} must be a list of at most 12 keywords.")
    result, seen = [], set()
    for word in value:
        if not isinstance(word, str) or not word.strip() or len(word) > 80:
            raise ValueError(f"Each {name} entry must contain 1 to 80 characters.")
        word = word.strip()
        if word.casefold() not in seen:
            result.append(word)
            seen.add(word.casefold())
    return result


def _plan(plan):
    if not isinstance(plan, dict):
        raise ValueError("A validated search plan is required.")
    stage = plan.get("career_stage", "any")
    if not isinstance(stage, str) or stage not in STAGES:
        raise ValueError("Choose a valid subscription career stage.")
    # Search text, resume, model notes and API settings are intentionally omitted.
    return {
        "keywords": _keywords(plan.get("keywords", []), "keywords"),
        "title_keywords": _keywords(plan.get("title_keywords", []), "title_keywords"),
        "min_annual_salary": valid_salary(plan.get("min_annual_salary", 0)),
        "career_stage": stage,
    }


def _contains(text, words):
    return any(re.search(r"(?<!\w)" + re.escape(word.casefold()) + r"(?!\w)", text.casefold()) for word in words)


def _matches(jobs, plan, excluded_keys):
    matches = {}
    for job in eligible_jobs(jobs, plan["min_annual_salary"]):
        key = application_key(job)
        if key in excluded_keys or key in matches or not stage_matches(job, plan["career_stage"]):
            continue
        title = str(job.get("business_title") or "")
        if plan["title_keywords"] and not _contains(title, plan["title_keywords"]):
            continue
        text = "\n".join(str(job.get(field) or "") for field in (
            "business_title", "preferred_skills", "job_description", "minimum_qual_requirements"))
        if plan["keywords"] and not _contains(text, plan["keywords"]):
            continue
        matches[key] = job
    return matches


def _url(job):
    url = job.get("apply_url") or job.get("source_url") or "https://cityjobs.nyc.gov/"
    if isinstance(url, str) and not any(character.isspace() or ord(character) < 32 for character in url) and "\\" not in url:
        try:
            parts = urlsplit(url)
            parts.port
            if parts.scheme == "https" and parts.hostname and parts.username is None and parts.password is None:
                return url
        except ValueError:
            pass
    return "https://cityjobs.nyc.gov/"


class SubscriptionStore:
    """SQLite transactions make each subscription/job notification occur once."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""CREATE TABLE IF NOT EXISTS job_subscriptions (
                id TEXT PRIMARY KEY, label TEXT NOT NULL, enabled INTEGER NOT NULL,
                plan TEXT NOT NULL, created_at TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS subscription_seen (
                subscription_id TEXT NOT NULL REFERENCES job_subscriptions(id) ON DELETE CASCADE,
                job_key TEXT NOT NULL, PRIMARY KEY (subscription_id, job_key))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS subscription_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id TEXT NOT NULL REFERENCES job_subscriptions(id) ON DELETE CASCADE,
                job_key TEXT NOT NULL, title TEXT NOT NULL, message TEXT NOT NULL,
                url TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE (subscription_id, job_key))""")

    def _connect(self):
        connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _subscription(row):
        plan = json.loads(row["plan"])
        return {"id": row["id"], "label": row["label"], "enabled": bool(row["enabled"]),
                "career_stage": plan["career_stage"], "created_at": row["created_at"], "plan": plan}

    def save(self, plan, label, jobs, excluded_keys=()):
        plan = _plan(plan)
        if not isinstance(label, str) or not label.strip() or len(label) > 120:
            raise ValueError("A subscription label must contain 1 to 120 characters.")
        row = {"id": uuid4().hex, "label": label.strip(), "enabled": 1,
               "plan": json.dumps(plan, ensure_ascii=False), "created_at": datetime.now(timezone.utc).isoformat()}
        baseline = _matches(jobs, plan, set(excluded_keys or ()))
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO job_subscriptions (id,label,enabled,plan,created_at) VALUES (?,?,?,?,?)",
                               tuple(row[field] for field in ("id", "label", "enabled", "plan", "created_at")))
            connection.executemany("INSERT INTO subscription_seen (subscription_id,job_key) VALUES (?,?)",
                                   ((row["id"], key) for key in baseline))
        return self._subscription(row)

    def list(self):
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN")
            subscriptions = [self._subscription(row) for row in connection.execute(
                "SELECT * FROM job_subscriptions ORDER BY created_at DESC,id")]
            notifications = [dict(row) for row in connection.execute(
                "SELECT id,title,message,url,created_at FROM subscription_notifications ORDER BY id DESC")]
        return {"subscriptions": subscriptions, "notifications": notifications}

    def remove(self, subscription_id):
        if not isinstance(subscription_id, str) or not subscription_id or len(subscription_id) > 128:
            raise ValueError("A valid subscription ID is required.")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute("DELETE FROM job_subscriptions WHERE id=?", (subscription_id,))
        return {"removed": bool(result.rowcount)}

    def check(self, jobs, excluded_keys=()):
        jobs, excluded = list(jobs), set(excluded_keys or ())
        notifications = []
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            subscriptions = list(connection.execute("SELECT * FROM job_subscriptions WHERE enabled=1 ORDER BY id"))
            for subscription in subscriptions:
                plan = json.loads(subscription["plan"])
                for key, job in _matches(jobs, plan, excluded).items():
                    inserted = connection.execute("INSERT OR IGNORE INTO subscription_seen (subscription_id,job_key) VALUES (?,?)",
                                                  (subscription["id"], key))
                    if not inserted.rowcount:
                        continue
                    title = str(job.get("business_title") or "Job")[:250]
                    agency = str(job.get("agency") or "Employer")[:200]
                    record = {"title": "New match: " + title,
                              "message": f"{agency}: {title} matches your saved search '{subscription['label']}'.",
                              "url": _url(job), "created_at": now}
                    result = connection.execute("INSERT INTO subscription_notifications (subscription_id,job_key,title,message,url,created_at) VALUES (?,?,?,?,?,?)",
                                                (subscription["id"], key, record["title"], record["message"], record["url"], now))
                    notifications.append({"id": result.lastrowid, **record})
        return {"new_notifications": len(notifications), "notifications": notifications}
