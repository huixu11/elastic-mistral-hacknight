"""The hackathon pilot focuses on roles explicitly about software engineering."""

import re
from .career import classify_career

_SOFTWARE = re.compile(r"\b(software (?:engineer|engineering|developer)|(?:front[ -]?end|back[ -]?end|full[ -]?stack|web|mobile|ios|android|application|applications|api) (?:engineer|developer)|devops (?:engineer|engineering)|site reliability (?:engineer|engineering)|sre|(?:platform|infrastructure) software engineer)\b", re.I)
_EXCLUDED = re.compile(r"\b(sales engineer|solutions engineer|civil engineer|mechanical engineer|electrical engineer)\b", re.I)


def is_software_job(job):
    title = str(job.get("business_title") or "")
    return bool(_SOFTWARE.search(title) and not _EXCLUDED.search(title))


def software_snapshot(snapshot):
    jobs = [classify_career(job) for job in snapshot["jobs"] if is_software_job(job)]
    sources = []
    for source in snapshot.get("sources", []):
        selected = [job for job in jobs if (source.get("board") and job.get("board") == source["board"])
                    or (source.get("provider") == "nyc" and job.get("provider", "nyc") == "nyc")]
        sources.append({**source, "company": "NYC Government" if source.get("provider") == "nyc" else source.get("company"), "total_job_count": source.get("total_job_count", source.get("job_count", 0)), "job_count": len(selected)})
    return {**snapshot, "jobs": jobs, "sources": sources, "scope": "software_engineering"}
