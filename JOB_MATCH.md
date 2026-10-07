# NYC Job Match

Upload a resume and describe what you want in a few sentences. Search NYC government software roles and selected public NYC postings from Datadog, Figma, MongoDB, Stripe, and Scale AI. Elasticsearch retrieves jobs and applies salary filters; Mistral plans the query and explains matches with source quotes. This pilot does not cover the entire NYC employment market.

## Run

The main application requires Python 3.10+ and no third-party Python packages:

```powershell
python -m nyc_job_match fetch
python -m nyc_job_match
```

Open http://127.0.0.1:8765 . The fetch command refreshes the government dataset and five connected employer boards, saving the snapshot to the Git-ignored `data/nyc_jobs.json`. The UI shows source coverage, counts, timestamps, and warnings. An unavailable employer contributes no jobs to that refresh. The UI data refresh also imports the new snapshot when Elasticsearch is configured.

Without cloud connections, real downloaded jobs remain available through the local keyword preview. Elasticsearch search, AI explanations, and PDF OCR require valid service configuration.

After updating the code, stop the old server and restart it to enable new APIs. Keys entered in the UI stay in process memory and must be entered again after restarting. Saving a resume does not save cloud keys. Optional browser prefilling requires Node.js, Playwright, and Microsoft Edge; official application links remain available without these dependencies.

## Connect services

