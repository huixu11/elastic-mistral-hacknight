"""Read selected employers' public Greenhouse boards; never submit applications.

Coverage is limited to the connected boards and postings whose published city
location explicitly includes NYC. Public GET access does not authorize POST.
"""

from __future__ import annotations

import html
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .scope import is_software_job


BOARDS = (
    {"board": "datadog", "company": "Datadog", "careers_url": "https://careers.datadoghq.com/all-jobs/"},
    {"board": "figma", "company": "Figma", "careers_url": "https://www.figma.com/careers/"},
    {"board": "mongodb", "company": "MongoDB", "careers_url": "https://www.mongodb.com/company/careers"},
    {"board": "stripe", "company": "Stripe", "careers_url": "https://stripe.com/jobs/search"},
    {"board": "scaleai", "company": "Scale AI", "careers_url": "https://job-boards.greenhouse.io/scaleai"},
    {"board": "robinhood", "company": "Robinhood", "careers_url": "https://job-boards.greenhouse.io/robinhood"},
    {"board": "gusto", "company": "Gusto", "careers_url": "https://job-boards.greenhouse.io/gusto"},
    {"board": "brex", "company": "Brex", "careers_url": "https://job-boards.greenhouse.io/brex"},
    {"board": "databricks", "company": "Databricks", "careers_url": "https://job-boards.greenhouse.io/databricks"},
    {"board": "janestreet", "company": "Jane Street", "careers_url": "https://job-boards.greenhouse.io/janestreet"},
    {"board": "chime", "company": "Chime", "careers_url": "https://job-boards.greenhouse.io/chime"},
    {"board": "asana", "company": "Asana", "careers_url": "https://job-boards.greenhouse.io/asana"},
    {"board": "peloton", "company": "Peloton", "careers_url": "https://job-boards.greenhouse.io/peloton"},
    {"board": "affirm", "company": "Affirm", "careers_url": "https://job-boards.greenhouse.io/affirm"},
    {"board": "reddit", "company": "Reddit", "careers_url": "https://job-boards.greenhouse.io/reddit"},
)
API_ROOT = "https://boards-api.greenhouse.io/v1/boards"
_YEARLY = re.compile(r"\b(annual|annually|yearly|per\s+year|per\s+annum)\b", re.I)
_NYC = re.compile(r"\b(new\s+york\s+city|nyc|manhattan|brooklyn|queens|bronx|staten\s+island)\b|\bnew\s+york\s*[,]\s*(?:new\s+york|ny)\b|^new\s+york$", re.I)


class JobSourceError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("p", "div", "li", "br", "h1", "h2", "h3", "h4"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1
        self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _plain(value):
    value = str(value or "")
    # Greenhouse's content may contain encoded HTML, including two layers.
    for _ in range(2):
        decoded = html.unescape(value)
        if decoded == value:
            break
        value = decoded
    parser = _PlainText()
    parser.feed(value)
    return " ".join("".join(parser.parts).split())


def _read_json(url):
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "NYC-Job-Match-Hackathon/1.0"})
    try:
        with urlopen(request, timeout=15) as response:
            return json.load(response)
    except HTTPError as exc:
        raise JobSourceError(f"HTTP {exc.code}", exc.code) from None
    except (URLError, TimeoutError, OSError, ValueError, UnicodeError) as exc:
        raise JobSourceError(f"Public job service unavailable ({type(exc).__name__})") from None


def is_nyc_location(location):
    """Accept an explicit NYC city; New York State alone is not sufficient."""
    return bool(_NYC.search(_plain(location)))


def _source_is_nyc(location, board):
    if is_nyc_location(location):
        return True
    # Stripe's official office list names cities without state suffixes.
    # Accept its verified NYC/San Francisco/Seattle list, never NY State.
    cities = {part.strip().casefold() for part in _plain(location).split(",")}
    return board == "stripe" and "new york" in cities and bool(cities & {"san francisco", "seattle"})


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _annual_salary(job):
    """Only declared annual USD ranges are eligible for the annual floor."""
    ranges = []
    for pay in job.get("pay_input_ranges") or []:
        if not isinstance(pay, dict) or pay.get("currency_type") != "USD" or not _YEARLY.search(_plain(pay.get("title"))):
            continue
        lower, upper = _number(pay.get("min_cents")), _number(pay.get("max_cents"))
        if lower is not None and upper is not None and lower <= upper:
            ranges.append((lower / 100, upper / 100))
    if ranges:
        # A conservative envelope does not select the highest regional minimum.
        return min(p[0] for p in ranges), max(p[1] for p in ranges), "Annual"
    # Datadog exposes a structured USD currency_range in the list response.
    # Cadence is separately confirmed by its public pay section's title.
    content = _plain(job.get("content"))
    yearly_salary = re.search(r"(?:annual|yearly|per\s+year)\s+(?:base\s+)?salary|salary.{0,60}(?:annual|yearly|per\s+year)", content, re.I)
    if yearly_salary:
        for field in job.get("metadata") or []:
            if not isinstance(field, dict) or field.get("name") != "Pay Transparency Range" or field.get("value_type") != "currency_range":
                continue
            value = field.get("value")
            if not isinstance(value, dict) or value.get("unit") != "USD":
                continue
            lower, upper = _number(value.get("min_value")), _number(value.get("max_value"))
            if lower is not None and upper is not None and lower <= upper:
                return lower, upper, "Annual"
    return None, None, "Unknown"


