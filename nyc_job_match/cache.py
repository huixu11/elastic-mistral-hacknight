"""Local cache of successful structured model responses, keyed by exact input.

The cache keeps derived JSON and a hash of the request, never API keys or raw
resume/request text. Elasticsearch retrieval still runs for every search.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clients import ServiceError


class ModelCache:
    def __init__(self, path, ttl_hours=24, max_entries=128):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl = timedelta(hours=ttl_hours)
        self.max_entries = max_entries
        self.lock = threading.RLock()
        with closing(self.connect()) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS model_cache (cache_key TEXT PRIMARY KEY, response TEXT NOT NULL, created_at TEXT NOT NULL)")

    def connect(self):
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.execute("PRAGMA secure_delete = ON")
        return connection

    def session(self):
        return CacheSession(self)

    def clear(self):
        with self.lock, closing(self.connect()) as connection, connection:
            connection.execute("DELETE FROM model_cache")

    def call(self, model, messages, schema, name, invoke):
        encoded = json.dumps({"version": 1, "model": model, "messages": messages, "schema": schema, "name": name},
                             sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        key = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc)
        with self.lock:
            with closing(self.connect()) as connection, connection:
                row = connection.execute("SELECT response, created_at FROM model_cache WHERE cache_key=?", (key,)).fetchone()
                if row:
                    try:
                        value, created = json.loads(row[0]), datetime.fromisoformat(row[1])
                        if isinstance(value, dict) and now - self.ttl <= created <= now:
                            return value, True, row[1]
                    except (ValueError, TypeError):
                        pass
                    connection.execute("DELETE FROM model_cache WHERE cache_key=?", (key,))
            # Do not hold a database transaction across a network request.
            value = invoke()
            if not isinstance(value, dict):
                raise ServiceError("Mistral returned an invalid structured response.")
            serialized = json.dumps(value, ensure_ascii=False, allow_nan=False)
            stamp = now.isoformat()
            with closing(self.connect()) as connection, connection:
                connection.execute("INSERT OR REPLACE INTO model_cache VALUES (?, ?, ?)", (key, serialized, stamp))
                connection.execute("DELETE FROM model_cache WHERE cache_key NOT IN (SELECT cache_key FROM model_cache ORDER BY created_at DESC LIMIT ?)", (self.max_entries,))
            return value, False, stamp


class CacheSession:
    def __init__(self, store):
        self.store = store
        self.hits = 0
        self.misses = 0
        self.cached_at = None

    def structured(self, settings, messages, schema, name, invoke):
        def counted_invoke():
            self.misses += 1
            return invoke()
        result, hit, stamp = self.store.call(settings.mistral_model, messages, schema, name, counted_invoke)
        if hit:
            self.hits += 1
            self.cached_at = min(self.cached_at, stamp) if self.cached_at else stamp
        return result

    def stats(self):
        return {"hits": self.hits, "misses": self.misses}
