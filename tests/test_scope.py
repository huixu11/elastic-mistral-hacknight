import copy
import json
import tempfile
import unittest
from pathlib import Path

from nyc_job_match.clients import Settings
from nyc_job_match.data import SOURCE_URL
from nyc_job_match.scope import is_software_job, software_snapshot
from nyc_job_match.server import App


def job(job_id, title, **changes):
    return {
        "job_id": job_id, "posting_id": job_id + "-a", "application_key": "nyc:" + job_id,
        "business_title": title, "posting_type": "External", "agency": "Example",
        "salary_frequency": "Annual", "salary_range_from": 90_000.0, "salary_range_to": 100_000.0,
        "job_description": "Develop software with Python and SQL.",
        "preferred_skills": "Python SQL", "minimum_qual_requirements": "Relevant experience.",
        "post_until": "31-DEC-2099", **changes,
    }


class SoftwareScopeTests(unittest.TestCase):
    def test_includes_explicit_software_engineering_titles(self):
        titles = (
            "Software Engineer", "Senior Software Engineer, Python", "Software Developer",
            "Frontend Developer", "Front-End Engineer", "Backend Engineer", "Back End Developer",
            "Fullstack Engineer", "Full Stack Developer", "Full-Stack Software Engineer",
            "DevOps Engineer", "Site Reliability Engineer", "SRE",
            "Software Engineering Manager", "Mobile Developer", "iOS Engineer",
        )
        for title in titles:
            with self.subTest(title=title):
                self.assertTrue(is_software_job({"business_title": title}))

    def test_excludes_other_professions_even_with_software_in_description(self):
        titles = (
            "Data Analyst", "Sales Engineer", "Civil Engineer", "Designer", "Product Manager",
            "Software Sales Engineer", "Solutions Engineer", "Mechanical Engineer",
            "Software Product Manager", "Product Designer", "Engineering Manager",
        )
        for title in titles:
            with self.subTest(title=title):
                self.assertFalse(is_software_job({"business_title": title, "job_description": "Work with software engineers to build software in Python."}))

    def test_description_alone_and_missing_title_do_not_establish_scope(self):
        for title in (None, "", "Program Manager"):
            with self.subTest(title=title):
                self.assertFalse(is_software_job({"business_title": title, "job_description": "Software Engineer, Frontend Developer, Backend Engineer and SRE responsibilities."}))

    def mixed_snapshot(self):
        jobs = [
            job("1", "Software Engineer"),
            job("2", "Software Developer", provider="nyc"),
            job("3", "Data Analyst", provider="nyc"),
            job("4", "Backend Engineer", provider="greenhouse", board="datadog", application_key="greenhouse:datadog:4"),
            job("5", "Sales Engineer", provider="greenhouse", board="datadog", application_key="greenhouse:datadog:5"),
            job("6", "SRE", provider="greenhouse", board="figma", application_key="greenhouse:figma:6"),
            job("7", "Designer", provider="greenhouse", board="figma", application_key="greenhouse:figma:7"),
        ]
        return {
            "jobs": jobs, "fetched_at": "2026-10-07T12:00:00+00:00", "source_url": SOURCE_URL,
            "total_job_count": 7, "raw_count": 10,
            "sources": [
                {"provider": "nyc", "label": "NYC government", "job_count": 3},
                {"provider": "greenhouse", "board": "datadog", "label": "Datadog", "job_count": 2},
                {"provider": "greenhouse", "board": "figma", "label": "Figma", "job_count": 2, "total_job_count": 100},
            ],
        }

    def test_software_snapshot_recounts_sources_and_preserves_original_totals(self):
        original = self.mixed_snapshot()
        before = copy.deepcopy(original)
        scoped = software_snapshot(original)
        self.assertEqual({item["job_id"] for item in scoped["jobs"]}, {"1", "2", "4", "6"})
        self.assertEqual(scoped["scope"], "software_engineering")
        self.assertEqual([source["job_count"] for source in scoped["sources"]], [2, 1, 1])
        self.assertEqual([source["total_job_count"] for source in scoped["sources"]], [3, 2, 100])
        self.assertEqual(scoped["total_job_count"], 7)
        self.assertEqual(scoped["raw_count"], 10)
        self.assertEqual(original, before)
        self.assertIsNot(scoped, original)

    def test_reapplying_scope_does_not_replace_original_totals_with_filtered_counts(self):
        once = software_snapshot(self.mixed_snapshot())
        twice = software_snapshot(once)
        self.assertEqual(twice, once)

    def test_source_with_no_remaining_software_jobs_has_zero_count(self):
        raw = {"jobs": [job("1", "Data Analyst")], "sources": [{"provider": "nyc", "job_count": 1}]}
        scoped = software_snapshot(raw)
        self.assertEqual(scoped["jobs"], [])
        self.assertEqual(scoped["sources"][0]["job_count"], 0)
        self.assertEqual(scoped["sources"][0]["total_job_count"], 1)

    def test_app_snapshot_search_and_selection_share_software_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            data_path = Path(directory) / "jobs.json"
            data_path.write_text(json.dumps(self.mixed_snapshot()), encoding="utf-8")
            app = App(data_path, Settings(), Path(directory) / "state.sqlite3")
            self.assertEqual(len(app.snapshot()["jobs"]), 4)
            self.assertEqual(app.status()["job_count"], 4)
            self.assertEqual([source["job_count"] for source in app.status()["sources"]], [2, 1, 1])
            result = app.search({"profile": "Python SQL", "min_salary": 0, "backend": "local", "use_mistral": False, "max_results": 5})
            self.assertEqual({item["job_id"] for item in result["jobs"]}, {"1", "2", "4", "6"})
            with self.assertRaises(ValueError):
                app.application({"job_key": "nyc:3", "action": "open"})
            self.assertEqual(app.workflow.list()["applications"], [])


if __name__ == "__main__":
    unittest.main()
