import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from nyc_job_match.clients import Settings
from nyc_job_match.data import SOURCE_URL, save_snapshot
from nyc_job_match.server import App, REFRESH_INTERVAL


def posting(key, title="Senior Software Engineer"):
    return {"job_id": key, "posting_id": key + "-v1", "application_key": "nyc:" + key,
            "business_title": title, "agency": "Example", "provider": "nyc", "posting_type": "External",
            "salary_frequency": "Annual", "salary_range_from": 100000, "salary_range_to": 150000,
            "job_description": "Python software development", "preferred_skills": "Python",
            "minimum_qual_requirements": "", "post_until": "31-DEC-2099", "apply_url": "https://cityjobs.nyc.gov/"}


def snapshot(jobs, fetched="2026-10-07T12:00:00+00:00"):
    return {"jobs": jobs, "fetched_at": fetched, "source_url": SOURCE_URL, "sources": [], "warnings": []}


class RefreshSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "jobs.json"
        save_snapshot(self.path, snapshot([posting("old")]))
        self.app = App(self.path, Settings(), Path(self.directory.name) / "state.sqlite3")
        self.addCleanup(self.app.auto_refresh_stop.set)

    def test_refresh_removes_closed_job_retains_history_and_alerts_new_matches(self):
        self.app.application({"job_key": "nyc:old", "action": "mark_submitted"})
        self.app.subscribe({"label": "Senior Python", "plan": {"keywords": ["Python"], "min_annual_salary": 0, "career_stage": "senior"}})
        fresh = snapshot([posting("new"), posting("grad", "Software Engineer, New Grad")], "2026-10-07T15:00:00+00:00")
        def refresh(path):
            save_snapshot(path, fresh)
            return fresh
        with patch("nyc_job_match.server.refresh_snapshot", side_effect=refresh):
            status = self.app.refresh()
        self.assertEqual(status["job_count"], 2)
        self.assertEqual(self.app.workflow.submitted_keys(), {"nyc:old"})
        with self.assertRaises(ValueError):
            self.app.selected_job("nyc:old")
        notifications = self.app.subscription_status()["notifications"]
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]["title"], "New match: Senior Software Engineer")
        result = self.app.search({"profile": "senior Python", "backend": "local", "use_mistral": False, "min_salary": 0})
        self.assertEqual([job["job_id"] for job in result["jobs"]], ["new"])
        self.assertEqual(status["auto_refresh_interval_seconds"], 10800)

    def test_scheduler_runs_when_due_without_model_calls(self):
        called = threading.Event()
        def refresh():
            self.app.auto_refresh_stop.set()
            called.set()
        self.app.next_refresh_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        with patch.object(self.app, "refresh", side_effect=refresh), patch("nyc_job_match.engine.MistralClient.structured") as model:
            self.app.start_scheduler()
            self.assertTrue(called.wait(2))
            model.assert_not_called()

    def test_busy_refresh_does_not_mutate_snapshot(self):
        before = self.path.read_bytes()
        self.app.ingest_lock.acquire()
        try:
            with self.assertRaises(ValueError):
                self.app.refresh()
        finally:
            self.app.ingest_lock.release()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(REFRESH_INTERVAL, 10800)


if __name__ == "__main__":
    unittest.main()
