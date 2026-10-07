"""Refresh connected sources, then publish one atomic retrieval snapshot."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .data import SOURCE_URL, fetch_snapshot, save_snapshot
from .job_sources import fetch_private_jobs


def refresh_snapshot(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the previous mixed snapshot until government pagination validates.
    with tempfile.TemporaryDirectory(prefix=".job-refresh-", dir=path.parent) as staging:
        government = fetch_snapshot(Path(staging) / "government.json")
    private = fetch_private_jobs()
    snapshot = {**government, "jobs": government["jobs"] + private["jobs"],
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "sources": [{"provider": "nyc", "company": "NYC Government", "status": "ok",
                             "job_count": len(government["jobs"]), "source_url": SOURCE_URL,
                             "fetched_at": government["fetched_at"],
                             "source_updated_at": government.get("source_updated_at")}] + private["sources"],
                "warnings": private["warnings"]}
    save_snapshot(path, snapshot)
    return snapshot
