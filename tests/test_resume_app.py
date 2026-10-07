import base64
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from nyc_job_match.clients import Settings
from nyc_job_match.data import SOURCE_URL
from nyc_job_match.resume import ResumeError
from nyc_job_match.server import App


def encoded(content):
    return base64.b64encode(content).decode("ascii")


class SavedResumeAppTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data_path = Path(self.directory.name) / "jobs.json"
        self.state_path = Path(self.directory.name) / "state.sqlite3"
        self.job = {"job_id": "123", "posting_id": "123-a", "application_key": "nyc:123", "business_title": "Python Software Engineer", "agency": "Example", "posting_type": "External", "salary_frequency": "Annual", "salary_range_from": 90_000.0, "salary_range_to": 100_000.0, "job_description": "Python SQL analysis.", "preferred_skills": "Python SQL", "minimum_qual_requirements": "Two years of experience.", "post_until": "31-DEC-2099"}
        self.data_path.write_text(json.dumps({"jobs": [self.job], "fetched_at": "2026-10-07T12:00:00+00:00", "source_url": SOURCE_URL}), encoding="utf-8")
        self.app = App(self.data_path, Settings(), self.state_path)
        self.cache_invoke = Mock(return_value={"derived": "result"})

    def upload(self, content=b"Python SQL experience", filename="resume.txt"):
        return self.app.upload_resume({"filename": filename, "content_base64": encoded(content)})

    def cached_call(self):
        session = self.app.model_cache.session()
        value = session.structured(self.app.settings, [{"role": "user", "content": "test derived cache"}], {"type": "object"}, "test", self.cache_invoke)
        return value, session.stats()

    def cache_count(self):
        with closing(sqlite3.connect(self.state_path)) as connection:
            return connection.execute("SELECT count(*) FROM model_cache").fetchone()[0]

    def test_pdf_upload_route_reuses_ocr_for_same_file(self):
        self.app.settings = Settings(mistral_api_key="test-key")
        with patch("nyc_job_match.resume.request_json", return_value={"pages": [{"markdown": "Python SQL experience"}]}) as ocr:
            first = self.upload(b"%PDF-1.7\nresume", "resume.pdf")
            repeated = self.upload(b"%PDF-1.7\nresume", "renamed.pdf")
        ocr.assert_called_once()
        self.assertFalse(first["cached"])
        self.assertTrue(repeated["cached"])
        self.assertEqual(repeated["filename"], "renamed.pdf")

    def test_saved_resume_is_injected_by_default_without_mutating_payload(self):
        self.upload()
        payload = {"profile": "Data analyst"}
        with patch("nyc_job_match.server.search", return_value={"jobs": []}) as engine:
            result = self.app.search(payload)
        self.assertEqual(result, {"jobs": []})
        self.assertEqual(engine.call_args.args[2]["resume_text"], "Python SQL experience")
        self.assertNotIn("resume_text", payload)
        restored = App(self.data_path, Settings(), self.state_path)
        with patch("nyc_job_match.server.search", return_value={"jobs": []}) as engine:
            restored.search(payload)
        self.assertEqual(engine.call_args.args[2]["resume_text"], "Python SQL experience")

    def test_explicit_empty_resume_omits_saved_resume(self):
        self.upload()
        with patch("nyc_job_match.server.search", return_value={"jobs": []}) as engine:
            self.app.search({"profile": "Data analyst", "resume_text": ""})
        self.assertEqual(engine.call_args.args[2]["resume_text"], "")

    def test_saved_resume_alone_drives_real_local_search(self):
        self.upload()
        result = self.app.search({"profile": "", "backend": "local", "use_mistral": False, "min_salary": 0})
        self.assertEqual([job["job_id"] for job in result["jobs"]], ["123"])
        self.assertEqual(result["model_cache"], {"hits": 0, "misses": 0})
        self.assertFalse(result["cached_response"])

    def test_same_file_preserves_derived_cache_and_new_file_clears_it(self):
        self.upload()
        self.cached_call()
        self.assertTrue(self.upload()["cached"])
        self.assertEqual(self.cached_call()[1], {"hits": 1, "misses": 0})
        self.assertFalse(self.upload(b"Python SQL Java experience")["cached"])
        self.assertEqual(self.cache_count(), 0)
        self.assertEqual(self.cached_call()[1], {"hits": 0, "misses": 1})
        self.assertEqual(self.cache_invoke.call_count, 2)

    def test_edit_route_persists_text_and_clears_derived_cache(self):
        self.upload()
        self.cached_call()
        result = self.app.edit_resume({"text": "Python SQL with five years of experience"})
        self.assertTrue(result["edited"])
        self.assertEqual(self.cache_count(), 0)
        self.assertEqual(self.app.resumes.get()["resume"]["text"], result["text"])
        self.assertEqual(self.app.resumes.get_original(), ("resume.txt", b"Python SQL experience"))

    def test_failed_new_upload_does_not_clear_old_resume_or_derived_cache(self):
        self.upload()
        self.cached_call()
        original = self.app.resumes.get()
        with patch("nyc_job_match.profile.parse_resume", side_effect=ResumeError("parse failed")):
            with self.assertRaises(ResumeError):
                self.upload(b"%PDF-1.7\nnew resume", "new.pdf")
        self.assertEqual(self.app.resumes.get(), original)
        self.assertEqual(self.cached_call()[1], {"hits": 1, "misses": 0})
        self.cache_invoke.assert_called_once()

    def test_invalid_edit_leaves_resume_and_cache_unchanged(self):
        self.upload()
        self.cached_call()
        original = self.app.resumes.get()
        with self.assertRaises(ResumeError):
            self.app.edit_resume({"text": " "})
        self.assertEqual(self.app.resumes.get(), original)
        self.assertEqual(self.cache_count(), 1)

    def test_unchanged_edit_preserves_derived_cache(self):
        self.upload()
        self.cached_call()
        result = self.app.edit_resume({"text": "Python SQL experience"})
        self.assertFalse(result["edited"])
        self.assertEqual(self.cached_call()[1], {"hits": 1, "misses": 0})
        self.cache_invoke.assert_called_once()

    def test_clear_route_removes_resume_and_cache_without_workflow_loss(self):
        self.app.application({"job_key": "nyc:123", "action": "mark_submitted"})
        self.upload()
        self.cached_call()
        self.assertEqual(self.app.clear_resume(), {"resume": None})
        self.assertEqual(self.app.resumes.get(), {"resume": None})
        self.assertIsNone(self.app.resumes.get_original())
        self.assertEqual(self.cache_count(), 0)
        self.assertEqual(self.app.workflow.submitted_keys(), {"nyc:123"})
        self.assertEqual(len(self.app.workflow.list()["notifications"]), 1)

    def test_clear_waits_for_search_and_removes_its_late_cache_write(self):
        self.upload()
        search_started, finish_search, search_finished, clear_attempted = Event(), Event(), Event(), Event()
        original_clear = self.app.model_cache.clear

        def slow_search(*arguments):
            search_started.set()
            if not finish_search.wait(timeout=5):
                raise AssertionError("test did not finish search")
            self.cached_call()
            search_finished.set()
            return {"jobs": []}

        def clear_cache():
            self.assertTrue(search_finished.is_set(), "resume/cache clearing must wait for active search")
            return original_clear()

        def clear_resume():
            clear_attempted.set()
            return self.app.clear_resume()

        with patch("nyc_job_match.server.search", side_effect=slow_search), patch.object(self.app.model_cache, "clear", side_effect=clear_cache):
            with ThreadPoolExecutor(max_workers=2) as executor:
                searching = executor.submit(self.app.search, {"profile": "Python"})
                try:
                    self.assertTrue(search_started.wait(timeout=5))
                    acquired = self.app.resume_lock.acquire(blocking=False)
                    if acquired:
                        self.app.resume_lock.release()
                    self.assertFalse(acquired, "active search must hold the resume lock")
                    clearing = executor.submit(clear_resume)
                    self.assertTrue(clear_attempted.wait(timeout=5))
                finally:
                    finish_search.set()
                self.assertEqual(searching.result(timeout=5), {"jobs": []})
                self.assertEqual(clearing.result(timeout=5), {"resume": None})
        self.assertEqual(self.app.resumes.get(), {"resume": None})
        self.assertEqual(self.cache_count(), 0)


if __name__ == "__main__":
    unittest.main()
