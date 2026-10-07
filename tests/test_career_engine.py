import copy
import unittest
from unittest.mock import patch

from nyc_job_match.clients import ElasticClient, ServiceError, Settings
from nyc_job_match.data import SOURCE_URL
from nyc_job_match.engine import build_query, search


def posting(identifier, title, stage):
    return {
        "posting_id": identifier + "-a", "job_id": identifier, "application_key": "nyc:" + identifier,
        "business_title": title, "posting_type": "External", "agency": "Example",
        "salary_frequency": "Annual", "salary_range_from": 100_000.0, "salary_range_to": 120_000.0,
        "job_description": "Python SQL software development.", "preferred_skills": "Python SQL",
        "minimum_qual_requirements": "", "post_until": "31-DEC-2099",
        "career_stage": stage,
        "career_stage_evidence": [{"field": "business_title", "quote": title}],
    }


class CareerEngineTests(unittest.TestCase):
    def setUp(self):
        self.jobs = [
            posting("1", "Software Engineer, New Grad", "new_grad"),
            posting("2", "Software Engineer Intern", "internship"),
            posting("3", "Senior Software Engineer", "senior"),
            posting("4", "Software Engineer", "unspecified"),
            posting("5", "Junior Software Engineer", "entry_level"),
        ]
        self.snapshot = {"jobs": self.jobs, "fetched_at": "2026-10-07T12:00:00+00:00", "source_url": SOURCE_URL}

    def run_search(self, **changes):
        payload = {"profile": "Python SQL", "min_salary": 0, "backend": "local", "use_mistral": False, "max_results": 5, **changes}
        return search(self.snapshot, Settings(), payload)

    def test_explicit_new_grad_filter_excludes_intern_senior_and_unspecified(self):
        result = self.run_search(career_stage="new_grad")
        self.assertEqual([job["job_id"] for job in result["jobs"]], ["1"])
        self.assertEqual(result["plan"]["career_stage"], "new_grad")

    def test_profile_can_request_new_grad_but_resume_is_not_a_preference(self):
        requested = self.run_search(profile="Looking for new grad Python SQL roles")
        self.assertEqual([job["job_id"] for job in requested["jobs"]], ["1"])
        resume_only = self.run_search(resume_text="New grad seeking internship opportunities. Python SQL.")
        self.assertEqual(resume_only["plan"]["career_stage"], "any")
        self.assertEqual({job["job_id"] for job in resume_only["jobs"]}, {"1", "2", "3", "4", "5"})

    def test_explicit_internship_filter_does_not_include_new_grad(self):
        result = self.run_search(career_stage="internship")
        self.assertEqual([job["job_id"] for job in result["jobs"]], ["2"])

    def test_entry_level_includes_confirmed_junior_and_new_grad_only(self):
        result = self.run_search(career_stage="entry_level")
        self.assertEqual({job["job_id"] for job in result["jobs"]}, {"1", "5"})
        plan = {"keywords": ["Python"], "search_text": "Python", "min_annual_salary": 0, "career_stage": "entry_level"}
        filters = build_query(plan, "snapshot", ["1-a", "5-a"])["query"]["bool"]["filter"]
        self.assertIn({"terms": {"career_stage": ["entry_level", "new_grad"]}}, filters)

    def test_senior_preferences_filter_independently_of_resume(self):
        result = self.run_search(profile="Looking for senior Python software engineer roles", resume_text="Recent graduate")
        self.assertEqual(result["plan"]["career_stage"], "senior")
        self.assertEqual([job["job_id"] for job in result["jobs"]], ["3"])

    def test_stage_filter_is_enforced_by_elastic_term_and_posting_allowlist(self):
        settings = Settings(elastic_endpoint="https://example.es.elastic.cloud", elastic_api_key="test-key")
        response = {"hits": {"total": {"value": 1, "relation": "eq"}, "hits": [{"_source": copy.deepcopy(self.jobs[0])}]}}
        with patch("nyc_job_match.engine.ElasticClient.search", return_value=response) as retrieve:
            result = search(self.snapshot, settings, {"profile": "Python", "career_stage": "new_grad", "backend": "elasticsearch", "use_mistral": False})
        filters = retrieve.call_args.args[0]["query"]["bool"]["filter"]
        self.assertIn({"term": {"career_stage": "new_grad"}}, filters)
        self.assertIn({"terms": {"posting_id": ["1-a"]}}, filters)
        self.assertEqual([job["job_id"] for job in result["jobs"]], ["1"])

    def test_any_stage_query_has_no_career_term(self):
        plan = {"keywords": ["Python"], "search_text": "Python", "min_annual_salary": 0, "career_stage": "any"}
        filters = build_query(plan, "snapshot", ["1-a"])["query"]["bool"]["filter"]
        self.assertFalse(any("career_stage" in item.get("term", {}) for item in filters))


class CareerMappingTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(elastic_endpoint="https://example.es.elastic.cloud", elastic_api_key="test-key")
        self.client = ElasticClient(self.settings)
        self.job = posting("1", "Software Engineer, New Grad", "new_grad")

    def run_ingest(self, *, owned=True, has_stage=False):
        properties = {"salary_range_from": {"type": "double"}}
        if has_stage:
            properties["career_stage"] = {"type": "keyword"}
        mapping = {self.settings.index: {"mappings": {"_meta": {"application": "nyc-job-match" if owned else "other-project"}, "properties": properties}}}

        def call(path, *, method="GET", body=None, ndjson=False):
            if path.endswith("/_mapping") and method == "GET":
                return mapping
            if path.startswith("/_bulk"):
                return {"errors": False, "items": [{}]}
            return {}

        mocked = patch.object(self.client, "call", side_effect=call)
        return mocked

    def test_existing_managed_index_adds_stage_mapping_and_keeps_evidence_source(self):
        with self.run_ingest() as call:
            self.assertEqual(self.client.ingest([self.job], "snapshot")["indexed"], 1)
        updates = [item for item in call.call_args_list if item.kwargs.get("method") == "PUT"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].kwargs["body"], {"properties": {"career_stage": {"type": "keyword"}}})
        bulk = next(item for item in call.call_args_list if item.args[0].startswith("/_bulk"))
        self.assertIn(b'"career_stage": "new_grad"', bulk.kwargs["body"])
        self.assertIn(b'"career_stage_evidence"', bulk.kwargs["body"])

    def test_existing_keyword_mapping_is_not_modified(self):
        with self.run_ingest(has_stage=True) as call:
            self.client.ingest([self.job], "snapshot")
        self.assertFalse(any(item.kwargs.get("method") == "PUT" for item in call.call_args_list))

    def test_unowned_index_is_rejected_before_mapping_mutation(self):
        with self.run_ingest(owned=False) as call:
            with self.assertRaisesRegex(ServiceError, "not created by NYC Job Match"):
                self.client.ingest([self.job], "snapshot")
        self.assertFalse(any(item.kwargs.get("method") == "PUT" for item in call.call_args_list))
        self.assertFalse(any(item.args[0].startswith("/_bulk") for item in call.call_args_list))


if __name__ == "__main__":
    unittest.main()
