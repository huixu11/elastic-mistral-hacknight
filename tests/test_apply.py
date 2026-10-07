import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nyc_job_match.apply import ApplicationAssistant
from nyc_job_match.cache import ModelCache
from nyc_job_match.clients import ServiceError, Settings


def question(label, name, kind="input_text", required=True, values=None):
    return {"label": label, "required": required,
            "fields": [{"name": name, "type": kind, "values": values or []}]}


YES_NO = [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]
JOB = {"provider": "greenhouse", "board": "figma", "job_id": "123",
       "posting_id": "greenhouse:figma:123", "application_key": "greenhouse:figma:999",
       "apply_url": "https://boards.greenhouse.io/figma/jobs/123",
       "business_title": "Software Engineer"}
SOURCE = {"id": 123, "title": "Software Engineer", "absolute_url": JOB["apply_url"],
          "questions": [question("First Name", "first_name"),
                        question("Last Name", "last_name"), question("Email", "email"),
                        question("Why do you want to join Figma?", "question_10", "textarea"),
                        question("Are you authorized to work in the United States?", "question_11", "multi_value_single_select", values=YES_NO),
                        question("I certify the information is true.", "question_12", "multi_value_single_select", values=YES_NO),
                        {"label": "Resume", "required": True, "fields": [
                            {"name": "resume", "type": "input_file", "values": []},
                            {"name": "resume_text", "type": "textarea", "values": []}]}],
          "location_questions": [], "data_compliance": []}
