"""Loopback-only HTTP app, with no build step or third-party packages."""

from __future__ import annotations

import json
import hashlib
import os
import threading
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from .clients import ElasticClient, Settings, ServiceError, request_json
from .cache import ModelCache
from .apply import ApplicationAssistant
from .browser_assist import BrowserAssistant
from .data import DataFetchError, SOURCE_URL, load_snapshot
from .engine import search
from .refresh import refresh_snapshot
from .profile import ResumeStore
from .workflow import WorkflowStore
from .scope import software_snapshot
from .subscriptions import SubscriptionStore

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT / "data" / "nyc_jobs.json"
REFRESH_INTERVAL = 3 * 60 * 60


def load_settings():
    # Shell environment overrides .env. This parser intentionally executes nothing.
    values = {}
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    def get(name, default=""):
        return os.getenv(name, values.get(name, default)).strip()
    return Settings(elastic_endpoint=get("ELASTIC_ENDPOINT").rstrip("/"), elastic_api_key=get("ELASTIC_API_KEY"),
                    mistral_api_key=get("MISTRAL_API_KEY"), mistral_model=get("MISTRAL_MODEL", "ministral-3b-2512"),
                    index=get("JOB_MATCH_INDEX", "nyc-job-match")).validated()


class App:
    def __init__(self, data_path=DEFAULT_DATA, settings=None, workflow_path=None):
        self.data_path = Path(data_path)
        self.settings = settings or load_settings()
        self.lock = threading.Lock()
        self.ingest_lock = threading.Lock()
        state_path = workflow_path or self.data_path.parent / "job_match.sqlite3"
        self.workflow = WorkflowStore(state_path)
        self.resumes = ResumeStore(state_path)
        self.model_cache = ModelCache(state_path)
        self.resume_lock = threading.RLock()
        self.assistant = ApplicationAssistant(state_path, self.model_cache)
        self.browser = BrowserAssistant(self.resumes, self.workflow.confirm)
        self.subscriptions = SubscriptionStore(state_path)
        self.auto_refresh_stop = threading.Event()
        self.next_refresh_at = datetime.now(timezone.utc) + timedelta(seconds=REFRESH_INTERVAL)
        self.auto_refresh_error = None

    def start_scheduler(self):
        def run():
            while not self.auto_refresh_stop.is_set():
                delay = max(0, (self.next_refresh_at - datetime.now(timezone.utc)).total_seconds())
                if self.auto_refresh_stop.wait(min(60, delay)):
                    return
                if datetime.now(timezone.utc) < self.next_refresh_at:
                    continue
                try:
                    self.refresh()
                    self.auto_refresh_error = None
                except ValueError:
                    self.next_refresh_at = datetime.now(timezone.utc) + timedelta(seconds=60)
                except Exception:
                    self.auto_refresh_error = "Scheduled refresh failed. Existing data is retained; use Refresh data to retry."
                    self.next_refresh_at = datetime.now(timezone.utc) + timedelta(seconds=REFRESH_INTERVAL)
        threading.Thread(target=run, name="job-refresh", daemon=True).start()

    def subscription_status(self):
        return {**self.subscriptions.list(), "refresh_interval_seconds": REFRESH_INTERVAL,
                "next_refresh_at": self.next_refresh_at.isoformat()}

    def subscribe(self, payload):
        return self.subscriptions.save(payload.get("plan"), payload.get("label"),
                                       self.snapshot()["jobs"], self.workflow.submitted_keys())

    def snapshot(self):
        return software_snapshot(load_snapshot(self.data_path))

    def status(self):
        snapshot = self.snapshot()
        return {"job_count": len(snapshot["jobs"]), "fetched_at": snapshot.get("fetched_at"),
                "source_updated_at": snapshot.get("source_updated_at"), "source_url": snapshot.get("source_url", SOURCE_URL),
                "elastic_configured": self.settings.elastic_configured, "mistral_configured": self.settings.mistral_configured,
                "mistral_model": self.settings.mistral_model, "index": self.settings.index,
                "sources": snapshot.get("sources", []), "warnings": snapshot.get("warnings", []), "scope": "software_engineering",
                "auto_refresh_interval_seconds": REFRESH_INTERVAL, "next_refresh_at": self.next_refresh_at.isoformat(),
                "auto_refresh_error": self.auto_refresh_error}

    def refresh(self):
        if not self.ingest_lock.acquire(blocking=False):
            raise ValueError("Jobs are being refreshed or indexed. Please try again shortly.")
        try:
            snapshot = software_snapshot(refresh_snapshot(self.data_path))
            self.next_refresh_at = datetime.now(timezone.utc) + timedelta(seconds=REFRESH_INTERVAL)
            self.auto_refresh_error = None
            result = self.status()
            result["warnings"] = list(snapshot.get("warnings", []))
            if self.settings.elastic_configured:
                try:
                    ingested = ElasticClient(self.settings).ingest(snapshot["jobs"], snapshot["fetched_at"])
                    result["indexed"] = ingested.get("indexed", len(snapshot["jobs"]))
                except ServiceError as exc:
                    result["warnings"].append("Local jobs were refreshed, but Elasticsearch indexing failed. Please index again: " + str(exc))
            self.subscriptions.check(snapshot["jobs"], self.workflow.submitted_keys())
            return result
        finally:
            self.ingest_lock.release()

    def search(self, payload):
        if not self.ingest_lock.acquire(blocking=False):
            raise ValueError("A refresh, indexing operation, or search is in progress. Please try again shortly.")
        try:
            with self.resume_lock:
                effective = dict(payload)
                # A client may deliberately send an empty resume to omit it.
                if "resume_text" not in effective:
                    saved = self.resumes.get()["resume"]
                    effective["resume_text"] = saved["text"] if saved else ""
                result = search(self.snapshot(), self.settings, effective, self.workflow.submitted_keys(), self.model_cache.session())
                statuses = {a["job_key"]: a["status"] for a in self.workflow.list()["applications"]}
                for job in result["jobs"]:
                    job["application_status"] = statuses.get(job["application_key"])
                return result
        finally:
            self.ingest_lock.release()

    def application(self, payload):
        key = payload.get("job_key")
        if not isinstance(key, str) or not key or len(key) > 300:
            raise ValueError("Select a valid job.")
        snapshot = self.snapshot()
        job = next((j for j in snapshot["jobs"] if (j.get("application_key") or "nyc:" + j["job_id"]) == key), None)
        if job is None:
            raise ValueError("This job is no longer in the current snapshot. Refresh your results.")
        return self.workflow.record(job, payload.get("action"))

    def upload_resume(self, payload):
        with self.resume_lock:
            result = self.resumes.upload(payload.get("filename"), payload.get("content_base64"), self.settings)
            if not result["cached"]:
                self.model_cache.clear()
                self.assistant.clear_drafts()
            return result

    def clear_resume(self):
        with self.resume_lock:
            self.model_cache.clear()
            self.assistant.clear_drafts()
            return self.resumes.clear()

    def edit_resume(self, payload):
        with self.resume_lock:
            previous = self.resumes.get()["resume"]
            result = self.resumes.update_text(payload.get("text"))
            if previous is None or previous["text"] != result["text"]:
                self.model_cache.clear()
                self.assistant.clear_drafts()
            return result

    def selected_job(self, key):
        if not isinstance(key, str) or not key:
            raise ValueError("Select a valid job.")
        job = next((j for j in self.snapshot()["jobs"] if (j.get("application_key") or "nyc:" + j["job_id"]) == key), None)
        if job is None:
            raise ValueError("This job is no longer in the current snapshot. Search again.")
        return job

    def prepare_application(self, payload):
        job = self.selected_job(payload.get("job_key"))
        if job["application_key"] in self.workflow.submitted_keys():
            raise ValueError("This job is already marked applied. Check the employer site before applying again.")
        with self.resume_lock:
            resume = self.resumes.get()["resume"]
            return self.assistant.prepare(job, self.assistant.get_candidate(), resume["text"] if resume else "",
                                          self.settings, use_mistral=payload.get("use_mistral", True) is True,
                                          has_resume=self.resumes.get_original() is not None)

    def fill_application(self, payload):
        job = self.selected_job(payload.get("job_key"))
        if job["application_key"] in self.workflow.submitted_keys():
            raise ValueError("This job is already marked applied. A duplicate application cannot be started.")
        with self.resume_lock:
            draft = self.assistant.get_draft(job["application_key"])
            resume = self.resumes.get()["resume"]
            if draft is None or resume is None:
                raise ValueError("Save a resume and prepare this job's application first.")
            digest = hashlib.sha256(resume["text"].encode("utf-8")).hexdigest()
            if draft.get("resume_fingerprint") != digest:
                raise ValueError("Your resume changed. Prepare the application again.")
            return self.browser.start(job, draft)

    def configure(self, payload):
        changes = {}
        for field in ("elastic_endpoint", "elastic_api_key", "mistral_api_key", "mistral_model"):
            value = payload.get(field)
            if value is not None and not isinstance(value, str):
                raise ValueError("Connection settings must be text.")
            if value and value.strip():
                if len(value) > 4096:
                    raise ValueError("Connection settings are too long.")
                changes[field] = value.strip().rstrip("/") if field == "elastic_endpoint" else value.strip()
        with self.lock:
            proposed = replace(self.settings, **changes).validated()
            if ("elastic_endpoint" in changes or "elastic_api_key" in changes) and not proposed.elastic_configured:
                raise ValueError("Provide both the Elasticsearch endpoint and encoded API key.")
            if "elastic_endpoint" in changes or "elastic_api_key" in changes:
                # Authenticate the key without requiring cluster-level monitor.
                ElasticClient(proposed).call("/_security/_authenticate")
            if "mistral_api_key" in changes:
                request_json("https://api.mistral.ai/v1/models", headers={"Authorization": "Bearer " + proposed.mistral_api_key})
            self.settings = proposed
        return self.status()

    def ingest(self):
        if not self.settings.elastic_configured:
            raise ValueError("Configure the Elasticsearch endpoint and API key first.")
        if not self.ingest_lock.acquire(blocking=False):
            raise ValueError("Jobs are being indexed. Please try again shortly.")
        try:
            snapshot = self.snapshot()
            if not snapshot["jobs"]:
                raise ValueError("No job data is available. Run python -m nyc_job_match fetch first.")
            return ElasticClient(self.settings).ingest(snapshot["jobs"], snapshot["fetched_at"])
        finally:
            self.ingest_lock.release()


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NYCJobMatch/1.0"

        def log_message(self, format, *args):
            # No request payloads, profiles or API keys in access logs.
            pass

        def send_json(self, status, value):
            data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def valid_origin(self):
            host = self.headers.get("Host", "")
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if host not in allowed:
                return False
            origin = self.headers.get("Origin")
            # Browser requests must originate at this loopback app; CLI requests
            # without Origin remain usable for smoke tests.
            return not origin or origin in {"http://" + item for item in allowed}

        def do_GET(self):
            if not self.valid_origin():
                self.send_json(403, {"error": "Only this local application may access this endpoint."})
                return
            path = urlsplit(self.path).path
            if path == "/api/status":
                try:
                    self.send_json(200, app.status())
                except (ValueError, OSError, DataFetchError):
                    self.send_json(500, {"error": "Unable to read the job snapshot. Refresh the data."})
            elif path == "/api/applications":
                try:
                    self.send_json(200, app.workflow.list())
                except Exception:
                    self.send_json(500, {"error": "Unable to read application history."})
            elif path == "/api/subscriptions":
                self.send_json(200, app.subscription_status())
            elif path == "/api/resume":
                try:
                    self.send_json(200, app.resumes.get())
                except Exception:
                    self.send_json(500, {"error": "Unable to read the saved resume."})
            elif path == "/api/candidate":
                self.send_json(200, {"candidate": app.assistant.get_candidate()})
            elif path == "/api/application/run":
                try:
                    identifier = parse_qs(urlsplit(self.path).query).get("run_id", [""])[0]
                    self.send_json(200, app.browser.status(identifier))
                except ValueError as exc:
                    self.send_json(400, {"error": str(exc)})
            elif path in ("/", "/index.html"):
                data = (Path(__file__).parent / "static" / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_json(404, {"error": "Page not found."})

        def do_POST(self):
            if not self.valid_origin():
                self.send_json(403, {"error": "Only this local application may access this endpoint."})
                return
            try:
                path = urlsplit(self.path).path
                length = int(self.headers.get("Content-Length", "0"))
                maximum = 8 * 1024 * 1024 if path == "/api/resume" else 160_000
                if not 0 < length <= maximum:
                    raise ValueError("Invalid request size.")
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("Requests must use application/json.")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("Invalid request format.")
                if path == "/api/search":
                    result = app.search(payload)
                elif path == "/api/subscriptions":
                    result = app.subscribe(payload)
                elif path == "/api/subscriptions/remove":
                    result = app.subscriptions.remove(payload.get("id"))
                elif path == "/api/resume":
                    result = app.upload_resume(payload)
                elif path == "/api/resume/edit":
                    result = app.edit_resume(payload)
                elif path == "/api/resume/clear":
                    result = app.clear_resume()
                elif path == "/api/refresh":
                    result = app.refresh()
                elif path == "/api/application":
                    result = app.application(payload)
                elif path == "/api/candidate":
                    result = {"candidate": app.assistant.save_candidate(payload)}
                elif path == "/api/application/prepare":
                    result = app.prepare_application(payload)
                elif path == "/api/application/draft":
                    result = app.assistant.save_answers(payload.get("job_key"), payload.get("answers"))
                elif path == "/api/application/fill":
                    result = app.fill_application(payload)
                elif path == "/api/configure":
                    result = app.configure(payload)
                elif path == "/api/ingest":
                    result = app.ingest()
                else:
                    self.send_json(404, {"error": "Endpoint not found."})
                    return
                self.send_json(200, result)
            except (ValueError, UnicodeError) as exc:
                self.send_json(400, {"error": str(exc)})
            except ServiceError as exc:
                self.send_json(502, {"error": str(exc)})
            except DataFetchError as exc:
                self.send_json(500, {"error": str(exc)})
            except Exception as exc:
                print("Request failed:", type(exc).__name__)
                self.send_json(500, {"error": "Request failed. Check the job snapshot or connection settings and try again."})
    return Handler


def serve(data_path=DEFAULT_DATA, port=8765):
    app = App(data_path)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    app.start_scheduler()
    print(f"NYC Job Match: http://127.0.0.1:{port}", flush=True)
    print("API keys remain in local process memory. Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.auto_refresh_stop.set()
        server.server_close()
