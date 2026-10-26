from __future__ import annotations

import argparse
from datetime import date
import json
import logging
import math
from pathlib import Path
import sys

from .http import Client
from .dates import date_window
from .pipeline import crawl, discovery, load_registry, write_json
from .store import Store, now


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def nonnegative(value):
    number = float(value)
    if not math.isfinite(number) or not number >= 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return number


def day(value):
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def add_date_options(command):
    command.add_argument("--month", help="Only articles with a known publication date in YYYY-MM; excludes undated articles")
    command.add_argument("--since", type=day, help="Inclusive publication date YYYY-MM-DD; undated articles remain included")
    command.add_argument("--before", type=day, help="Exclusive publication date YYYY-MM-DD; undated articles remain included")


def parser():
    p = argparse.ArgumentParser(description="Discover official QS top university news sources, then collect articles.")
    p.add_argument("--registry", type=Path, help="Custom JSON university registry (default: pinned QS 2027 top 10)")
    p.add_argument("--verbose", action="store_true")
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("universities", help="Print the pinned university list and ranking provenance")
    for command in ("discover", "crawl", "run"):
        sub = commands.add_parser(command, help={"discover": "Discover and validate news sources", "crawl": "Collect articles from previously discovered sources", "run": "Discover sources and collect articles"}[command])
        sub.add_argument("--university", action="append", dest="universities", help="University ID; repeat to select several (default: all)")
        sub.add_argument("--sources", type=Path, default=Path("data/sources.json"))
        sub.add_argument("--delay", type=nonnegative, default=1.0, help="Minimum seconds between requests per origin (default: 1)")
        sub.add_argument("--timeout", type=positive, default=20)
        sub.add_argument("--time-budget", type=positive, help="Best-effort seconds per university per stage; defer waits that cannot fit")
        sub.add_argument("--retries", type=int, choices=range(0, 6), default=2)
        sub.add_argument("--user-agent", default="QSUniversityNewsBot/0.1", help="Set an identifying agent with your contact URL/email")
        if command in {"discover", "run"}:
            sub.add_argument("--discovery-pages", type=positive, default=35, help="Discovery page budget per university")
            sub.add_argument("--depth", type=positive, default=3)
            sub.add_argument("--max-sources", type=positive, default=80)
            sub.add_argument("--discovery-report", type=Path, default=Path("data/discovery-report.json"))
        if command in {"crawl", "run"}:
            sub.add_argument("--db", type=Path, default=Path("data/news.sqlite"))
            sub.add_argument("--max-articles", type=positive, default=20, help="New articles per university per run")
            sub.add_argument("--crawl-pages", type=positive, default=30, help="Feed/listing page budget per university")
            sub.add_argument("--sitemaps", action="store_true", help="Also follow robots-advertised sitemaps (after listings)")
            sub.add_argument("--max-sitemaps", type=positive, default=5)
            add_date_options(sub)
            sub.add_argument("--refresh", action="store_true", help="Re-fetch previously stored articles")
            sub.add_argument("--report", type=Path, default=Path("data/crawl-report.json"))
    export = commands.add_parser("export", help="Export all stored articles as UTF-8 JSONL")
    export.add_argument("--db", type=Path, default=Path("data/news.sqlite"))
    export.add_argument("--output", type=Path, default=Path("data/articles.jsonl"))
    add_date_options(export)
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    try:
        registry = load_registry(args.registry)
        if args.command in {"crawl", "run", "export"}:
            since, before = date_window(month=args.month, since=args.since, before=args.before)
        if args.command == "universities":
            print(json.dumps(registry, indent=2, ensure_ascii=False))
            return 0
        if args.command == "export":
            if not args.db.exists():
                raise ValueError(f"Database does not exist: {args.db}")
            store = Store(args.db)
            try:
                count = store.export(args.output, since=since, before=before, require_date=bool(args.month))
                print(f"Exported {count} articles to {args.output}")
            finally:
                store.close()
            return 0
        selected_ids = set(args.universities or [u["id"] for u in registry["universities"]])
        unknown = selected_ids - {u["id"] for u in registry["universities"]}
        if unknown:
            raise ValueError(f"Unknown university IDs: {', '.join(sorted(unknown))}")
        universities = [u for u in registry["universities"] if u["id"] in selected_ids]
        client_options = dict(delay=args.delay, timeout=args.timeout, retries=args.retries, user_agent=args.user_agent, time_budget=args.time_budget, state={})
        if args.command in {"discover", "run"}:
            previous = json.loads(args.sources.read_text()) if args.sources.exists() else None
            # Preserve unselected universities, plus explicit enable/disable choices.
            if previous and previous["ranking"] != registry["ranking"]:
                raise ValueError("Existing source file uses a different ranking snapshot; use a new --sources path")
            previous_sources = previous["sources"] if previous else []
            sources = [s for s in previous_sources if s["university_id"] not in selected_ids]
            choices = {(s["university_id"], s["url"]): s.get("enabled", True) for s in previous_sources}
            reports = []
            for uni in universities:
                print(f"Discovering {uni['name']}...", flush=True)
                client = Client(uni["domains"], **client_options)
                try:
                    result = discovery(uni, client, max_pages=args.discovery_pages, max_depth=args.depth, max_sources=args.max_sources)
                    for source in result["sources"]:
                        source["enabled"] = choices.get((uni["id"], source["url"]), True)
                    sources.extend(result.pop("sources"))
                    reports.append(result)
                    write_json(args.sources, {"ranking": registry["ranking"], "generated_at": now(), "sources": sources})
                    write_json(args.discovery_report, {"generated_at": now(), "universities": reports})
                    found = sum(s["university_id"] == uni["id"] for s in sources)
                    print(f"  {found} sources recorded; visited {result['pages_visited']} pages", flush=True)
                finally:
                    client.close()
        if args.command in {"crawl", "run"}:
            if not args.sources.exists():
                raise ValueError("No sources file. Run 'discover' first (or use 'run').")
            source_data = json.loads(args.sources.read_text())
            if source_data["ranking"] != registry["ranking"]:
                raise ValueError("Source file ranking differs from registry; rediscover using a matching registry")
            sources = source_data["sources"]
            store = Store(args.db)
            reports = []
            try:
                for uni in universities:
                    if not any(s["university_id"] == uni["id"] and s.get("enabled", True) for s in sources):
                        raise ValueError(f"No enabled sources for {uni['id']}; run discovery for that university first")
                    print(f"Crawling {uni['name']}...", flush=True)
                    client = Client(uni["domains"], **client_options)
                    try:
                        report = crawl(uni, registry["ranking"]["edition"], sources, client, store,
                                       max_articles=args.max_articles, max_pages=args.crawl_pages,
                                       use_sitemaps=args.sitemaps, max_sitemaps=args.max_sitemaps,
                                       since=since, before=before, require_date=bool(args.month), refresh=args.refresh)
                        reports.append(report)
                        write_json(args.report, {"ranking": registry["ranking"], "generated_at": now(), "universities": reports})
                        print(f"  saved {report['articles_saved']}; seen {report['already_seen']}; failures {report['failures']}", flush=True)
                    finally:
                        client.close()
            finally:
                store.close()
            if not any(r["articles_saved"] or r["already_seen"] for r in reports):
                print(f"No articles collected or previously seen; inspect {args.report}", file=sys.stderr)
                return 2
        return 0
    except (ValueError, KeyError, OSError) as exc:
        p.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
