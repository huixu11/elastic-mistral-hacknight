"""Download and normalize the official NYC Jobs open-data snapshot.

The public Socrata endpoint needs no API key.  Counts refer to all source rows;
only External postings are retained in ``jobs``.  A posting's ID distinguishes
levels, salary ranges, title codes and substantive text, while update/deadline
changes replace an older copy of that same posting.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SOURCE_URL = "https://data.cityofnewyork.us/City-Government/Jobs-NYC-Postings/kpav-sd4t/about_data"
API_URL = "https://data.cityofnewyork.us/resource/kpav-sd4t.json"
METADATA_URL = "https://data.cityofnewyork.us/api/views/kpav-sd4t.json"
PAGE_SIZE = 1000

TEXT_FIELDS = (
    "job_id", "posting_type", "business_title", "agency", "work_location",
    "job_description", "minimum_qual_requirements", "preferred_skills",
    "additional_information", "residency_requirement", "title_classification",
    "to_apply", "level", "title_code_no", "salary_frequency",
)
DATE_FIELDS = ("posting_date", "posting_updated", "process_date", "post_until")
SALARY_FIELDS = ("salary_range_from", "salary_range_to")


class DataFetchError(RuntimeError):
    """A failed download; the previous on-disk snapshot remains intact."""


def _text(value: Any) -> str:
    if value is None:
        return ""
    value = html.unescape(str(value))
    value = re.sub(r"<[^>]*>", " ", value)
    return " ".join(value.split())


def _salary(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(_text(value).replace(",", "").replace("$", ""))
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _parse_date(value: Any) -> date | None:
    """Unknown textual deadlines stay unknown, rather than becoming expired."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    value = _text(value)
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for pattern in (
        "%d-%b-%Y", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y",
        "%B %d, %Y", "%b %d, %Y", "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M %p",
    ):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            pass
    return None


def _timestamp(value: Any) -> datetime:
    value = _text(value)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except ValueError:
        parsed = _parse_date(value)
        return datetime.combine(parsed or date.min, datetime.min.time(), timezone.utc)


def _new_york_today() -> date:
    now = datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            return now.astimezone(ZoneInfo("America/New_York")).date()
        except ZoneInfoNotFoundError:
            pass
    except ImportError:
        pass
    # Windows may not have tzdata installed.  Since 2007, NYC daylight saving
    # starts on March's second Sunday at 07:00 UTC and ends on November's first
    # Sunday at 06:00 UTC.  No optional dependency is needed for today's date.
    year = now.year
    spring_day = 1 + (6 - date(year, 3, 1).weekday()) % 7 + 7
    autumn_day = 1 + (6 - date(year, 11, 1).weekday()) % 7
    spring = datetime(year, 3, spring_day, 7, tzinfo=timezone.utc)
    autumn = datetime(year, 11, autumn_day, 6, tzinfo=timezone.utc)
    offset = -4 if spring <= now < autumn else -5
    return (now + timedelta(hours=offset)).date()


def is_open(job: dict[str, Any], as_of: date | None = None) -> bool:
    """A listed posting passes the deadline check if its deadline is not past.

    A blank or unparseable deadline is unknown; this check does not certify
    that a vacancy is still accepting applications.
    """
    deadline = _parse_date(job.get("post_until"))
    return deadline is None or deadline >= (as_of or _new_york_today())