def normalize_greenhouse_job(job, board, company):
    identifier = str(job.get("id", "")).strip()
    location = _plain((job.get("location") or {}).get("name"))
    if not identifier or not _source_is_nyc(location, board):
        return None
    canonical = str(job.get("internal_job_id") or identifier)
    salary_from, salary_to, frequency = _annual_salary(job)
    url = str(job.get("absolute_url") or "").strip()
    if not url.startswith("https://"):
        return None
    return {
        "job_id": identifier,
        "posting_id": f"greenhouse:{board}:{identifier}",
        "application_key": f"greenhouse:{board}:{canonical}",
        "canonical_job_id": canonical,
        "provider": "greenhouse", "board": board, "employer_type": "private",
        "posting_type": "External", "agency": company,
        "business_title": _plain(job.get("title")), "work_location": location,
        "job_description": _plain(job.get("content")),
        "minimum_qual_requirements": "", "preferred_skills": "",
        "additional_information": "", "residency_requirement": "",
        "title_classification": "", "level": "", "title_code_no": "",
        "to_apply": "Review the job, resume, and required questions on the employer's official application page before submitting. Opening the page does not submit an application.",
        "apply_url": url, "source_url": url,
        "salary_range_from": salary_from, "salary_range_to": salary_to,
        "salary_frequency": frequency, "salary_currency": "USD" if frequency == "Annual" else "",
        "posting_date": str(job.get("first_published") or ""),
        "posting_updated": str(job.get("updated_at") or ""),
        "post_until": str(job.get("application_deadline") or ""), "process_date": "",
    }


def fetch_private_jobs():
    """Return actual NYC postings, per-board coverage and recoverable warnings."""
    stamp = datetime.now(timezone.utc).isoformat()
    jobs, sources, warnings = [], [], []
    board_rows = {}
    sources_by_board = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks = {pool.submit(_read_json, f"{API_ROOT}/{source['board']}/jobs?content=true"): source for source in BOARDS}
        for future in as_completed(tasks):
            source = tasks[future]
            board = source["board"]
            info = {**source, "provider": "greenhouse", "employer_type": "private",
                    "api_url": f"{API_ROOT}/{board}/jobs?content=true", "fetched_at": stamp,
                    "coverage": "This employer's public NYC software-engineering postings only; coverage does not include all NYC jobs.",
                    "scope": "software_engineering", "status": "ok", "published_count": 0,
                    "nyc_count": 0, "software_count": 0, "job_count": 0, "annual_usd_count": 0}
            sources_by_board[board] = info
            try:
                result = future.result()
                rows = result.get("jobs") if isinstance(result, dict) else None
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    raise JobSourceError("Invalid public job list format")
                info["published_count"] = len(rows)
                nyc_rows = [row for row in rows if _source_is_nyc((row.get("location") or {}).get("name"), board)]
                info["nyc_count"] = len(nyc_rows)
                board_rows[board] = [row for row in nyc_rows if is_software_job({"business_title": row.get("title")})]
                info["software_count"] = len(board_rows[board])
            except Exception as exc:
                info["status"] = "failed"
                warnings.append(f"Could not read {source['company']} public jobs ({type(exc).__name__}). This source contributed no jobs to this refresh.")
        detail_tasks = {}
        for source in BOARDS:
            board = source["board"]
            for row in board_rows.get(board, []):
                if _annual_salary(row)[2] == "Annual":
                    normalized = normalize_greenhouse_job(row, board, source["company"])
                    if normalized:
                        jobs.append(normalized)
                else:
                    url = f"{API_ROOT}/{board}/jobs/{row['id']}?pay_transparency=true"
                    detail_tasks[pool.submit(_read_json, url)] = (source, row)
        detail_failures = {}
        for future in as_completed(detail_tasks):
            source, row = detail_tasks[future]
            board = source["board"]
            try:
                detail = future.result()
                if not isinstance(detail, dict) or str(detail.get("id")) != str(row.get("id")):
                    raise JobSourceError("Invalid job detail format")
                row = {**row, **detail}
            except Exception as exc:
                detail_failures[board] = detail_failures.get(board, 0) + 1
                if isinstance(exc, JobSourceError) and exc.status == 404:
                    continue  # A posting removed since the list download is omitted.
            normalized = normalize_greenhouse_job(row, board, source["company"])
            if normalized:
                jobs.append(normalized)
        for board, count in detail_failures.items():
            warnings.append(f"Could not read salary details for {count} {sources_by_board[board]['company']} jobs. Jobs without confirmed annual USD pay are excluded when the salary floor is positive.")
    jobs = list({job["posting_id"]: job for job in jobs}.values())
    jobs.sort(key=lambda job: (job["agency"], job["job_id"]))
    for source in BOARDS:
        info = sources_by_board[source["board"]]
        selected = [job for job in jobs if job["board"] == source["board"]]
        info["job_count"] = len(selected)
        info["annual_usd_count"] = sum(job["salary_frequency"] == "Annual" for job in selected)
        sources.append(info)
    return {"jobs": jobs, "sources": sources, "warnings": warnings}
