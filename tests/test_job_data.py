import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from nyc_job_match.data import DataFetchError, fetch_snapshot, is_open, load_snapshot, normalize_jobs


class JobDataTests(unittest.TestCase):
    def posting(self, **changes):
        return {
            "job_id": "123", "posting_type": "External", "level": "01",
            "business_title": "Data Analyst", "title_code_no": "12345",
            "salary_range_from": "80000", "salary_range_to": "90000",
            "salary_frequency": "Annual", "posting_updated": "2026-09-01T00:00:00",
            **changes,
        }

    def test_retains_distinct_levels_salaries_and_title_codes(self):
        jobs = normalize_jobs([
            self.posting(), self.posting(level="02"),
            self.posting(salary_range_to="100000"), self.posting(title_code_no="54321"),
        ])
        self.assertEqual(len(jobs), 4)
        self.assertEqual(len({job["posting_id"] for job in jobs}), 4)

    def test_same_version_refresh_replaces_old_deadline_with_stable_id(self):
        old = self.posting(post_until="13-OCT-2026", posting_date="2026-09-01")
        new = self.posting(post_until="30-OCT-2026", posting_date="2026-09-02", posting_updated="2026-09-02")
        jobs = normalize_jobs([old, new])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["post_until"], "30-OCT-2026")
        self.assertEqual(jobs[0]["posting_id"], normalize_jobs([old])[0]["posting_id"])
        self.assertEqual(normalize_jobs([new, old]), jobs)

    def test_same_update_chooses_later_deadline(self):
        jobs = normalize_jobs([self.posting(post_until="13-OCT-2026"), self.posting(post_until="30-OCT-2026")])
        self.assertEqual(jobs[0]["post_until"], "30-OCT-2026")

    def test_external_filter_html_and_numeric_salary(self):
        jobs = normalize_jobs([
            self.posting(posting_type=" INTERNAL "),
            self.posting(posting_type=" external ", business_title="<b>Data&nbsp; Analyst</b>", salary_range_from="$80,000.50", salary_range_to="NaN"),
        ])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["posting_type"], "External")
        self.assertEqual(jobs[0]["business_title"], "Data Analyst")
        self.assertEqual(jobs[0]["salary_range_from"], 80000.5)
        self.assertIsNone(jobs[0]["salary_range_to"])
        for value in (None, "", "N/A", "Infinity", -1, True):
            with self.subTest(value=value):
                self.assertIsNone(normalize_jobs([self.posting(salary_range_from=value)])[0]["salary_range_from"])

    def test_deadline_formats_and_unknown(self):
        today = date(2026, 10, 7)
        for deadline in ("2026-10-06T00:00:00.000", "10/06/2026", "06-OCT-2026"):
            self.assertFalse(is_open({"post_until": deadline}, today))
        for deadline in ("07-OCT-2026", "10/08/2026", "Until Filled", "", None):
            self.assertTrue(is_open({"post_until": deadline}, today))

    def test_missing_snapshot_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = load_snapshot(Path(directory) / "absent.json")
        self.assertEqual(snapshot["jobs"], [])
        self.assertEqual(snapshot["raw_count"], 0)

    def test_complete_download_saves_counts_and_external_rows(self):
        rows = [self.posting(), self.posting(posting_type="Internal")]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "jobs.json"
            with patch("nyc_job_match.data._source_updated_at", return_value="2026-10-06T00:00:00+00:00"), patch("nyc_job_match.data._row_count", return_value=2), patch("nyc_job_match.data._read_json_url", return_value=rows):
                snapshot = fetch_snapshot(target)
            self.assertEqual(snapshot["raw_count"], 2)
            self.assertEqual(len(snapshot["jobs"]), 1)
            self.assertEqual(load_snapshot(target), snapshot)
            self.assertTrue(snapshot["fetched_at"].endswith("+00:00"))

    def test_count_failure_preserves_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "jobs.json"
            target.write_text(json.dumps({"jobs": [], "raw_count": 99}), encoding="utf-8")
            before = target.read_bytes()
            with patch("nyc_job_match.data._source_updated_at", return_value=None), patch("nyc_job_match.data._row_count", return_value=2), patch("nyc_job_match.data._read_json_url", return_value=[self.posting()]):
                with self.assertRaises(DataFetchError):
                    fetch_snapshot(target)
            self.assertEqual(target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
