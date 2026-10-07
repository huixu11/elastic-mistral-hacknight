"""Small REST clients; all credentials stay in the local Python process."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class ServiceError(RuntimeError):
    pass


class NoRedirects(HTTPRedirectHandler):
    # Never forward an Authorization header to an unexpected redirect target.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url, *, method="GET", headers=None, body=None, timeout=45):
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=body, headers=headers or {}, method=method)
    try:
        with build_opener(NoRedirects).open(req, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        # Do not echo provider bodies or headers: these can contain credentials.
        hints = {401: "Invalid API key", 403: "Insufficient API key permissions", 404: "Resource not found", 402: "Account credits or billing are not ready", 429: "Rate limit exceeded"}
        raise ServiceError(f"HTTP {exc.code}: {hints.get(exc.code, 'Service request failed')}") from None
    except (URLError, TimeoutError, OSError) as exc:
        raise ServiceError(f"Network connection failed ({type(exc).__name__}). Check your connection or try again.") from None
    except (ValueError, UnicodeError):
        raise ServiceError("The service did not return valid JSON.") from None


@dataclass(frozen=True)
class Settings:
    elastic_endpoint: str = ""
    elastic_api_key: str = ""
    mistral_api_key: str = ""
    mistral_model: str = "ministral-3b-2512"
    index: str = "nyc-job-match"

    @property
    def elastic_configured(self):
        return bool(self.elastic_endpoint and self.elastic_api_key)

    @property
    def mistral_configured(self):
        return bool(self.mistral_api_key)

    def validated(self):
        if self.elastic_endpoint:
            parts = urlsplit(self.elastic_endpoint)
            if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
                raise ValueError("The Elasticsearch endpoint must be an HTTPS root URL, such as https://example.es.elastic.cloud.")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,100}", self.index):
            raise ValueError("Index names may contain only lowercase letters, digits, hyphens, and underscores.")
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", self.mistral_model):
            raise ValueError("Invalid Mistral model name.")
        return self


class ElasticClient:
    def __init__(self, settings):
        self.settings = settings.validated()

    def call(self, path, *, method="GET", body=None, ndjson=False):
        headers = {"Authorization": "ApiKey " + self.settings.elastic_api_key,
                   "Content-Type": "application/x-ndjson" if ndjson else "application/json"}
        return request_json(self.settings.elastic_endpoint.rstrip("/") + path,
                            method=method, headers=headers, body=body, timeout=60)

    def search(self, query):
        return self.call("/" + quote(self.settings.index, safe="") + "/_search", method="POST", body=query)

    def ingest(self, jobs, snapshot_id):
        index = quote(self.settings.index, safe="")
        try:
            self.call("/" + index)
        except ServiceError as exc:
            if "HTTP 404:" not in str(exc):
                raise
            properties = {
                "posting_id": {"type": "keyword"}, "job_id": {"type": "keyword"},
                "snapshot_id": {"type": "keyword"},
                "posting_type": {"type": "keyword"}, "salary_frequency": {"type": "keyword"},
                "salary_range_from": {"type": "double"}, "salary_range_to": {"type": "double"},
                "agency": {"type": "keyword"}, "level": {"type": "keyword"},
                "title_classification": {"type": "keyword"},
                "career_stage": {"type": "keyword"},
            }
            for field in ("business_title", "work_location", "job_description", "minimum_qual_requirements", "preferred_skills", "additional_information", "residency_requirement", "to_apply"):
                properties[field] = {"type": "text"}
            self.call("/" + index, method="PUT", body={"mappings": {"_meta": {"application": "nyc-job-match"}, "dynamic": False, "properties": properties}})
        else:
            # Detect an old/incompatible index rather than silently comparing strings.
            mapping = self.call("/" + index + "/_mapping")
            index_mapping = mapping[self.settings.index]["mappings"]
            if index_mapping.get("_meta", {}).get("application") != "nyc-job-match":
                raise ServiceError("This index was not created by NYC Job Match. Set a new JOB_MATCH_INDEX to avoid overwriting another project's data.")
            props = index_mapping.get("properties", {})
            if props.get("salary_range_from", {}).get("type") not in ("double", "float", "long", "integer"):
                raise ServiceError("The existing index's salary field is not numeric. Set a new JOB_MATCH_INDEX in .env and retry.")
            if "career_stage" not in props:
                self.call("/" + index + "/_mapping", method="PUT", body={"properties": {"career_stage": {"type": "keyword"}}})
        indexed = 0
        for offset in range(0, len(jobs), 200):
            lines = []
            for job in jobs[offset:offset + 200]:
                lines.extend((json.dumps({"index": {"_index": self.settings.index, "_id": job["posting_id"]}}),
                              json.dumps({**job, "snapshot_id": snapshot_id}, ensure_ascii=False, allow_nan=False)))
            result = self.call("/_bulk?refresh=wait_for", method="POST", body=("\n".join(lines) + "\n").encode("utf-8"), ndjson=True)
            if result.get("errors"):
                raise ServiceError("Some jobs could not be indexed. Check index mappings and write permissions; retrying updates the same document IDs.")
            indexed += len(result.get("items", []))
        return {"indexed": indexed, "index": self.settings.index}


class MistralClient:
    def __init__(self, settings):
        self.settings = settings

    def structured(self, messages, schema, name):
        result = request_json("https://api.mistral.ai/v1/chat/completions", method="POST", headers={
            "Authorization": "Bearer " + self.settings.mistral_api_key, "Content-Type": "application/json"},
            body={"model": self.settings.mistral_model, "temperature": 0,
                  "max_tokens": 3500, "messages": messages,
                  "response_format": {"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}}})
        try:
            content = result["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content)
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise ValueError
            return parsed
        except (KeyError, IndexError, TypeError, ValueError):
            raise ServiceError("Mistral did not return a valid structured response.") from None
