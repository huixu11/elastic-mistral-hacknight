import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from nyc_job_match.workflow import DEFAULT_APPLY_URL, WorkflowStore


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "workflow.db"
        self.store = WorkflowStore(self.path)
        self.job = {"job_id": "123", "business_title": "Data Analyst", "agency": "NYC Agency", "provider": "nyc"}

    def test_persistence_across_store_instances(self):
        result = self.store.record(self.job, "mark_submitted")
        restored = WorkflowStore(self.path)
        self.assertEqual(restored.list()["applications"], [result["application"]])
        self.assertEqual(restored.submitted_keys(), {"nyc:123"})
        self.assertEqual(len(restored.list()["notifications"]), 1)
        self.assertIn("You marked this job applied", result["message"])

    def test_versions_of_same_job_share_one_application(self):
        first = self.store.record({**self.job, "posting_id": "123-version-a", "level": "01"}, "open")
        second = self.store.record({**self.job, "posting_id": "123-version-b", "level": "02"}, "open")
        self.assertEqual(first["application"], second["application"])
        self.assertEqual(len(self.store.list()["applications"]), 1)
        self.assertEqual(len(self.store.list()["notifications"]), 1)

    def test_provider_keys_do_not_collide(self):
        self.store.record({**self.job, "application_key": "nyc:123"}, "mark_submitted")
        self.store.record({**self.job, "provider": "usajobs", "application_key": "usajobs:123"}, "mark_submitted")
        self.assertEqual(self.store.submitted_keys(), {"nyc:123", "usajobs:123"})
        self.assertEqual(len(self.store.list()["applications"]), 2)

    def test_repeated_submission_keeps_timestamp_and_notification(self):
        first = self.store.record(self.job, "mark_submitted")
        repeated = self.store.record(self.job, "mark_submitted")
        self.assertEqual(first["application"], repeated["application"])
        self.assertIn("already", repeated["message"])
        self.assertEqual(len(self.store.list()["notifications"]), 1)

    def test_open_after_self_report_never_regresses(self):
        submitted = self.store.record(self.job, "mark_submitted")
        opened = self.store.record(self.job, "open")
        self.assertEqual(opened["application"], submitted["application"])
        self.assertEqual(opened["application"]["status"], "submitted_self_reported")
        self.assertEqual(len(self.store.list()["notifications"]), 1)

    def test_one_notification_per_job_per_status(self):
        self.store.record(self.job, "open")
        submitted = self.store.record(self.job, "mark_submitted")
        self.store.record(self.job, "open")
        self.store.record(self.job, "mark_submitted")
        state = self.store.list()
        self.assertEqual(state["applications"], [submitted["application"]])
        self.assertEqual(len(state["notifications"]), 2)

    def test_concurrent_submission_is_idempotent(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: self.store.record(self.job, "mark_submitted"), range(24)))
        self.assertEqual(len({result["application"]["updated_at"] for result in results}), 1)
        self.assertEqual(len(self.store.list()["applications"]), 1)
        self.assertEqual(len(self.store.list()["notifications"]), 1)

    def test_concurrent_open_and_submit_cannot_regress(self):
        actions = ["open", "mark_submitted"] * 12
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda action: self.store.record(self.job, action), actions))
        state = self.store.list()
        self.assertEqual(state["applications"][0]["status"], "submitted_self_reported")
        self.assertIn(len(state["notifications"]), (1, 2))
        self.assertEqual(self.store.submitted_keys(), {"nyc:123"})

    def test_apply_url_validation_and_fallback(self):
        self.assertEqual(self.store.record(self.job, "open")["apply_url"], DEFAULT_APPLY_URL)
        safe_url = "https://example.gov/jobs/123?source=nyc#apply"
        self.assertEqual(self.store.record({**self.job, "apply_url": safe_url}, "open")["apply_url"], safe_url)
        for url in ("http://example.gov", "javascript:alert(1)", "https://user:password@example.gov", "https://example.gov:bad/", "https://", "https://example.gov\\other", "https://exa mple.gov", 123):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    self.store.record({**self.job, "apply_url": url}, "mark_submitted")
        self.assertEqual(self.store.submitted_keys(), set())

    def test_invalid_action_and_missing_id_do_not_write(self):
        with self.assertRaises(ValueError):
            self.store.record(self.job, "submit_on_website")
        with self.assertRaises(ValueError):
            self.store.record({}, "open")
        self.assertEqual(self.store.list(), {"applications": [], "notifications": []})

    def test_sql_parameters_and_minimal_metadata(self):
        job = {**self.job, "application_key": "nyc:123'; DROP TABLE applications;--", "resume": "private resume", "api_key": "secret key"}
        result = self.store.record(job, "mark_submitted")
        self.assertEqual(self.store.submitted_keys(), {job["application_key"]})
        self.assertNotIn("resume", result["application"])
        self.assertNotIn("api_key", result["application"])
        self.assertNotIn(b"private resume", self.path.read_bytes())
        self.assertNotIn(b"secret key", self.path.read_bytes())


if __name__ == "__main__":
    unittest.main()
