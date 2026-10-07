import json
import unittest
from datetime import date
from unittest.mock import patch

from nyc_job_match.clients import Settings, ServiceError
from nyc_job_match.data import normalize_jobs
from nyc_job_match.engine import attach_assessments, build_query, eligible_jobs, explain_jobs, search, validate_plan, valid_salary


def posting(**changes):
    raw = {"job_id": "100", "posting_type": "External", "salary_frequency": "Annual", "salary_range_from": "80000",
           "salary_range_to": "120000", "business_title": "Data Analyst", "agency": "TEST AGENCY", "preferred_skills": "Experience with Python and SQL.",
           "job_description": "Analyze city data.", "minimum_qual_requirements": "A degree and two years of experience.",
           "additional_information": "Must be permanent in the civil service title.", "post_until": "31-DEC-2099"}
    raw.update(changes)
    return normalize_jobs([raw])[0]


class SearchTests(unittest.TestCase):
    def test_salary_floor_is_starting_salary_not_range_ceiling(self):
        low_start = posting(salary_range_from="50000", salary_range_to="150000")
        high_start = posting(job_id="101", salary_range_from="90000")
        self.assertEqual(eligible_jobs([low_start, high_start], 80000), [high_start])

    def test_hourly_internal_and_expired_are_excluded(self):
        annual = posting()
        hourly = posting(job_id="102", salary_frequency="Hourly")
        expired = posting(job_id="103", post_until="06-OCT-2026")
        internal = {**annual, "posting_type": "Internal"}
        self.assertEqual(eligible_jobs([annual, hourly, expired, internal], 80000, date(2026, 10, 7)), [annual])

    def test_elastic_query_enforces_snapshot_and_numeric_floor(self):
        plan = {"keywords": ["Python", "SQL"], "search_text": "data analyst", "min_annual_salary": 80000}
        query = build_query(plan, "snapshot-1", ["posting-1"], 3)
        self.assertIn({"range": {"salary_range_from": {"gte": 80000}}}, query["query"]["bool"]["filter"])
        self.assertIn({"term": {"snapshot_id": "snapshot-1"}}, query["query"]["bool"]["filter"])
        self.assertTrue(query["track_total_hits"])

    def test_invented_evidence_is_removed(self):
        job = posting()
        rejected = attach_assessments([job], {"assessments": [{"posting_id": job["posting_id"], "summary": "Relevant role", "matches": [
            {"field": "preferred_skills", "quote": "Experience with Python and SQL.", "explanation": "Matches stated skills"},
            {"field": "job_description", "quote": "Fully remote role", "explanation": "Invented"}],
            "checks": [{"field": "additional_information", "quote": "Must be permanent in the civil service title.", "explanation": "Verify title eligibility"}]}]})
        self.assertEqual(rejected, 1)
        self.assertEqual(len(job["assessment"]["matches"]), 1)
        self.assertEqual(len(job["assessment"]["checks"]), 1)

    def test_ungrounded_summary_is_not_shown(self):
        job = posting()
        attach_assessments([job], {"assessments": [{"posting_id": job["posting_id"], "summary": "You are eligible", "matches": [], "checks": []}]})
        self.assertNotIn("assessment", job)

    def test_tail_qualification_is_not_cut_from_model_context(self):
        job = posting(minimum_qual_requirements="A" * 7000 + " Civil service exam required.")
        with patch("nyc_job_match.engine.MistralClient.structured", return_value={"assessments": []}) as call:
            explain_jobs("Python SQL", [job], Settings(mistral_api_key="test"))
        context = json.loads(call.call_args.args[0][1]["content"])
        self.assertTrue(context["postings"][0]["minimum_qual_requirements"].endswith("Civil service exam required."))

    def test_form_floor_cannot_be_lowered_by_model(self):
        plan = validate_plan({"search_text": "Python", "keywords": ["Python"], "min_annual_salary": 100, "notes": []}, 80000)
        self.assertEqual(plan["min_annual_salary"], 80000)

    def test_unstated_preferences_are_not_shown_as_model_notes(self):
        raw = {"search_text": "Python", "keywords": ["Python"], "min_annual_salary": 0, "notes": [
            {"preference_quote": "no remote work", "explanation": "Remote work is not wanted"},
            {"preference_quote": "", "explanation": "No visa sponsorship is needed"}, "no visa sponsorship required"]}
        self.assertEqual(validate_plan(raw, 0, "Python engineer")["notes"], [])

    def test_explicit_unfiltered_constraint_requires_preference_quote(self):
        raw = {"search_text": "Python", "keywords": ["Python"], "min_annual_salary": 0, "notes": [
            {"preference_quote": "fully remote", "explanation": "Remote work was not used as a hard filter; verify the official posting."}]}
        plan = validate_plan(raw, 0, "Python engineer, fully remote")
        self.assertEqual(plan["notes"], ["Remote work was not used as a hard filter; verify the official posting."])
        self.assertTrue(plan["notes_verified"])

    def test_reject_nonfinite_salary_and_boolean(self):
        for value in (float("nan"), float("inf"), -1, True, "not a number"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                valid_salary(value)

    def test_local_preview_is_explicit_and_not_fake_ai(self):
        job = posting()
        snapshot = {"jobs": [job], "fetched_at": "2026-10-07T00:00:00Z", "source_url": "https://data.cityofnewyork.us/"}
        result = search(snapshot, Settings(), {"profile": "Python SQL", "min_salary": 80000})
        self.assertEqual(result["backend"], "local")
        self.assertEqual(result["total"], 1)
        self.assertNotIn("assessment", result["jobs"][0])
        self.assertTrue(any("Mistral" in warning for warning in result["warnings"]))

    def test_elastic_failure_does_not_silently_fake_success(self):
        job = posting()
        snapshot = {"jobs": [job], "fetched_at": "snapshot", "source_url": "https://data.cityofnewyork.us/"}
        settings = Settings(elastic_endpoint="https://example.elastic.cloud", elastic_api_key="fake")
        with patch("nyc_job_match.engine.ElasticClient.search", side_effect=ServiceError("HTTP 401: key invalid")):
            with self.assertRaises(ServiceError):
                search(snapshot, settings, {"profile": "SQL", "min_salary": 80000, "use_mistral": False})


if __name__ == "__main__":
    unittest.main()
