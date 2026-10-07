import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nyc_job_match.browser_assist import BrowserAssistant
from nyc_job_match.clients import Settings
from nyc_job_match.data import SOURCE_URL
from nyc_job_match.profile import ResumeStore
from nyc_job_match.server import App
from nyc_job_match.workflow import WorkflowStore


def job():
    return {"job_id":"123", "posting_id":"greenhouse:figma:123", "application_key":"greenhouse:figma:789",
            "provider":"greenhouse", "board":"figma", "business_title":"Software Engineer", "agency":"Figma",
            "apply_url":"https://job-boards.greenhouse.io/figma/jobs/123", "source_url":SOURCE_URL,
            "posting_type":"External", "salary_frequency":"Annual", "salary_range_from":100000,
            "salary_range_to":150000, "job_description":"Python software engineering", "post_until":""}


class BrowserWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path=Path(self.directory.name)/"state.sqlite3"
        self.resumes=ResumeStore(self.path)
        self.workflow=WorkflowStore(self.path)
        self.helper=BrowserAssistant(self.resumes,self.workflow.confirm)

    def upload(self):
        self.resumes.upload("resume.txt",base64.b64encode(b"Python software engineer").decode(),Settings())

    def test_missing_resume_or_identity_never_starts_browser(self):
        with patch("nyc_job_match.browser_assist.subprocess.Popen") as process:
            with self.assertRaises(ValueError):self.helper.start(job(),{"answers":{}})
            self.upload()
            with self.assertRaises(ValueError):self.helper.start(job(),{"answers":{"email":"test@example.com"}})
            process.assert_not_called()

    def test_all_question_categories_are_sent_but_same_job_does_not_open_twice(self):
        self.upload()
        draft={"answers":{"first_name":"Example","last_name":"Person","email":"test@example.com","location":"NYC","consent":"yes"},
               "questions":[{"label":"First Name","fields":[{"name":"first_name","type":"input_text"}]}],
               "location_questions":[{"label":"Location","fields":[{"name":"location","type":"input_text"}]}],
               "consent_questions":[{"label":"Consent","fields":[{"name":"consent","type":"input_text"}]}]}
        with patch("nyc_job_match.browser_assist.threading.Thread") as thread,patch("nyc_job_match.browser_assist.shutil.which",return_value="node"):
            first=self.helper.start(job(),draft)
            second=self.helper.start(job(),draft)
        self.assertEqual(first["run_id"],second["run_id"])
        self.assertEqual(thread.call_count,1)
        payload=thread.call_args.kwargs["args"][2]
        self.assertEqual({field["name"] for field in payload["fields"]},{"first_name","location","consent"})
        self.assertEqual(base64.b64decode(payload["resume"]["base64"]),b"Python software engineer")
        self.assertNotIn("resume",first)

    def test_official_receipt_persists_dedup_and_one_notification(self):
        url="https://job-boards.greenhouse.io/figma/jobs/123/confirmation"
        self.workflow.confirm(job(),url)
        self.workflow.confirm(job(),url)
        reopened=WorkflowStore(self.path)
        self.assertEqual(reopened.submitted_keys(),{job()["application_key"]})
        records=reopened.list()
        self.assertEqual(records["applications"][0]["status"],"submitted_confirmed")
        self.assertEqual(len(records["notifications"]),1)
        result=reopened.record(job(),"open")
        self.assertEqual(result["application"]["status"],"submitted_confirmed")

    def test_wrong_site_job_or_http_cannot_record_receipt(self):
        for url in ("https://example.com/figma/jobs/123","https://job-boards.greenhouse.io/figma/jobs/124", "http://job-boards.greenhouse.io/figma/jobs/123"):
            with self.subTest(url=url),self.assertRaises(ValueError):self.workflow.confirm(job(),url)
        self.assertEqual(self.workflow.submitted_keys(),set())

    def test_changed_resume_rejects_old_draft_before_browser_start(self):
        data=Path(self.directory.name)/"jobs.json"
        data.write_text(json.dumps({"jobs":[job()],"fetched_at":"test","source_url":SOURCE_URL}),encoding="utf8")
        app=App(data,Settings(),self.path)
        self.upload()
        draft={"resume_fingerprint":hashlib.sha256(b"old resume").hexdigest()}
        with patch.object(app.assistant,"get_draft",return_value=draft),patch.object(app.browser,"start") as start:
            with self.assertRaises(ValueError):app.fill_application({"job_key":job()["application_key"]})
            start.assert_not_called()
