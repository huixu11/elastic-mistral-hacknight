"""Requested roles must take precedence over candidate background."""

import copy
import unittest
from unittest.mock import patch

from nyc_job_match.career import requested_stage
from nyc_job_match.clients import Settings
from nyc_job_match.engine import search


def posting(identifier, title):
    return {
        "posting_id": identifier, "job_id": identifier,
        "application_key": "example:" + identifier,
        "business_title": title, "agency": "Example", "posting_type": "External",
        "salary_frequency": "Unknown", "salary_range_from": None,
        "job_description": "Develop software using Python.",
        "minimum_qual_requirements": "", "preferred_skills": "Python",
        "post_until": "31-DEC-2099",
    }


class PreferencePriorityTests(unittest.TestCase):
    def test_new_grad_spelling_and_unicode_variants(self):
        for profile in (
            "find newgrad software engineer roles",
            "find new grad software engineer roles",
            "find new\u00a0grad software engineer roles",
            "find new\u2011grad software engineer roles",
            "\u6211\u60f3\u627enew grad\u804c\u4f4d",
        ):
            with self.subTest(profile=profile):
                self.assertEqual(requested_stage({"career_stage": "auto"}, profile), "new_grad")

    def test_target_role_overrides_background_in_preferences(self):
        profile = "I am a senior software engineer, but I am looking for entry level roles."
        self.assertEqual(requested_stage({}, profile), "entry_level")

    def test_prompt_overrides_conflicting_dropdown(self):
        self.assertEqual(requested_stage({"career_stage": "senior"}, "Find newgrad roles"), "new_grad")
        self.assertEqual(requested_stage({"career_stage": "any"}, "Find new grad roles"), "new_grad")

    def test_dropdown_used_when_preferences_do_not_request_a_level(self):
        self.assertEqual(requested_stage({"career_stage": "senior"}, "Python backend roles"), "senior")
        self.assertEqual(requested_stage({"career_stage": "auto"}, "Python backend roles"), "any")

    def test_negated_stage_does_not_override_requested_stage(self):
        self.assertEqual(requested_stage({}, "Not new grad, looking for senior roles"), "senior")

    def test_senior_resume_does_not_override_new_grad_search(self):
        jobs = [posting("grad", "Software Engineer, New Grad"),
                posting("senior", "Senior Software Engineer"),
                posting("intern", "Software Engineering Intern"),
                posting("unknown", "Software Engineer")]
        snapshot = {"jobs": jobs, "fetched_at": "2026-10-07T00:00:00Z",
                    "source_url": "https://example.com/jobs"}
        payload = {"profile": "Find newgrad software engineer roles", "career_stage": "auto",
                   "resume_text": "Senior Software Engineer. Ten years of Python development.",
                   "backend": "local", "use_mistral": False, "max_results": 5}
        result = search(snapshot, Settings(), payload)
        self.assertEqual(result["plan"]["career_stage"], "new_grad")
        self.assertEqual([job["job_id"] for job in result["jobs"]], ["grad"])

        settings = Settings(elastic_endpoint="https://example.es.elastic.cloud", elastic_api_key="fixture")
        response = {"hits": {"total": {"value": 1, "relation": "eq"},
                             "hits": [{"_source": copy.deepcopy(jobs[0])}]}}
        with patch("nyc_job_match.engine.ElasticClient.search", return_value=response) as retrieve:
            elastic_result = search(snapshot, settings, {**payload, "backend": "elasticsearch"})
        filters = retrieve.call_args.args[0]["query"]["bool"]["filter"]
        self.assertIn({"term": {"career_stage": "new_grad"}}, filters)
        self.assertIn({"terms": {"posting_id": ["grad"]}}, filters)
        self.assertEqual([job["job_id"] for job in elastic_result["jobs"]], ["grad"])


if __name__ == "__main__":
    unittest.main()
