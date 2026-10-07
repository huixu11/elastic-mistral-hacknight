"""Conservative career labels grounded in employer wording."""

import re
import unicodedata

_INTERN = re.compile(r"(?<![A-Za-z0-9_])(?:intern|internship|co[ -]?op)(?![A-Za-z0-9_])", re.I)
_SENIOR = re.compile(r"\b(?:senior|sr\.?|staff|principal|lead|manager|director|architect)\b", re.I)
_SENIOR_ONLY = re.compile(r"(?<![A-Za-z0-9_])(?:senior|sr\.?)(?![A-Za-z0-9_])", re.I)
_ENTRY = re.compile(r"(?<![A-Za-z0-9_])(?:entry[\s-]*level|junior|jr\.?)(?![A-Za-z0-9_])", re.I)
_GRAD = re.compile(r"(?<![A-Za-z0-9_])(?:new[\s-]*grad(?:uate)?s?|recent graduates?|early[\s-]*career|university graduates?|graduate software engineer)(?![A-Za-z0-9_])", re.I)
_DESC_GRAD = re.compile(r"\b(?:this (?:role|position|opportunity) is (?:for|designed for|open to) (?:new|recent) graduates|(?:seeking|looking for|hiring) (?:new|recent) graduates|you (?:are|will be) (?:a )?(?:new|recent) graduate)\b", re.I)


def classify_career(job):
    title = str(job.get("business_title") or "")
    stage, evidence = "unspecified", None
    for regex, label in ((_INTERN, "internship"), (_SENIOR_ONLY, "senior"), (_SENIOR, "experienced"), (_GRAD, "new_grad"), (_ENTRY, "entry_level")):
        match = regex.search(title)
        if match:
            stage, evidence = label, {"field": "business_title", "quote": match.group(0)}
            break
    if evidence is None:
        for field in ("job_description", "minimum_qual_requirements"):
            match = _DESC_GRAD.search(str(job.get(field) or ""))
            if match:
                stage, evidence = "new_grad", {"field": field, "quote": match.group(0)}
                break
    return {**job, "career_stage": stage, "career_stage_evidence": evidence}


def stage_of(job):
    return classify_career(job)["career_stage"]


def stage_matches(job, requested):
    stage = stage_of(job)
    return requested == "any" or stage == requested or (requested == "entry_level" and stage == "new_grad")


_INTENT = re.compile(r"(?<![A-Za-z])(?:looking for|look for|want(?: to (?:find|apply for))?|need|seeking|find(?: me)?|searching for|targeting|interested in|applying for|apply for|prefer)(?![A-Za-z])|\u60f3\u627e|\u5bfb\u627e|\u60f3\u8981|\u5e0c\u671b|\u5e94\u8058|\u7533\u8bf7", re.I)


def infer_stage(profile):
    text = unicodedata.normalize("NFKC", str(profile or ""))
    text = re.sub(r"[\u2010-\u2015\u2212]", "-", text)
    patterns = ((_INTERN, "internship"), (_GRAD, "new_grad"), (_SENIOR_ONLY, "senior"), (_ENTRY, "entry_level"))

    def candidates(segment, background=False):
        found = []
        for regex, stage in patterns:
            for match in regex.finditer(segment):
                before = segment[max(0, match.start() - 70):match.start()]
                if re.search(r"\b(?:not|no|exclude|excluding|avoid|without)\s+(?:\w+\s+){0,3}$|(?:\u4e0d\u8981|\u4e0d\u60f3|\u6392\u9664|\u4e0d\u662f)\s*$", before, re.I):
                    continue
                if background and re.search(r"\b(?:i am|i'm|i have|currently|worked as|experience as)\s+(?:\w+\s+){0,3}$", before, re.I):
                    continue
                found.append((match.start(), stage))
        return sorted(found)

    # A requested-role clause takes precedence over stated career history.
    for intent in reversed(list(_INTENT.finditer(text))):
        target = re.split(r"[,.!?;\n\u3002\uff0c\uff1b]", text[intent.end():], maxsplit=1)[0]
        found = candidates(target)
        if found:
            return found[0][1]
    found = candidates(text, background=True)
    return found[0][1] if found else "any"


def requested_stage(payload, profile):
    explicit = payload.get("career_stage", "auto")
    if explicit not in ("auto", "any", "new_grad", "entry_level", "senior", "internship"):
        raise ValueError("Choose a valid career stage.")
    desired = infer_stage(profile)
    if desired != "any":
        return desired
    return explicit if explicit != "auto" else "any"
