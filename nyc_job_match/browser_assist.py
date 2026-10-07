"""Open an interactive browser to prefill a Greenhouse application.

The browser helper never clicks Submit or answers an absent personal fact.
Original resumes travel through stdin in memory, not a temporary file.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import threading
import uuid
from pathlib import Path


class BrowserAssistant:
    def __init__(self, resumes, on_confirmation):
        self.resumes = resumes
        self.on_confirmation = on_confirmation
        self.runs = {}
        self.lock = threading.Lock()

    def start(self, job, draft):
        if job.get("provider") != "greenhouse" or job.get("board") not in ("datadog", "figma") or not str(job.get("job_id", "")).isdigit():
            raise ValueError("Browser autofill currently supports Datadog and Figma Greenhouse postings.")
        original = self.resumes.get_original()
        if original is None:
            raise ValueError("Upload and save a resume first. The employer site needs the original attachment.")
        answers = draft.get("answers", {})
        if any(not str(answers.get(field) or "").strip() for field in ("first_name", "last_name", "email")):
            raise ValueError("Save your first name, last name, and email in the application first.")
        executable = shutil.which("node")
        if not executable:
            raise ValueError("Browser autofill requires Node.js. You can still use the official application link.")
        filename, content = original
        fields = []
        for question in draft.get("questions", []) + draft.get("location_questions", []) + draft.get("consent_questions", []):
            for field in question.get("fields", []):
                if field["name"] in answers:
                    fields.append({**field, "label": question["label"], "answer": answers[field["name"]]})
        payload = {"board": job["board"], "job_id": str(job["job_id"]), "fields": fields,
                   "resume": {"name": filename, "base64": base64.b64encode(content).decode("ascii")}}
        key = job["application_key"]
        with self.lock:
            for identifier, run in self.runs.items():
                if run["job_key"] == key and run["status"] in ("started", "filled"):
                    return {"run_id": identifier, **run}
            identifier = uuid.uuid4().hex
            self.runs[identifier] = {"job_key": key, "status": "started", "message": "Opening the official application in Edge. Review the filled fields and submit on the employer site."}
        threading.Thread(target=self._run, args=(identifier, executable, payload, dict(job)), daemon=True).start()
        return {"run_id": identifier, **self.runs[identifier]}

    def status(self, identifier):
        with self.lock:
            if identifier not in self.runs:
                raise ValueError("Application window not found.")
            return {"run_id": identifier, **self.runs[identifier]}

    def _run(self, identifier, executable, payload, job):
        env = os.environ.copy()
        bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright"
        if bundled.exists():
            env.setdefault("JOB_MATCH_PLAYWRIGHT_MODULE", str(bundled))
        try:
            process = subprocess.Popen([executable, str(Path(__file__).with_name("browser_fill.cjs"))], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            process.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            process.stdin.close()
            for raw in iter(process.stdout.readline, b""):
                try:
                    event = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeError):
                    continue
                if event.get("status") not in ("filled", "confirmed", "closed", "error", "unknown"):
                    continue
                if event["status"] == "confirmed":
                    self.on_confirmation(job, event.get("receipt_url", ""))
                with self.lock:
                    self.runs[identifier].update({field: event[field] for field in ("status", "message", "filled", "missing") if field in event})
            process.stdout.close()
            process.wait()
            with self.lock:
                if self.runs[identifier]["status"] in ("started", "filled"):
                    self.runs[identifier].update(status="unknown", message="The application window ended without a confirmed receipt. Check the employer site before marking it applied.")
        except Exception:
            with self.lock:
                self.runs[identifier].update(status="error", message="Unable to complete browser autofill. Use the official application link. Autofill requires Playwright and Edge.")
