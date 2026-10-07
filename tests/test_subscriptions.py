import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from nyc_job_match.subscriptions import SubscriptionStore


def job(key, **changes):
    return {"job_id": key, "posting_id": key + ":v1", "application_key": "greenhouse:figma:" + key,
            "business_title": "Software Engineer", "agency": "Figma", "posting_type": "External",
            "salary_frequency": "Annual", "salary_range_from": 120000,
            "job_description": "Python and SQL", "apply_url": "https://job-boards.greenhouse.io/figma/jobs/123",
            **changes}


def test_baseline_and_canonical_new_job_once_after_restart(store):
    baseline = job("1")
    saved = store.save({"keywords": ["python"], "min_annual_salary": 100000}, "Python roles", [baseline], [])
    assert saved["enabled"] and saved["career_stage"] == "any"
    assert store.list()["notifications"] == []
    new = job("2")
    result = store.check([baseline, new, job("2", posting_id="2:v2")], [])
    assert result["new_notifications"] == 1
    assert result["notifications"][0]["url"] == new["apply_url"]
    restored = SubscriptionStore(store.path)
    assert restored.check([new])["new_notifications"] == 0
    assert len(restored.list()["notifications"]) == 1
    # A missing snapshot row is neither deleted nor reported closed.
    assert restored.check([])["new_notifications"] == 0
    assert restored.check([new])["new_notifications"] == 0


def test_salary_open_external_and_applied_filters(store):
    store.save({"keywords": [], "min_annual_salary": 0}, "All open roles", [], [])
    rows = [job("unknown", salary_frequency="Unknown", salary_range_from=None),
            job("hourly", salary_frequency="Hourly"), job("internal", posting_type="Internal"),
            job("closed", post_until="2000-01-01T00:00:00"), job("applied")]
    result = store.check(rows, {"greenhouse:figma:applied"})
    assert result["new_notifications"] == 1
    high_floor = SubscriptionStore(store.path.parent / "high.sqlite3")
    high_floor.save({"keywords": [], "min_annual_salary": 130000}, "High pay", [], [])
    assert high_floor.check(rows)["new_notifications"] == 0


def test_stage_title_keywords_and_word_boundaries(store):
    store.save({"keywords": ["SQL"], "title_keywords": ["Engineer"], "career_stage": "entry_level"}, "Entry SQL", [], [])
    result = store.check([job("grad", business_title="Software Engineer, New Grad"),
                          job("junior", business_title="Junior Software Engineer"),
                          job("senior", business_title="Senior Software Engineer"),
                          job("nosql", business_title="Junior Software Engineer", job_description="NoSQL"),
                          job("analyst", business_title="Junior Data Analyst")])
    assert result["new_notifications"] == 2


def test_plan_does_not_persist_resume_settings_or_model_notes(store):
    saved = store.save({"keywords": ["Python", "python"], "search_text": "RESUME SECRET", "notes": ["PRIVATE"],
                        "resume_text": "RESUME SECRET", "api_key": "SECRET", "career_stage": "any"}, "Python", [], [])
    assert saved["plan"] == {"keywords": ["Python"], "title_keywords": [], "min_annual_salary": 0.0, "career_stage": "any"}
    with closing(sqlite3.connect(store.path)) as connection, connection:
        stored = connection.execute("SELECT plan FROM job_subscriptions").fetchone()[0]
    assert "SECRET" not in stored and "PRIVATE" not in stored
    assert json.loads(stored) == saved["plan"]


def test_invalid_plans_rejected(store):
    for plan in [None, {"keywords": "python"}, {"keywords": [1]}, {"keywords": ["x" * 81]},
                 {"keywords": ["a"] * 13}, {"keywords": [""]}, {"title_keywords": {}},
                 {"min_annual_salary": True}, {"min_annual_salary": -1},
                 {"min_annual_salary": float("nan")}, {"min_annual_salary": 10000001},
                 {"career_stage": "auto"}]:
        try:
            store.save(plan, "Label", [], [])
        except ValueError:
            continue
        raise AssertionError("Invalid plan was accepted: " + repr(plan))


def test_invalid_labels_rejected(store):
    for label in [None, "", " ", "x" * 121]:
        try:
            store.save({}, label, [], [])
        except ValueError:
            continue
        raise AssertionError("Invalid label was accepted: " + repr(label))


def test_removed_and_disabled_subscriptions_do_not_notify(store):
    saved = store.save({}, "Disabled", [], [])
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("UPDATE job_subscriptions SET enabled=0 WHERE id=?", (saved["id"],))
    assert not store.list()["subscriptions"][0]["enabled"]
    assert store.check([job("new")])["new_notifications"] == 0
    assert store.remove(saved["id"]) == {"removed": True}
    assert store.remove(saved["id"]) == {"removed": False}
    assert store.list() == {"subscriptions": [], "notifications": []}


def test_concurrent_instances_notify_once(store):
    store.save({}, "Software", [], [])
    def check(_):
        return SubscriptionStore(store.path).check([job("1")])["new_notifications"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(check, range(8))) == 1
    assert len(store.list()["notifications"]) == 1


def test_notifications_are_independent_per_subscription_and_safe_urls(store):
    store.save({}, "One", [], [])
    store.save({}, "Two", [], [])
    result = store.check([job("1", apply_url="javascript:alert(1)")])
    assert result["new_notifications"] == 2
    assert all(row["url"] == "https://cityjobs.nyc.gov/" for row in result["notifications"])


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SubscriptionStore(Path(self.tmp.name) / "jobs.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()


def _wrap(test):
    def run(self):
        test(self.store)
    return run


for _name, _test in list(globals().items()):
    if _name.startswith("test_") and callable(_test):
        setattr(SubscriptionTests, _name, _wrap(_test))


if __name__ == "__main__":
    unittest.main()