1. Create an Elasticsearch Serverless project in [Elastic Cloud](https://cloud.elastic.co/) and copy its Elasticsearch Endpoint.
2. In Kibana **Help → Connection details → API key**, create a key and copy its **Encoded** value. The application needs permission to create, write, refresh, read, and inspect metadata for the `nyc-job-match` index. [Official connection instructions](https://www.elastic.co/docs/solutions/elasticsearch-solution-project/search-connection-details)
3. Create a key in **API keys** in [Mistral Studio](https://console.mistral.ai/), or use an event-provided key. [Mistral setup instructions](https://docs.mistral.ai/getting-started/quickstarts/developer/first-api-request)
4. Enter the values in the UI connection settings, save, and import into Elasticsearch. These UI settings last only for the current server process.

You may instead copy `.env.example` to `.env` and fill it in locally. Git ignores `.env`; environment variables take precedence. The CLI uses `.env` or environment variables. Do not send keys or passwords through chat.

`MISTRAL_MODEL` defaults to `ministral-3b-2512` for inexpensive testing. It supports [structured output](https://docs.mistral.ai/models/ministral-3-3b-25-12); current input and output pricing is $0.10 per million tokens each, subject to the [official pricing page](https://docs.mistral.ai/inference/pricing).

```powershell
python -m nyc_job_match ingest
python -m nyc_job_match search --profile "Python SQL software engineering new grad" --min-salary 0
```

## Resume storage and privacy

- Supported formats: TXT, MD, DOCX, and PDF. Maximum file size: 5 MB. Maximum extracted text: 20,000 characters. TXT, MD, and DOCX are parsed locally; PDF files are sent to Mistral OCR.
- Original resume files, extracted text, candidate facts, application drafts, application records, saved search filters, and notifications persist locally in `data/job_match.sqlite3`. This is a single-user pilot without account isolation. Uploading, editing, or clearing a resume removes derived model cache entries and application drafts; candidate facts remain.
- AI matching and answer drafting send the relevant request and resume text to Mistral. PDF OCR also sends the original PDF. Resume persistence and cloud-key persistence are separate; UI keys remain in process memory.
- Successful structured model responses are cached locally and may include resume quotes. The cache uses an exact hash of the model, messages, schema, and other request parameters, expires after 24 hours, and holds at most 128 entries. Every search still queries Elasticsearch again.

## Greenhouse application assistant

Save ten candidate fields: name and contact details, profile links, US work authorization, and current/future sponsorship requirements. Empty values mean information is missing. The assistant reads each Datadog or Figma posting's real public questions and choices. It prefills explicit saved facts; US authorization and sponsorship facts are reused only for clearly US-specific questions with supported Yes/No choices.

Mistral drafts open-ended text answers only, with exact resume quotes. The backend checks required fields, valid choice IDs, and the resume fingerprint. Truthfulness declarations, privacy consent, and other personal choices require user review. A draft's ready flag means required data is complete; it does not mean an application was submitted.

Optional BrowserAssistant opens Edge on the current job's official Greenhouse form and attempts to fill saved answers and attach the original resume. The user reviews the form, completes missing questions and CAPTCHA, and clicks Submit on the official site. The helper never clicks Submit or CAPTCHA. Other employer boards use their official application links; automated preparation is limited to Datadog and Figma.

Official confirmation is recorded only when the current job's official Greenhouse URL shows an explicit confirmation heading or recognized legacy receipt. Without that signal, the state remains unknown; users may separately mark an application as self-reported. Opening a link, filling a form, or completing a draft is not submission success. Notifications appear in the application; email and SMS delivery are not implemented.

## Two-minute demo

1. Refresh data, configure services, import into Elasticsearch, and upload a sample resume.
2. Search for NYC software, new-grad, or internship roles. Use salary floor 0 to include jobs without confirmed annual pay.
3. Show employer, location, published salary information, and AI explanations with exact source quotes.
4. Change the salary floor and show the result change. Elasticsearch responses include query DSL and aggregations.
5. Prepare a Datadog or Figma application with real questions and draft answers. Use a form fixture to demonstrate browser prefilling and the difference between official confirmation, unknown status, and self-reporting.

No actual application was sent to an employer during validation. The pilot has no employer submission API credentials and does not automatically submit applications.

## Saved search subscriptions

Save a search's keywords, optional title keywords, salary floor, and resolved career stage for in-app new-match alerts. Existing matches form a baseline; a later snapshot check notifies once per subscription and canonical job, excluding jobs already marked applied. These checks use deterministic keyword and posting filters without additional AI or network calls. Saved subscriptions do not store the full resume, model notes, or service keys.

Alerts describe newly matching posts in the connected sources. Missing posts are not treated as confirmed closures, and an unavailable source does not prove that a job closed. The local service must be running to perform checks; email and SMS delivery are not implemented.

## Data and interpretation

The current connector list includes 15 private employer boards: Datadog, Figma, MongoDB, Stripe, Scale AI, Robinhood, Gusto, Brex, Databricks, Jane Street, Chime, Asana, Peloton, Affirm, and Reddit, plus NYC Government. The latest validated snapshot contains 162 NYC software-engineering postings. Counts change with each refresh.

- Government data comes from [Jobs NYC Postings](https://data.cityofnewyork.us/City-Government/Jobs-NYC-Postings/kpav-sd4t/about_data). Employer boards are [Datadog](https://boards-api.greenhouse.io/v1/boards/datadog/jobs?content=true), [Figma](https://boards-api.greenhouse.io/v1/boards/figma/jobs?content=true), [MongoDB](https://boards-api.greenhouse.io/v1/boards/mongodb/jobs?content=true), [Stripe](https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true), and [Scale AI](https://boards-api.greenhouse.io/v1/boards/scaleai/jobs?content=true). Only NYC software-engineering postings are included. New York State alone is insufficient. Hiring status must be checked on the official page.
- The annual floor compares `salary_range_from >= requested minimum` only for explicitly confirmed annual USD pay. Hourly and daily pay are not annualized. Unknown pay is included at floor 0 and excluded at a positive floor; some new-grad and internship jobs therefore require floor 0.
- Structured Greenhouse amounts reported in cents are divided by 100. Multiple applicable annual ranges use a conservative envelope. Source text remains authoritative. The model does not guess missing compensation cadence or amounts.
- Government postings are filtered to External and duplicate versions are merged. Distinct levels, salaries, or substantive text versions remain. External visibility does not waive civil-service exams, eligible-list requirements, employment status, or residency rules. Read the full qualifications and [NYC DCAS guidance](https://www.nyc.gov/site/dcas/employment/civil-service-system.page).
- Parsed past deadlines are excluded; missing or unknown deadlines require checking the official page. Exact quote validation removes unsupported citations but does not establish hiring eligibility.
- Government API Job IDs differ from official page URL identifiers. The application does not invent individual government URLs; it provides source application instructions and [Jobs NYC](https://cityjobs.nyc.gov/). Private jobs use source-provided official links.
- Stable application keys deduplicate drafts and records across posting versions and exclude jobs already marked applied. Repeated marking does not create duplicate notifications. Self-reported records are distinct from observed official confirmation.
- [Greenhouse's public GET API](https://docs.greenhouse.io/job-board.html) exposes jobs and questions, while application POST requires an employer key. The [SmartRecruiters application API](https://developers.smartrecruiters.com/docs/partners-post-an-application) also requires authorization. The pilot does not call either submission endpoint.
- Elasticsearch imports only into an application-owned index identified by `_meta.application=nyc-job-match`, using stable document IDs and snapshot filtering.

## Validation

```powershell
python -m unittest discover -s tests -v
```

Unit, integration, and form-fixture checks have passed. Public source fetching and real Elasticsearch/Mistral matching were verified; invalid citations are discarded with warnings. Edge desktop and mobile layouts were checked for resume upload, resume-only matching, official links, self-reported notifications, and applied-job exclusion.

BrowserAssistant was validated with the local Node/Playwright/Edge runtime and form fixtures. The real Greenhouse page timed out in headless testing, so official-page prefilling remains a prototype without a completed live-page validation. PDF OCR has only mocked tests and has not been validated with a real PDF in the cloud. Users must configure valid service keys locally.
