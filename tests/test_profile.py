import base64
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch

from nyc_job_match.clients import Settings
from nyc_job_match.profile import ResumeStore
from nyc_job_match.resume import MAX_FILE_BYTES, ResumeError, parse_resume
from nyc_job_match.workflow import WorkflowStore


def encoded(content):
    return base64.b64encode(content).decode("ascii")


class ResumeStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "job_match.sqlite3"
        self.store = ResumeStore(self.path)
        self.settings = Settings()
        self.content = b"Python SQL experience"

    def upload(self, **changes):
        return self.store.upload(changes.get("filename", "resume.txt"), encoded(changes.get("content", self.content)), self.settings)

    def test_persists_across_instances_and_retains_original(self):
        uploaded = self.upload()
        self.assertFalse(uploaded["cached"])
        restored = ResumeStore(self.path)
        self.assertEqual(restored.get(), {"resume": {**uploaded, "cached": True}})
        self.assertEqual(restored.get_original(), ("resume.txt", self.content))

    def test_repeated_upload_parses_only_once_and_keeps_timestamp(self):
        with patch("nyc_job_match.profile.parse_resume", wraps=parse_resume) as parser:
            first = self.upload()
            second = self.upload()
        parser.assert_called_once()
        self.assertEqual(second, {**first, "cached": True})

    def test_pdf_reupload_uses_cached_ocr_even_without_current_api_key(self):
        content = encoded(b"%PDF-1.7\nmock resume")
        with patch("nyc_job_match.resume.request_json", return_value={"pages": [{"markdown": "Python SQL experience"}]}) as ocr:
            first = self.store.upload("resume.pdf", content, Settings(mistral_api_key="test-key"))
            cached = ResumeStore(self.path).upload("resume.pdf", content, Settings())
        ocr.assert_called_once()
        self.assertEqual(first["parser"], "mistral-ocr")
        self.assertEqual(cached, {**first, "cached": True})

    def test_same_bytes_renamed_without_reparsing_or_path_leak(self):
        with patch("nyc_job_match.profile.parse_resume", wraps=parse_resume) as parser:
            self.upload()
            renamed = self.upload(filename=r"C:\private\candidate\renamed.TXT")
        parser.assert_called_once()
        self.assertTrue(renamed["cached"])
        self.assertEqual(renamed["filename"], "renamed.TXT")
        self.assertEqual(self.store.get_original(), ("renamed.TXT", self.content))

    def test_changed_bytes_or_extension_requires_reparse(self):
        with patch("nyc_job_match.profile.parse_resume", wraps=parse_resume) as parser:
            self.upload()
            changed = self.upload(content=b"Python SQL Java")
            changed_extension = self.upload(filename="resume.md", content=b"Python SQL Java")
        self.assertEqual(parser.call_count, 3)
        self.assertFalse(changed["cached"])
        self.assertFalse(changed_extension["cached"])
        self.assertEqual(self.store.get_original(), ("resume.md", b"Python SQL Java"))

    def test_failed_new_upload_preserves_previous_resume_and_original(self):
        self.upload()
        before = self.store.get()
        with patch("nyc_job_match.profile.parse_resume", side_effect=ResumeError("OCR failed")):
            with self.assertRaises(ResumeError):
                self.upload(filename="new.pdf", content=b"%PDF-1.7\nnew resume")
        self.assertEqual(self.store.get(), before)
        self.assertEqual(self.store.get_original(), ("resume.txt", self.content))

    def test_file_validation_rejects_before_parser_and_preserves_previous(self):
        self.upload()
        before = self.store.get()
        invalid = [
            ("resume.txt", "not base64!"),
            ("resume.txt", "YWJj\n"),
            ("resume.exe", encoded(b"abc")),
            ("resume.txt", encoded(b"x" * (MAX_FILE_BYTES + 1))),
            ("resume.txt", ""),
        ]
        with patch("nyc_job_match.profile.parse_resume") as parser:
            for filename, content in invalid:
                with self.subTest(filename=filename, length=len(content)):
                    with self.assertRaises(ResumeError):
                        self.store.upload(filename, content, self.settings)
            parser.assert_not_called()
        self.assertEqual(self.store.get(), before)

    def test_edited_text_persists_and_retains_original_upload(self):
        self.upload()
        edited = self.store.update_text("Python SQL with five years of experience")
        self.assertTrue(edited["edited"])
        self.assertEqual(self.store.get_original(), ("resume.txt", self.content))
        restored = ResumeStore(self.path)
        self.assertEqual(restored.get()["resume"]["text"], edited["text"])
        with patch("nyc_job_match.profile.parse_resume") as parser:
            cached = self.upload()
        parser.assert_not_called()
        self.assertEqual(cached["text"], edited["text"])
        self.assertTrue(cached["edited"])
        reverted = self.store.update_text(self.content.decode("utf-8"))
        self.assertFalse(reverted["edited"])

    def test_edit_validation_and_no_resume_error(self):
        with self.assertRaises(ResumeError):
            self.store.update_text("Python")
        self.upload()
        before = self.store.get()
        for text in (None, 123, " \n\t", "x" * 20_001):
            with self.subTest(kind=type(text).__name__):
                with self.assertRaises(ResumeError):
                    self.store.update_text(text)
        self.assertEqual(self.store.get(), before)

    def test_unchanged_edit_is_idempotent(self):
        original = self.upload()
        repeated = self.store.update_text(original["text"])
        self.assertEqual(repeated, {**original, "cached": True})

    def test_public_responses_never_include_original_bytes_or_hash(self):
        uploaded = self.upload()
        expected_fields = {"filename", "text", "parser", "updated_at", "cached", "edited"}
        self.assertEqual(set(uploaded), expected_fields)
        self.assertEqual(set(self.store.get()["resume"]), expected_fields)
        self.assertEqual(set(self.store.update_text("Updated experience")), expected_fields)

    def test_clear_removes_text_and_original_and_does_not_clear_workflow(self):
        history = WorkflowStore(self.path)
        history.record({"job_id": "123", "business_title": "Data Analyst"}, "mark_submitted")
        self.upload()
        self.assertEqual(self.store.clear(), {"resume": None})
        self.assertEqual(self.store.get(), {"resume": None})
        self.assertIsNone(self.store.get_original())
        self.assertEqual(ResumeStore(self.path).get(), {"resume": None})
        self.assertEqual(history.submitted_keys(), {"nyc:123"})
        self.assertNotIn(self.content, self.path.read_bytes())
        self.assertEqual(self.store.clear(), {"resume": None})

    def test_concurrent_duplicate_uploads_across_instances_parse_once(self):
        second_store = ResumeStore(self.path)
        started, finish_parse = Event(), Event()

        def slow_parse(filename, content_base64, settings):
            started.set()
            if not finish_parse.wait(timeout=5):
                raise AssertionError("test did not finish the parser")
            return parse_resume(filename, content_base64, settings)

        with patch("nyc_job_match.profile.parse_resume", side_effect=slow_parse) as parser:
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = [executor.submit(self.store.upload, "resume.txt", encoded(self.content), self.settings)]
                try:
                    self.assertTrue(started.wait(timeout=5))
                    for index in range(15):
                        store = self.store if index % 2 else second_store
                        futures.append(executor.submit(store.upload, "resume.txt", encoded(self.content), self.settings))
                finally:
                    finish_parse.set()
                results = [future.result(timeout=5) for future in futures]
        parser.assert_called_once()
        self.assertEqual(sum(not result["cached"] for result in results), 1)
        self.assertEqual({result["updated_at"] for result in results}, {results[0]["updated_at"]})

    def test_slow_resume_parse_does_not_lock_workflow_database(self):
        history = WorkflowStore(self.path)

        def parse_and_write_history(filename, content_base64, settings):
            history.record({"job_id": "456"}, "open")
            return parse_resume(filename, content_base64, settings)

        with patch("nyc_job_match.profile.parse_resume", side_effect=parse_and_write_history):
            self.upload()
        self.assertEqual(history.list()["applications"][0]["job_key"], "nyc:456")


if __name__ == "__main__":
    unittest.main()
