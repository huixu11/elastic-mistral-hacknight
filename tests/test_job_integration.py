"""Isolated app/engine contract checks; no network calls or HTTP sockets."""

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch

from nyc_job_match.clients import ServiceError, Settings
from nyc_job_match.data import DataFetchError, SOURCE_URL
from nyc_job_match.engine import search
from nyc_job_match.server import App


def job(posting_id="123-a", job_id="123", **changes):
    return {
        "posting_id": posting_id,
        "job_id": job_id,
        "application_key": "nyc:" + job_id,
        "provider": "nyc",
        "posting_type": "External",
        "business_title": "Python Software Engineer",
        "agency": "NYC Example Agency",
        "salary_range_from": 90_000.0,
        "salary_range_to": 110_000.0,
        "salary_frequency": "Annual",
        "job_description": "Analyze data with Python and SQL.",
        "minimum_qual_requirements": "Two years of relevant experience.",
        "preferred_skills": "Python, SQL",
        "additional_information": "",
        "residency_requirement": "",
        "post_until": "31-DEC-2099",
        "source_url": SOURCE_URL,
        "apply_url": "https://cityjobs.nyc.gov/",
        **changes,
    }


def snapshot(jobs, stamp="2026-10-07T12:00:00+00:00", **changes):
    return {
        "jobs": jobs,
        "fetched_at": stamp,
        "source_updated_at": "2026-10-06T12:00:00+00:00",
        "source_url": SOURCE_URL,
        "raw_count": len(jobs),
        "warnings": [],
        **changes,
    }


def search_payload(**changes):
    return {
        "profile": "Python SQL",
        "min_salary": 0,
        "backend": "local",
        "use_mistral": False,
        "max_results": 5,
        **changes,
    }


class EngineIntegrationTests(unittest.TestCase):
    def test_resume_alone_can_drive_local_search_without_salary_inference(self):
        resume = "Python and SQL analyst. Previous annual salary: $250,000."
        result = search(snapshot([job()]), Settings(), search_payload(profile="", resume_text=resume))
        self.assertEqual(len(result["jobs"]), 1)
        self.assertEqual(result["plan"]["min_annual_salary"], 0)
        self.assertIn("python", result["plan"]["keywords"])

    def test_planner_and_explainer_receive_resume_with_salary_prompt_boundary(self):
        resume = "Python SQL. Previous annual salary: $250,000."
        planned = {"search_text": "Python SQL", "keywords": ["Python", "SQL"], "min_annual_salary": 0, "notes": []}
        explained = {"assessments": [{
            "posting_id": "123-a", "summary": "The posting mentions Python.",
            "matches": [{"field": "preferred_skills", "quote": "Python", "explanation": "The resume mentions Python."}],
            "checks": [],
        }]}
        with patch("nyc_job_match.engine.MistralClient") as mistral:
            mistral.return_value.structured.side_effect = [planned, explained]
            result = search(snapshot([job()]), Settings(mistral_api_key="test-key"), search_payload(profile="", resume_text=resume, use_mistral=True))
        calls = mistral.return_value.structured.call_args_list
        self.assertEqual(len(calls), 2)
        planner_messages, _, planner_name = calls[0].args
        explanation_messages, _, explanation_name = calls[1].args
        self.assertEqual(planner_name, "job_search_plan")
        self.assertEqual(explanation_name, "job_evidence_report")
        planner_input = json.loads(next(message["content"] for message in planner_messages if message["role"] == "user"))
        explanation_input = json.loads(explanation_messages[1]["content"])
        self.assertEqual(planner_input["resume"], resume)
        self.assertEqual(planner_input["preferences"], "")
        self.assertEqual(explanation_input["resume"], resume)
        self.assertEqual(explanation_input["profile"], "")
        self.assertIn("from preferences ONLY", planner_messages[0]["content"])
        self.assertIn("salary in a resume is past experience, not a requested salary", planner_messages[0]["content"])
        self.assertIn("UNTRUSTED DATA", planner_messages[0]["content"])
        self.assertIn("UNTRUSTED DATA", explanation_messages[0]["content"])
        self.assertIn("an English explanation", planner_messages[0]["content"])
        self.assertIn("in English", explanation_messages[0]["content"])
        self.assertNotIn("in Chinese", explanation_messages[0]["content"])
        self.assertEqual(result["plan"]["min_annual_salary"], 0)
        self.assertEqual(result["jobs"][0]["assessment"]["matches"][0]["quote"], "Python")

    def test_submitted_key_excludes_all_versions_but_not_other_provider(self):
        jobs = [
            job("123-a"), job("123-b", salary_range_from=95_000.0, level="02"),
            job("acme-123", application_key="greenhouse:acme:123", provider="greenhouse", agency="Acme"),
            job("456-a", "456"),
        ]
        result = search(snapshot(jobs), Settings(), search_payload(), {"nyc:123"})
        self.assertEqual({card["application_key"] for card in result["jobs"]}, {"greenhouse:acme:123", "nyc:456"})
        shown = search(snapshot(jobs), Settings(), search_payload(exclude_applied=False), {"nyc:123"})
        self.assertEqual({card["application_key"] for card in shown["jobs"]}, {"nyc:123", "greenhouse:acme:123", "nyc:456"})
        self.assertEqual(len(shown["jobs"]), 3)

    def test_unknown_salary_only_at_zero_floor_and_hourly_is_never_annualized(self):
        jobs = [
            job("annual", "1"),
            job("unknown", "2", salary_frequency="Unknown", salary_range_from=None, salary_range_to=None),
            job("hourly", "3", salary_frequency="Hourly", salary_range_from=1000.0, salary_range_to=2000.0),
        ]
        free = search(snapshot(jobs), Settings(), search_payload(min_salary=0))
        self.assertEqual({card["posting_id"] for card in free["jobs"]}, {"annual", "unknown"})
        filtered = search(snapshot(jobs), Settings(), search_payload(min_salary=80_000))
        self.assertEqual([card["posting_id"] for card in filtered["jobs"]], ["annual"])

    def test_local_output_has_one_card_per_application_key(self):
        jobs = [job("123-a"), job("123-b", level="02"), job("123-c", salary_range_to=120_000.0), job("456", "456")]
        result = search(snapshot(jobs), Settings(), search_payload())
        keys = [card["application_key"] for card in result["jobs"]]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(set(keys), {"nyc:123", "nyc:456"})


class AppIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data_path = Path(self.directory.name) / "jobs.json"
        self.workflow_path = Path(self.directory.name) / "workflow.sqlite3"
        self.jobs = [job("123-a"), job("123-b", level="02"), job("acme-123", application_key="greenhouse:acme:123", provider="greenhouse", agency="Acme")]
        self.write_snapshot(snapshot(self.jobs))
        self.app = App(self.data_path, Settings(), self.workflow_path)

    def write_snapshot(self, value):
        self.data_path.write_text(json.dumps(value), encoding="utf-8")

    def test_application_history_search_status_and_automatic_exclusion(self):
        first = self.app.application({"job_key": "nyc:123", "action": "open"})
        repeated = self.app.application({"job_key": "nyc:123", "action": "open"})
        self.assertEqual(first["application"], repeated["application"])
        opened = self.app.search(search_payload())
        self.assertEqual(next(card for card in opened["jobs"] if card["application_key"] == "nyc:123")["application_status"], "opened")
        submitted = self.app.application({"job_key": "nyc:123", "action": "mark_submitted"})
        self.app.application({"job_key": "nyc:123", "action": "mark_submitted"})
        reopened = self.app.application({"job_key": "nyc:123", "action": "open"})
        self.assertEqual(reopened["application"], submitted["application"])
        self.assertEqual(len(self.app.workflow.list()["notifications"]), 2)
        hidden = self.app.search(search_payload())
        self.assertEqual([card["application_key"] for card in hidden["jobs"]], ["greenhouse:acme:123"])
        visible = self.app.search(search_payload(exclude_applied=False))
        self.assertEqual(len(visible["jobs"]), 2)
        self.assertEqual(next(card for card in visible["jobs"] if card["application_key"] == "nyc:123")["application_status"], "submitted_self_reported")
        restored = App(self.data_path, Settings(), self.workflow_path)
        self.assertEqual(restored.workflow.submitted_keys(), {"nyc:123"})
        self.assertEqual(len(restored.workflow.list()["notifications"]), 2)

    def test_application_rejects_invalid_or_missing_snapshot_job(self):
        invalid = [
            {"job_key": None, "action": "open"},
            {"job_key": 123, "action": "open"},
            {"job_key": "", "action": "open"},
            {"job_key": "x" * 301, "action": "open"},
            {"job_key": "nyc:missing", "action": "open"},
            {"job_key": "nyc:123", "action": "submit_on_website"},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.app.application(payload)
        self.assertEqual(self.app.workflow.list(), {"applications": [], "notifications": []})

    def test_application_uses_snapshot_url_instead_of_client_url(self):
        result = self.app.application({"job_key": "nyc:123", "action": "open", "apply_url": "javascript:alert(1)"})
        self.assertEqual(result["apply_url"], "https://cityjobs.nyc.gov/")

    def test_refresh_reindexes_new_snapshot_and_releases_lock(self):
        self.app.settings = Settings(elastic_endpoint="https://example.es.elastic.cloud", elastic_api_key="test-key")
        fresh = snapshot([job("789-a", "789")], stamp="2026-10-07T13:00:00+00:00", warnings=["One connected board is unavailable."])

        def refresh(path):
            self.assertEqual(path, self.data_path)
            self.write_snapshot(fresh)
            return fresh

        with patch("nyc_job_match.server.refresh_snapshot", side_effect=refresh), patch("nyc_job_match.server.ElasticClient") as elastic:
            elastic.return_value.ingest.return_value = {"indexed": 1}
            result = self.app.refresh()
            from nyc_job_match.scope import software_snapshot
            elastic.return_value.ingest.assert_called_once_with(software_snapshot(fresh)["jobs"], fresh["fetched_at"])
        self.assertEqual(result["indexed"], 1)
        self.assertEqual(result["job_count"], 1)
        self.assertEqual(result["fetched_at"], fresh["fetched_at"])
        self.assertEqual(result["warnings"], fresh["warnings"])
        self.assertFalse(self.app.ingest_lock.locked())

    def test_refresh_index_failure_returns_warning_and_allows_retry(self):
        self.app.settings = Settings(elastic_endpoint="https://example.es.elastic.cloud", elastic_api_key="test-key")
        fresh = snapshot([job("789-a", "789")], stamp="2026-10-07T13:00:00+00:00")

        def refresh(path):
            self.write_snapshot(fresh)
            return fresh

        with patch("nyc_job_match.server.refresh_snapshot", side_effect=refresh), patch("nyc_job_match.server.ElasticClient") as elastic:
            elastic.return_value.ingest.side_effect = ServiceError("HTTP 403: Insufficient API key permissions")
            result = self.app.refresh()
            self.assertNotIn("indexed", result)
            self.assertTrue(any("Elasticsearch indexing failed" in warning for warning in result["warnings"]))
            self.assertFalse(self.app.ingest_lock.locked())
            self.assertEqual(self.app.snapshot()["fetched_at"], fresh["fetched_at"])
            elastic.return_value.ingest.side_effect = None
            elastic.return_value.ingest.return_value = {"indexed": 1}
            retry = self.app.refresh()
        self.assertEqual(retry["indexed"], 1)
        self.assertFalse(self.app.ingest_lock.locked())

    def test_refresh_download_failure_releases_lock_and_preserves_snapshot(self):
        before = self.data_path.read_bytes()
        with patch("nyc_job_match.server.refresh_snapshot", side_effect=DataFetchError("download failed")):
            with self.assertRaises(DataFetchError):
                self.app.refresh()
        self.assertFalse(self.app.ingest_lock.locked())
        self.assertEqual(self.data_path.read_bytes(), before)

    def test_search_busy_rejects_before_reading_snapshot_or_calling_engine(self):
        self.app.ingest_lock.acquire()
        try:
            with patch.object(self.app, "snapshot") as read_snapshot, patch("nyc_job_match.server.search") as engine_search:
                with self.assertRaises(ValueError):
                    self.app.search(search_payload())
                read_snapshot.assert_not_called()
                engine_search.assert_not_called()
            self.assertTrue(self.app.ingest_lock.locked())
        finally:
            self.app.ingest_lock.release()

    def test_successful_search_holds_lock_through_snapshot_engine_and_statuses(self):
        def read_snapshot():
            self.assertTrue(self.app.ingest_lock.locked())
            return snapshot(self.jobs)

        def engine_search(*arguments):
            self.assertTrue(self.app.ingest_lock.locked())
            return {"jobs": []}

        def read_statuses():
            self.assertTrue(self.app.ingest_lock.locked())
            return {"applications": [], "notifications": []}

        with patch.object(self.app, "snapshot", side_effect=read_snapshot), patch("nyc_job_match.server.search", side_effect=engine_search), patch.object(self.app.workflow, "list", side_effect=read_statuses):
            result = self.app.search(search_payload())
        self.assertEqual(result, {"jobs": []})
        self.assertFalse(self.app.ingest_lock.locked())

    def test_failed_search_always_releases_lock(self):
        for error in (ValueError("invalid search"), ServiceError("provider failed"), RuntimeError("unexpected engine failure")):
            with self.subTest(error=type(error).__name__):
                with patch("nyc_job_match.server.search", side_effect=error):
                    with self.assertRaises(type(error)):
                        self.app.search(search_payload())
                self.assertFalse(self.app.ingest_lock.locked())
        with patch.object(self.app, "snapshot", side_effect=DataFetchError("snapshot unreadable")):
            with self.assertRaises(DataFetchError):
                self.app.search(search_payload())
        self.assertFalse(self.app.ingest_lock.locked())

    def test_ingest_busy_does_not_read_snapshot(self):
        self.app.settings = Settings(elastic_endpoint="https://example.es.elastic.cloud", elastic_api_key="test-key")
        self.app.ingest_lock.acquire()
        try:
            with patch.object(self.app, "snapshot") as read_snapshot, patch("nyc_job_match.server.ElasticClient") as elastic:
                with self.assertRaises(ValueError):
                    self.app.ingest()
                read_snapshot.assert_not_called()
                elastic.assert_not_called()
            self.assertTrue(self.app.ingest_lock.locked())
        finally:
            self.app.ingest_lock.release()

    def test_ingest_reads_snapshot_only_after_acquiring_lock(self):
        self.app.settings = Settings(elastic_endpoint="https://example.es.elastic.cloud", elastic_api_key="test-key")
        current = snapshot(self.jobs)

        def read_snapshot():
            self.assertTrue(self.app.ingest_lock.locked())
            return current

        def ingest(jobs, snapshot_id):
            self.assertTrue(self.app.ingest_lock.locked())
            self.assertEqual(jobs, current["jobs"])
            self.assertEqual(snapshot_id, current["fetched_at"])
            return {"indexed": len(jobs)}

        with patch.object(self.app, "snapshot", side_effect=read_snapshot) as read, patch("nyc_job_match.server.ElasticClient") as elastic:
            elastic.return_value.ingest.side_effect = ingest
            result = self.app.ingest()
        read.assert_called_once_with()
        self.assertEqual(result["indexed"], len(current["jobs"]))
        self.assertFalse(self.app.ingest_lock.locked())

    def test_overlapping_search_blocks_ingest_before_any_snapshot_read(self):
        self.app.settings = Settings(elastic_endpoint="https://example.es.elastic.cloud", elastic_api_key="test-key")
        search_started, finish_search = Event(), Event()

        def slow_search(*arguments):
            search_started.set()
            if not finish_search.wait(timeout=5):
                raise AssertionError("test did not release the search")
            return {"jobs": []}

        with patch("nyc_job_match.server.search", side_effect=slow_search), patch("nyc_job_match.server.ElasticClient") as elastic:
            with ThreadPoolExecutor(max_workers=1) as executor:
                active_search = executor.submit(self.app.search, search_payload())
                try:
                    self.assertTrue(search_started.wait(timeout=5))
                    with patch.object(self.app, "snapshot") as read_snapshot:
                        with self.assertRaises(ValueError):
                            self.app.ingest()
                        read_snapshot.assert_not_called()
                    elastic.assert_not_called()
                finally:
                    finish_search.set()
                self.assertEqual(active_search.result(timeout=5), {"jobs": []})
        self.assertFalse(self.app.ingest_lock.locked())


if __name__ == "__main__":
    unittest.main()