def normalize_jobs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    versions: dict[str, dict[str, Any]] = {}
    ranks: dict[str, tuple[Any, ...]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise DataFetchError("Invalid NYC Jobs data format: job records must be objects.")
        if _text(row.get("posting_type")).casefold() != "external":
            continue
        job: dict[str, Any] = {name: _text(row.get(name)) for name in TEXT_FIELDS + DATE_FIELDS}
        job["posting_type"] = "External"
        frequency = job["salary_frequency"]
        job["salary_frequency"] = {"annual": "Annual", "hourly": "Hourly", "daily": "Daily"}.get(frequency.casefold(), frequency)
        job.update({name: _salary(row.get(name)) for name in SALARY_FIELDS})
        # Dates can change when an otherwise identical posting is refreshed.
        # Include the full substantive record so distinct levels and salaries
        # are retained, rather than silently collapsing everything by job_id.
        identity = {name: job[name] for name in TEXT_FIELDS + SALARY_FIELDS}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()[:24]
        posting_id = f"{job['job_id'] or 'unknown'}-{digest}"
        job["posting_id"] = posting_id
        job["source_url"] = SOURCE_URL
        job.update(application_key="nyc:" + job["job_id"], provider="nyc", employer_type="government",
                   apply_url="https://cityjobs.nyc.gov/")
        rank = (
            _timestamp(job["posting_updated"]),
            _parse_date(job["post_until"]) or date.min,
            _timestamp(job["posting_date"]),
            _timestamp(job["process_date"]),
        )
        if posting_id not in ranks or rank > ranks[posting_id]:
            versions[posting_id] = job
            ranks[posting_id] = rank
    return sorted(versions.values(), key=lambda job: (job["job_id"], job["level"], job["posting_id"]))


def _empty_snapshot() -> dict[str, Any]:
    return {"jobs": [], "fetched_at": None, "source_updated_at": None, "source_url": SOURCE_URL, "raw_count": 0}


def load_snapshot(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return _empty_snapshot()
    try:
        with path.open("r", encoding="utf-8") as handle:
            snapshot = json.load(handle)
    except (OSError, ValueError) as exc:
        raise DataFetchError("Could not read the local job snapshot. Download NYC Jobs data again.") from exc
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("jobs"), list):
        raise DataFetchError("Invalid local job snapshot format. Download NYC Jobs data again.")
    return {**_empty_snapshot(), **snapshot}


def _read_json_url(url: str) -> Any:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "NYC-Job-Match-Hackathon/1.0"})
    try:
        with urlopen(request, timeout=45) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise DataFetchError(f"NYC Open Data request failed (HTTP {exc.code}). Please try again later.") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise DataFetchError("Could not connect to NYC Open Data. Check your connection and download again.") from exc
    except (ValueError, UnicodeError) as exc:
        raise DataFetchError("NYC Open Data returned invalid JSON. Please try again later.") from exc


def _row_count() -> int:
    result = _read_json_url(f"{API_URL}?{urlencode({'$select': 'count(*)'})}")
    try:
        count = int(result[0].get("count", result[0].get("count_1")))
        if count < 0:
            raise ValueError("negative row count")
        return count
    except (IndexError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DataFetchError("Invalid NYC Open Data record count. The previous snapshot was preserved.") from exc


def _source_updated_at() -> str | None:
    # Data retrieval still works if the optional metadata service is down.
    try:
        metadata = _read_json_url(METADATA_URL)
        stamp = metadata.get("rowsUpdatedAt")
        return datetime.fromtimestamp(float(stamp), timezone.utc).isoformat() if stamp is not None else None
    except (DataFetchError, AttributeError, TypeError, ValueError, OverflowError, OSError):
        return None


def fetch_snapshot(path: Path) -> dict[str, Any]:
    """Fetch every source row, validate the count, then atomically save JSON."""
    updated_before = _source_updated_at()
    expected_count = _row_count()
    rows: list[dict[str, Any]] = []
    for offset in range(0, expected_count, PAGE_SIZE):
        params = {"$limit": PAGE_SIZE, "$offset": offset, "$order": ":id"}
        page = _read_json_url(f"{API_URL}?{urlencode(params)}")
        if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
            raise DataFetchError("Invalid NYC Open Data page format. The previous snapshot was preserved.")
        rows.extend(page)
    count_after = _row_count()
    updated_after = _source_updated_at()
    if len(rows) != expected_count or count_after != expected_count:
        raise DataFetchError("Downloaded records do not match the official count; the source may be updating. Please retry. The previous snapshot was preserved.")
    if updated_before is not None and updated_after is not None and updated_before != updated_after:
        raise DataFetchError("NYC Jobs changed during the download. Please retry. The previous snapshot was preserved.")
    snapshot = {
        "jobs": normalize_jobs(rows),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source_updated_at": updated_after or updated_before,
        "source_url": SOURCE_URL,
        "raw_count": len(rows),
    }
    save_snapshot(path, snapshot)
    return snapshot


def save_snapshot(path: Path, snapshot: dict[str, Any]) -> None:
    """Replace a complete snapshot only after its temporary file is flushed."""
    path = Path(path)
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary_path = Path(handle.name)
            json.dump(snapshot, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as exc:
        raise DataFetchError("Could not save the job snapshot. Check directory permissions and disk space. The previous snapshot was preserved.") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
