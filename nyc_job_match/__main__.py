from __future__ import annotations

import argparse
import json

from .clients import ElasticClient, ServiceError
from .data import DataFetchError, load_snapshot
from .engine import search
from .refresh import refresh_snapshot
from .server import DEFAULT_DATA, load_settings, serve
from .scope import software_snapshot


def main():
    parser = argparse.ArgumentParser(description="NYC Job Match — Python 3.10+, no pip install required")
    parser.add_argument("command", choices=("serve", "fetch", "ingest", "search"), nargs="?", default="serve")
    parser.add_argument("--data", default=str(DEFAULT_DATA))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--profile", default="I have Python, SQL and software development experience")
    parser.add_argument("--min-salary", type=float, default=80000)
    parser.add_argument("--backend", choices=("auto", "local", "elasticsearch"), default="auto")
    parser.add_argument("--no-mistral", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "fetch":
            from pathlib import Path
            snapshot = software_snapshot(refresh_snapshot(Path(args.data)))
            print(json.dumps({"raw_rows": snapshot["raw_count"], "public_postings": len(snapshot["jobs"]),
                              "sources": snapshot["sources"], "warnings": snapshot["warnings"],
                              "source_updated_at": snapshot.get("source_updated_at"), "saved_to": args.data}, indent=2))
        elif args.command == "ingest":
            settings = load_settings()
            if not settings.elastic_configured:
                raise ValueError("Set ELASTIC_ENDPOINT and ELASTIC_API_KEY in .env first, or use the app settings panel.")
            snapshot = software_snapshot(load_snapshot(args.data))
            if not snapshot["jobs"]:
                raise ValueError("Run python -m nyc_job_match fetch first.")
            print(json.dumps(ElasticClient(settings).ingest(snapshot["jobs"], snapshot["fetched_at"]), indent=2))
        elif args.command == "search":
            result = search(software_snapshot(load_snapshot(args.data)), load_settings(), {"profile": args.profile,
                "min_salary": args.min_salary, "backend": args.backend, "use_mistral": not args.no_mistral})
            print(json.dumps(result, ensure_ascii=True, indent=2))
        else:
            serve(args.data, args.port)
    except (ValueError, OSError, ServiceError, DataFetchError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
