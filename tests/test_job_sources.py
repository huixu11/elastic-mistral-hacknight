import unittest
from unittest.mock import patch

from nyc_job_match import job_sources as sources


def posting(identifier=100, location="New York, New York, USA", **changes):
    row = {"id": identifier, "internal_job_id": 500, "title": "Software Engineer",
           "location": {"name": location}, "content": "<p>Python and SQL</p>",
           "absolute_url": f"https://boards.greenhouse.io/example/jobs/{identifier}"}
    row.update(changes)
    return row


class PrivateJobSourcesTests(unittest.TestCase):
    def test_city_filter_does_not_include_state_only(self):
        for location in ("New York, NY", "New York, New York, USA", "NYC", "Manhattan", "San Francisco • New York, NY"):
            self.assertTrue(sources.is_nyc_location(location), location)
        for location in ("New York State", "New York, USA, Remote", "Albany, New York", "New York State, Remote", "Remote, United States"):
            self.assertFalse(sources.is_nyc_location(location), location)

    def test_structured_pay_converts_cents_and_requires_annual_usd(self):
        row = posting(pay_input_ranges=[{"currency_type": "USD", "title": "Annual Base Salary Range:", "min_cents": 12000000, "max_cents": 18000000}])
        result = sources.normalize_greenhouse_job(row, "example", "Example")
        self.assertEqual((120000.0, 180000.0, "Annual"), (result["salary_range_from"], result["salary_range_to"], result["salary_frequency"]))
        row["pay_input_ranges"][0]["title"] = "Hourly salary"
        self.assertEqual("Unknown", sources.normalize_greenhouse_job(row, "example", "Example")["salary_frequency"])
        row["pay_input_ranges"][0].update(title="Annual salary", currency_type="EUR")
        self.assertEqual("Unknown", sources.normalize_greenhouse_job(row, "example", "Example")["salary_frequency"])

    def test_datadog_metadata_requires_explicit_yearly_salary(self):
        row = posting(content="The reasonably estimated yearly salary for this role is:", metadata=[{"name": "Pay Transparency Range", "value_type": "currency_range", "value": {"unit": "USD", "min_value": "59000.0", "max_value": "79000.0"}}])
        self.assertEqual((59000.0, 79000.0, "Annual"), sources._annual_salary(row))
        row["content"] = "Competitive compensation"
        self.assertEqual((None, None, "Unknown"), sources._annual_salary(row))

    def test_invalid_or_inverted_salary_does_not_enter_floor_filter(self):
        for minimum, maximum in (("nan", 10000), (20000, 10000), (-1, 10000), (True, 10000)):
            row = posting(pay_input_ranges=[{"currency_type": "USD", "title": "Annual", "min_cents": minimum, "max_cents": maximum}])
            self.assertEqual("Unknown", sources._annual_salary(row)[2])

    def test_identity_and_quotes_preserve_published_evidence(self):
        row = posting(content="&lt;p&gt;Python &amp;amp; SQL&lt;/p&gt;&lt;script&gt;bad()&lt;/script&gt;")
        result = sources.normalize_greenhouse_job(row, "example", "Example")
        self.assertEqual("greenhouse:example:100", result["posting_id"])
        self.assertEqual("greenhouse:example:500", result["application_key"])
        self.assertEqual("Python & SQL", result["job_description"])
        self.assertEqual("", result["minimum_qual_requirements"])
        self.assertEqual("", result["preferred_skills"])
        self.assertEqual(row["absolute_url"], result["apply_url"])

    def test_multiple_ranges_use_conservative_minimum(self):
        row = posting(pay_input_ranges=[{"currency_type": "USD", "title": "Annual", "min_cents": 10000000, "max_cents": 16000000}, {"currency_type": "USD", "title": "Annual", "min_cents": 12000000, "max_cents": 18000000}])
        self.assertEqual((100000.0, 180000.0, "Annual"), sources._annual_salary(row))

    def test_fetch_retains_successful_source_when_other_board_fails(self):
        def read(url):
            if "/datadog/" in url:
                raise sources.JobSourceError("down")
            if "jobs?" in url:
                return {"jobs": [posting(), posting(101, "New York State")]}
            return posting(pay_input_ranges=[{"currency_type": "USD", "title": "Annual", "min_cents": 10000000, "max_cents": 15000000}])
        boards = tuple(source for source in sources.BOARDS if source["board"] in ("datadog", "figma"))
        with patch.object(sources, "BOARDS", boards), patch.object(sources, "_read_json", side_effect=read):
            result = sources.fetch_private_jobs()
        self.assertEqual(1, len(result["jobs"]))
        self.assertEqual("Figma", result["jobs"][0]["agency"])
        self.assertEqual(["failed", "ok"], [source["status"] for source in result["sources"]])
        self.assertTrue(result["warnings"])

    def test_detail_failure_keeps_unknown_pay_and_404_omits_removed_job(self):
        def read(url):
            if "/datadog/" in url:
                return {"jobs": []}
            if "jobs?" in url:
                return {"jobs": [posting(), posting(101)]}
            raise sources.JobSourceError("404" if "/101?" in url else "timeout", 404 if "/101?" in url else None)
        boards = tuple(source for source in sources.BOARDS if source["board"] in ("datadog", "figma"))
        with patch.object(sources, "BOARDS", boards), patch.object(sources, "_read_json", side_effect=read):
            result = sources.fetch_private_jobs()
        self.assertEqual(1, len(result["jobs"]))
        self.assertEqual("Unknown", result["jobs"][0]["salary_frequency"])
        self.assertTrue(result["warnings"])


if __name__ == "__main__":
    unittest.main()
