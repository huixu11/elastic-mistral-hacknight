import base64
import io
import unittest
import zipfile
from unittest.mock import patch

from nyc_job_match.clients import ServiceError, Settings
from nyc_job_match.resume import MAX_FILE_BYTES, MAX_TEXT_CHARACTERS, ResumeError, parse_resume


def encoded(content):
    return base64.b64encode(content).decode("ascii")


def docx_bytes(xml, extra=None):
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
        for name, content in (extra or {}).items():
            archive.writestr(name, content)
    return target.getvalue()


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()

    def test_text_utf8_bom_markdown_and_filename_basename(self):
        result = parse_resume(r"C:\private\candidate\Resume.MD", encoded("\ufeff# 简历\nPython SQL\n".encode("utf-8")), self.settings)
        self.assertEqual(result, {"filename": "Resume.MD", "text": "# 简历\nPython SQL", "parser": "utf-8"})

    def test_rejects_invalid_base64_and_whitespace(self):
        for payload in ("not base64!", "YWJj\n", "☃", "a===", "data:text/plain;base64,YQ=="):
            with self.subTest(payload=payload):
                with self.assertRaises(ResumeError):
                    parse_resume("resume.txt", payload, self.settings)

    def test_size_limit_before_parsing(self):
        with self.assertRaisesRegex(ResumeError, "5 MB"):
            parse_resume("resume.txt", encoded(b"x" * (MAX_FILE_BYTES + 1)), self.settings)

    def test_rejects_unsupported_and_binary_text(self):
        for filename, content in (("resume.exe", b"abc"), ("resume.txt", b"abc\x00def"), ("resume.txt", b"\xff\xfeA\x00"), ("resume.txt", b"%PDF-1.7\n")):
            with self.subTest(filename=filename, content=content):
                with self.assertRaises(ResumeError):
                    parse_resume(filename, encoded(content), self.settings)

    def test_rejects_empty_and_excessive_text_without_truncation(self):
        for content in (b"", b" \n\t", b"a" * (MAX_TEXT_CHARACTERS + 1)):
            with self.subTest(size=len(content)):
                with self.assertRaises(ResumeError):
                    parse_resume("resume.txt", encoded(content), self.settings)

    def test_pdf_requires_signature_and_mistral_key(self):
        with patch("nyc_job_match.resume.request_json") as request:
            with self.assertRaisesRegex(ResumeError, "PDF signature"):
                parse_resume("resume.pdf", encoded(b"plain text"), Settings(mistral_api_key="test-key"))
            with self.assertRaisesRegex(ResumeError, "Mistral API key"):
                parse_resume("resume.pdf", encoded(b"%PDF-1.7\n"), self.settings)
            request.assert_not_called()

    def test_pdf_ocr_payload_and_page_text(self):
        content = b"%PDF-1.7\nmock document"
        with patch("nyc_job_match.resume.request_json", return_value={"pages": [{"markdown": "# Resume"}, {"markdown": "Python, SQL"}]}) as request:
            result = parse_resume("/private/Resume.pdf", encoded(content), Settings(mistral_api_key="test-key"))
        self.assertEqual(result, {"filename": "Resume.pdf", "text": "# Resume\n\nPython, SQL", "parser": "mistral-ocr"})
        arguments = request.call_args.kwargs
        self.assertEqual(request.call_args.args[0], "https://api.mistral.ai/v1/ocr")
        self.assertEqual(arguments["method"], "POST")
        self.assertEqual(arguments["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(arguments["body"]["model"], "mistral-ocr-latest")
        self.assertEqual(arguments["body"]["document"], {"type": "document_url", "document_url": "data:application/pdf;base64," + encoded(content)})
        self.assertFalse(arguments["body"]["include_image_base64"])

    def test_pdf_rejects_empty_malformed_and_excessive_ocr(self):
        for result in ({"pages": []}, {"pages": [{"markdown": " "}]}, {"pages": [{}]}, {"pages": [{"markdown": "a" * (MAX_TEXT_CHARACTERS + 1)}]}, None):
            with self.subTest(result_type=type(result).__name__):
                with patch("nyc_job_match.resume.request_json", return_value=result):
                    with self.assertRaises(ResumeError):
                        parse_resume("resume.pdf", encoded(b"%PDF-1.7\n"), Settings(mistral_api_key="test-key"))

    def test_network_error_is_friendly_and_does_not_echo_credentials(self):
        with patch("nyc_job_match.resume.request_json", side_effect=ServiceError("secret-provider-detail")):
            with self.assertRaises(ResumeError) as error:
                parse_resume("resume.pdf", encoded(b"%PDF-1.7\n"), Settings(mistral_api_key="test-key"))
        self.assertIn("Mistral OCR request failed", str(error.exception))
        self.assertNotIn("secret-provider-detail", str(error.exception))

    def test_docx_extracts_body_and_ignores_unrelated_entries(self):
        xml = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Resume</w:t></w:r></w:p><w:p><w:r><w:t>Python</w:t><w:tab/><w:t>SQL</w:t><w:br/><w:t>NYC</w:t></w:r></w:p></w:body></w:document>'
        content = docx_bytes(xml, {"../../private.txt": "not resume content", "word/header1.xml": "excluded header"})
        result = parse_resume("resume.docx", encoded(content), self.settings)
        self.assertEqual(result["text"], "Resume\nPython\tSQL\nNYC")
        self.assertEqual(result["parser"], "docx-xml")

    def test_docx_rejects_invalid_xml_entities_and_oversized_expansion(self):
        inputs = [
            b"not a zip",
            docx_bytes("<broken>"),
            docx_bytes('<!DOCTYPE a [<!ENTITY x "hello">]><a>&x;</a>'),
            docx_bytes('<!DOCTYPE a [<!ENTITY x "hello">]><a>&x;</a>'.encode("utf-16")),
            docx_bytes("x" * (1024 * 1024 + 1)),
            docx_bytes("<a/>", {"ignored.bin": b"x" * (20 * 1024 * 1024)}),
        ]
        for content in inputs:
            with self.subTest(size=len(content)):
                with self.assertRaises(ResumeError):
                    parse_resume("resume.docx", encoded(content), self.settings)

    def test_no_resume_file_is_read_or_written(self):
        with patch("builtins.open", side_effect=AssertionError("filesystem access")), patch("pathlib.Path.open", side_effect=AssertionError("filesystem access")):
            result = parse_resume("/private/resume.txt", encoded(b"Python SQL"), self.settings)
            with patch("nyc_job_match.resume.request_json", return_value={"pages": [{"markdown": "Python"}]}):
                pdf_result = parse_resume("resume.pdf", encoded(b"%PDF-1.7\n"), Settings(mistral_api_key="test-key"))
        self.assertEqual(result["filename"], "resume.txt")
        self.assertEqual(pdf_result["text"], "Python")


if __name__ == "__main__":
    unittest.main()