PROFILE = {"first_name": "Ada", "last_name": "User", "email": "ada@example.com"}


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "workflow.sqlite3"
        self.assistant = ApplicationAssistant(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def prepare(self, source=SOURCE, profile=PROFILE, **kwargs):
        with patch("nyc_job_match.apply.request_json", return_value=copy.deepcopy(source)) as request:
            result = self.assistant.prepare(JOB, profile, "Built reliable Python and SQL tools.",
                                            Settings(), use_mistral=False, **kwargs)
        return result, request

    def test_public_get_and_missing_required(self):
        result, request = self.prepare()
        self.assertEqual(request.call_args.args[0], "https://boards-api.greenhouse.io/v1/boards/figma/jobs/123?questions=true")
        self.assertNotIn("method", request.call_args.kwargs)
        self.assertFalse(result["ready"])
        self.assertEqual(result["answers"]["first_name"], "Ada")
        self.assertIn("resume", result["missing"])
        self.assertIn("question_11", result["missing"])
        self.assertEqual(result["answers"]["question_12"], "")
        self.assertFalse(result["submission_enabled"])

    def test_candidate_persists_old_fields_and_fact_validation(self):
        stored = self.assistant.save_candidate(PROFILE)
        self.assertEqual(stored["work_authorized_us"], "")
        self.assertEqual(ApplicationAssistant(self.path).get_candidate(), stored)
        for value in ({"email": "invalid"}, {"phone": []}, {"sponsorship_now": "maybe"},
                      {"mistral_api_key": "secret"}):
            with self.assertRaises(ValueError):
                self.assistant.save_candidate(value)

    def test_valid_choices_and_file_group_make_ready(self):
        result, _ = self.prepare(has_resume=True)
        saved = self.assistant.save_answers(result["job_key"], {
            "question_10": "I value thoughtful collaboration.", "question_11": "1", "question_12": "1"})
        self.assertTrue(saved["ready"])
        self.assertEqual(saved["missing"], [])
        self.assertTrue(saved["requires_review"])
        self.assertFalse(saved["submission_enabled"])

    def test_reject_unknown_choice_and_file_path_atomically(self):
        result, _ = self.prepare()
        for answers in ({"question_11": "999"}, {"invented": "yes"}, {"resume": "C:/cv.pdf"},
                        {"question_11": ["1"]}):
            with self.assertRaises(ValueError):
                self.assistant.save_answers(result["job_key"], answers)
        self.assertEqual(self.assistant.get_draft(result["job_key"])["answers"], result["answers"])

    def test_canonical_key_survives_post_version(self):
        result, _ = self.prepare()
        self.assistant.save_answers(result["job_key"], {"question_10": "A saved answer."})
        changed = dict(JOB, job_id="124", posting_id="greenhouse:figma:124")
        source = dict(copy.deepcopy(SOURCE), id=124, absolute_url="https://boards.greenhouse.io/figma/jobs/124")
        with patch("nyc_job_match.apply.request_json", return_value=source):
            latest = self.assistant.prepare(changed, PROFILE, "Built reliable Python and SQL tools.", Settings(), use_mistral=False)
        self.assertEqual(latest["job_key"], "greenhouse:figma:999")
        self.assertEqual(latest["job_id"], "124")
        self.assertEqual(latest["answers"]["question_10"], "A saved answer.")
        self.assertEqual(ApplicationAssistant(self.path).get_draft(latest["job_key"]), latest)

    def test_source_failure_keeps_saved_draft(self):
        result, _ = self.prepare()
        with patch("nyc_job_match.apply.request_json", side_effect=ServiceError("unavailable")):
            with self.assertRaisesRegex(ServiceError, "Your existing draft is preserved"):
                self.assistant.prepare(JOB, PROFILE, "", Settings(), use_mistral=False)
        self.assertEqual(self.assistant.get_draft(result["job_key"]), result)

    def test_board_provider_and_link_are_restricted(self):
        for job in (dict(JOB, provider="nyc"), dict(JOB, board="unknown"),
                    dict(JOB, job_id="../etc"), dict(JOB, posting_id="other")):
            with patch("nyc_job_match.apply.request_json") as request:
                with self.assertRaises(ValueError):
                    self.assistant.prepare(job, PROFILE, "", Settings(), use_mistral=False)
                request.assert_not_called()
        with patch("nyc_job_match.apply.request_json", return_value=dict(SOURCE, absolute_url="https://evil.example/form")):
            with self.assertRaises(ServiceError):
                self.assistant.prepare(JOB, PROFILE, "", Settings(), use_mistral=False)

    def test_ai_exact_quote_and_protected_questions(self):
        settings = Settings(mistral_api_key="test")
        raw = {"drafts": [
            {"name": "question_10", "answer": "I have built Python and SQL tools.", "resume_quote": "Built reliable Python and SQL tools."},
            {"name": "question_11", "answer": "1", "resume_quote": "Built reliable Python and SQL tools."},
            {"name": "question_12", "answer": "1", "resume_quote": "Built reliable Python and SQL tools."},
        ]}
        with patch("nyc_job_match.apply.request_json", return_value=copy.deepcopy(SOURCE)), \
             patch("nyc_job_match.apply.MistralClient.structured", return_value=raw) as model:
            result = self.assistant.prepare(JOB, PROFILE, "Built reliable Python and SQL tools.", settings)
        self.assertEqual(len(result["drafts"]), 1)
        self.assertEqual(result["answers"]["question_11"], "")
        self.assertEqual(result["answers"]["question_12"], "")
        payload = model.call_args.args[0][1]["content"]
        self.assertNotIn("ada@example.com", payload)
        self.assertNotIn('"name": "question_11"', payload)
        self.assertTrue(result["warnings"])

    def test_ai_uninvented_resume_grounding_and_cache(self):
        self.assistant = ApplicationAssistant(self.path, ModelCache(self.path))
        settings = Settings(mistral_api_key="test")
        raw = {"drafts": [{"name": "question_10", "answer": "I led a NASA mission.",
                          "resume_quote": "I led a NASA mission."}]}
        with patch("nyc_job_match.apply.request_json", return_value=copy.deepcopy(SOURCE)), \
             patch("nyc_job_match.apply.MistralClient.structured", return_value=raw) as model:
            first = self.assistant.prepare(JOB, PROFILE, "Built reliable Python and SQL tools.", settings)
            second = self.assistant.prepare(JOB, PROFILE, "Built reliable Python and SQL tools.", settings)
        self.assertEqual(first["drafts"], [])
        self.assertEqual(second["answers"]["question_10"], "")
        self.assertEqual(model.call_count, 1)
        self.assertEqual(second["cache"]["hits"], 1)

    def test_explicit_us_facts_only_with_yes_no_options(self):
        source = copy.deepcopy(SOURCE)
        source["questions"] += [
            question("Will you need sponsorship now or in the future in the US?", "question_20", "multi_value_single_select", values=YES_NO),
            question("Do you need sponsorship currently in the USA?", "question_21", "multi_value_single_select", values=YES_NO),
            question("Will you need sponsorship in the future in the United States?", "question_22", "multi_value_single_select", values=YES_NO),
            question("Are you authorized to work in this country?", "question_23", "multi_value_single_select", values=YES_NO),
            question("Are you authorized to work in the US?", "question_24", "multi_value_single_select",
                     values=[{"label": "Yes, unrestricted", "value": 9}, {"label": "No", "value": 0}]),
        ]
        profile = dict(PROFILE, work_authorized_us="yes", sponsorship_now="no", sponsorship_future="yes")
        result, _ = self.prepare(source, profile)
        self.assertEqual(result["answers"]["question_11"], "1")
        self.assertEqual(result["answers"]["question_20"], "1")
        self.assertEqual(result["answers"]["question_21"], "0")
        self.assertEqual(result["answers"]["question_22"], "1")
        self.assertEqual(result["answers"]["question_23"], "")
        self.assertEqual(result["answers"]["question_24"], "")
        self.assertEqual(result["answers"]["question_12"], "")

    def test_combined_sponsorship_unknown_and_no(self):
        source = dict(copy.deepcopy(SOURCE), questions=[
            question("Need sponsorship now or in the future in the United States?", "question_20", "multi_value_single_select", values=YES_NO)])
        for now, future, expected in (("no", "", ""), ("", "no", ""), ("no", "no", "0"), ("", "yes", "1")):
            result, _ = self.prepare(source, dict(PROFILE, sponsorship_now=now, sponsorship_future=future))
            # Clear a previous user answer so each case is independent.
            self.assistant.save_answers(result["job_key"], {"question_20": ""})
            result, _ = self.prepare(source, dict(PROFILE, sponsorship_now=now, sponsorship_future=future))
            self.assertEqual(result["answers"]["question_20"], expected)

    def test_source_required_consent_is_not_defaulted(self):
        source = dict(copy.deepcopy(SOURCE), data_compliance=[
            {"type": "gdpr", "requires_processing_consent": True}])
        result, _ = self.prepare(source)
        name = "data_compliance[gdpr_processing_consent_given]"
        self.assertIn(name, result["missing"])
        self.assertEqual(result["answers"][name], "")
        self.assertEqual(len(result["consent_questions"]), 1)

    def test_resume_fingerprint_and_clear_keep_candidate(self):
        self.assistant.save_candidate(PROFILE)
        result, _ = self.prepare()
        self.assertEqual(result["resume_fingerprint"], hashlib.sha256(
            "Built reliable Python and SQL tools.".encode("utf-8")).hexdigest())
        self.assertEqual(self.assistant.get_draft(result["job_key"])["resume_fingerprint"],
                         result["resume_fingerprint"])
        self.assistant.clear_drafts()
        self.assertIsNone(self.assistant.get_draft(result["job_key"]))
        self.assertEqual(self.assistant.get_candidate()["first_name"], "Ada")

    def test_prepare_does_not_reuse_changed_resume_or_sensitive_answers(self):
        result, _ = self.prepare(profile=dict(PROFILE, work_authorized_us="yes"))
        self.assistant.save_answers(result["job_key"], {"question_10": "Saved text.", "question_12": "1"})
        with patch("nyc_job_match.apply.request_json", return_value=copy.deepcopy(SOURCE)):
            updated = self.assistant.prepare(JOB, PROFILE, "A completely different resume.", Settings(), use_mistral=False)
        self.assertEqual(updated["answers"]["question_10"], "")
        self.assertEqual(updated["answers"]["question_11"], "")
        self.assertEqual(updated["answers"]["question_12"], "")


if __name__ == "__main__":
    unittest.main()
