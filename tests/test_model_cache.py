import copy
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from nyc_job_match.cache import ModelCache
from nyc_job_match.clients import ServiceError, Settings
from nyc_job_match.data import SOURCE_URL
from nyc_job_match.engine import search


class ModelCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "cache.sqlite3"
        self.cache = ModelCache(self.path)
        self.settings = Settings(mistral_api_key="test-key", mistral_model="test-model")
        self.messages = [{"role": "system", "content": "Test role"}, {"role": "user", "content": "private-resume-marker-123"}]
        self.schema = {"type": "object", "properties": {"value": {"type": "string"}}}

    def call(self, invoke, *, store=None, settings=None, messages=None, schema=None, name="test_response"):
        session = (store or self.cache).session()
        value = session.structured(settings or self.settings, messages or self.messages, schema or self.schema, name, invoke)
        return value, session

    def row_count(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute("SELECT count(*) FROM model_cache").fetchone()[0]

    def test_identical_input_invokes_once_and_reports_per_session_stats(self):
        invoke = Mock(return_value={"value": "result"})
        first, first_session = self.call(invoke)
        second, second_session = self.call(invoke)
        invoke.assert_called_once_with()
        self.assertEqual(first, second)
        self.assertEqual(first_session.stats(), {"hits": 0, "misses": 1})
        self.assertEqual(second_session.stats(), {"hits": 1, "misses": 0})
        self.assertIsNone(first_session.cached_at)
        self.assertIsNotNone(second_session.cached_at)

    def test_cache_persists_across_instances_without_storing_raw_request(self):
        invoke = Mock(return_value={"value": "derived response"})
        self.call(invoke)
        result, session = self.call(invoke, store=ModelCache(self.path))
        invoke.assert_called_once()
        self.assertEqual(result["value"], "derived response")
        self.assertEqual(session.stats(), {"hits": 1, "misses": 0})
        self.assertNotIn(b"private-resume-marker-123", self.path.read_bytes())
        self.assertNotIn(b"test-key", self.path.read_bytes())

    def test_model_schema_messages_and_response_name_change_cache_key(self):
        invoke = Mock(return_value={"value": "derived"})
        self.call(invoke)
        variants = [
            {"settings": replace(self.settings, mistral_model="different-model")},
            {"schema": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}},
            {"messages": [*self.messages[:-1], {"role": "user", "content": "changed condition"}]},
            {"name": "different_response"},
        ]
        for arguments in variants:
            with self.subTest(arguments=arguments):
                _, session = self.call(invoke, **arguments)
                self.assertEqual(session.stats(), {"hits": 0, "misses": 1})
        self.assertEqual(invoke.call_count, 5)

    def test_expired_entry_invokes_again_without_sleeping(self):
        invoke = Mock(side_effect=[{"value": "first"}, {"value": "new"}])
        self.call(invoke)
        expired = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE model_cache SET created_at = ?", (expired,))
        value, session = self.call(invoke)
        self.assertEqual(value, {"value": "new"})
        self.assertEqual(session.stats(), {"hits": 0, "misses": 1})
        self.assertEqual(invoke.call_count, 2)

    def test_failed_invocation_is_not_cached_and_retry_can_succeed(self):
        invoke = Mock(side_effect=[ServiceError("provider failed"), {"value": "success"}])
        with self.assertRaises(ServiceError):
            self.call(invoke)
        self.assertEqual(self.row_count(), 0)
        value, _ = self.call(invoke)
        self.assertEqual(value, {"value": "success"})
        self.call(invoke)
        self.assertEqual(invoke.call_count, 2)

    def test_non_dictionary_responses_are_never_cached(self):
        invoke = Mock(side_effect=[None, [], "invalid", {"value": "valid"}])
        for kind in ("null", "list", "string"):
            with self.subTest(kind=kind):
                with self.assertRaises(ServiceError):
                    self.call(invoke)
                self.assertEqual(self.row_count(), 0)
        self.assertEqual(self.call(invoke)[0], {"value": "valid"})
        self.assertEqual(invoke.call_count, 4)

    def test_clear_removes_cached_response(self):
        invoke = Mock(return_value={"value": "response"})
        self.call(invoke)
        self.cache.clear()
        self.assertEqual(self.row_count(), 0)
        self.call(invoke)
        self.assertEqual(invoke.call_count, 2)


def posting(posting_id, job_id, **changes):
    return {
        "posting_id": posting_id, "job_id": job_id, "application_key": "nyc:" + job_id,
        "posting_type": "External", "provider": "nyc", "business_title": "Python Analyst",
        "agency": "Example Agency", "salary_frequency": "Annual",
        "salary_range_from": 100_000.0, "salary_range_to": 120_000.0,
        "post_until": "31-DEC-2099", "job_description": "Python and SQL analysis.",
        "preferred_skills": "Python, SQL", "minimum_qual_requirements": "Two years of experience.",
        "additional_information": "", "residency_requirement": "", **changes,
    }


class CachedEngineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = ModelCache(Path(self.directory.name) / "cache.sqlite3")
        self.settings = Settings(elastic_endpoint="https://example.es.elastic.cloud", elastic_api_key="test-elastic-key", mistral_api_key="test-model-key")
        self.jobs = [posting("123-a", "123"), posting("456-a", "456")]
        self.payload = {"profile": "Data analyst", "resume_text": "Python SQL experience", "min_salary": 0, "backend": "elasticsearch", "use_mistral": True, "max_results": 3}
        self.elastic_patch = patch("nyc_job_match.engine.ElasticClient")
        self.model_patch = patch("nyc_job_match.engine.MistralClient")
        self.elastic = self.elastic_patch.start().return_value.search
        self.model = self.model_patch.start().return_value.structured
        self.addCleanup(self.elastic_patch.stop)
        self.addCleanup(self.model_patch.stop)
        self.elastic.side_effect = self.retrieve
        self.model.side_effect = self.answer

    def retrieve(self, query):
        ids = next(item["terms"]["posting_id"] for item in query["query"]["bool"]["filter"] if "posting_id" in item.get("terms", {}))
        jobs = [copy.deepcopy(job) for job in self.jobs if job["posting_id"] in ids]
        return {"hits": {"total": {"value": len(jobs), "relation": "eq"}, "hits": [{"_source": job} for job in jobs]}, "aggregations": {}}

    def answer(self, messages, schema, name):
        if name == "job_search_plan":
            return {"search_text": "Python SQL", "keywords": ["Python", "SQL"], "min_annual_salary": 0, "notes": []}
        evidence = json.loads(messages[1]["content"])["postings"]
        return {"assessments": [{"posting_id": item["posting_id"], "summary": "The posting mentions Python.", "matches": [{"field": "preferred_skills", "quote": "Python", "explanation": "The resume mentions Python."}], "checks": []} for item in evidence]}

    def run_search(self, *, excluded=None, **changes):
        value = {"jobs": self.jobs, "fetched_at": "2026-10-07T12:00:00+00:00", "source_url": SOURCE_URL}
        return search(value, self.settings, {**self.payload, **changes}, excluded, self.cache.session())

    def call_counts(self):
        names = [call.args[2] for call in self.model.call_args_list]
        return names.count("job_search_plan"), names.count("job_evidence_report")

    def test_repeated_search_reuses_model_work_but_always_retrieves_elastic(self):
        first = self.run_search()
        second = self.run_search()
        self.assertEqual(self.elastic.call_count, 2)
        self.assertEqual(self.call_counts(), (1, 1))
        self.assertEqual(first["model_cache"], {"hits": 0, "misses": 2})
        self.assertFalse(first["cached_response"])
        self.assertIsNone(first["cached_at"])
        self.assertEqual(second["model_cache"], {"hits": 2, "misses": 0})
        self.assertTrue(second["cached_response"])
        self.assertIsNotNone(second["cached_at"])

    def test_salary_change_replans_but_same_jobs_can_reuse_explanations(self):
        self.run_search()
        changed = self.run_search(min_salary=80_000)
        repeated = self.run_search(min_salary=80_000)
        self.assertEqual(self.elastic.call_count, 3)
        self.assertEqual(self.call_counts(), (2, 1))
        self.assertEqual(changed["plan"]["min_annual_salary"], 80_000)
        self.assertEqual(changed["model_cache"], {"hits": 1, "misses": 1})
        self.assertFalse(changed["cached_response"])
        self.assertEqual(repeated["model_cache"], {"hits": 2, "misses": 0})

    def test_submitted_job_changes_evidence_payload_and_misses_explanation(self):
        self.run_search()
        changed = self.run_search(excluded={"nyc:123"})
        self.assertEqual([job["application_key"] for job in changed["jobs"]], ["nyc:456"])
        self.assertEqual(self.elastic.call_count, 2)
        self.assertEqual(self.call_counts(), (1, 2))
        self.assertEqual(changed["model_cache"], {"hits": 1, "misses": 1})

    def test_updated_job_text_invalidates_explanation_without_replanning(self):
        self.run_search()
        self.jobs[0]["minimum_qual_requirements"] = "Three years of Python experience, updated requirement."
        changed = self.run_search()
        self.assertEqual(self.elastic.call_count, 2)
        self.assertEqual(self.call_counts(), (1, 2))
        self.assertEqual(changed["model_cache"], {"hits": 1, "misses": 1})

    def test_failed_uncached_explanation_is_not_reported_as_fully_cached(self):
        self.run_search()
        self.model.side_effect = ServiceError("explanation request failed")
        changed = self.run_search(excluded={"nyc:123"})
        self.assertEqual(changed["model_cache"], {"hits": 1, "misses": 1})
        self.assertFalse(changed["cached_response"])
        self.assertTrue(any("Mistral explanations are unavailable" in warning for warning in changed["warnings"]))
        self.assertNotIn("assessment", changed["jobs"][0])


if __name__ == "__main__":
    unittest.main()
