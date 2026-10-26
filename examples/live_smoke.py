"""Bounded live integration check across universities; run from the project root.

Writes the same reviewable sources and reports as the CLI. Separate universities
are checked concurrently; requests within each university remain sequential.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from qs_news.http import Client
from qs_news.pipeline import crawl, discovery, load_registry, write_json
from qs_news.store import Store, now


def check_university(uni, ranking):
    budget = 100 if uni["id"] == "stanford" else 35
    state = {}
    client = Client(uni["domains"], timeout=10, retries=0, time_budget=budget, state=state)
    try:
        discovered = discovery(uni, client, max_pages=10, max_sources=150)
    finally:
        client.close()
    sources = discovered.pop("sources")
    client = Client(uni["domains"], timeout=10, retries=0, time_budget=budget, state=state)
    store = Store("data/news.sqlite")
    try:
        collected = crawl(uni, ranking["edition"], sources, client, store,
                          max_articles=1, max_pages=8)
    finally:
        store.close()
        client.close()
    return sources, discovered, collected


def main():
    registry = load_registry()
    # Initialize the schema before the worker connections open.
    Store("data/news.sqlite").close()
    all_sources, discovery_reports, crawl_reports = [], [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(check_university, uni, registry["ranking"]): uni["id"]
                   for uni in registry["universities"]}
        for future in as_completed(futures):
            sources, discovered, collected = future.result()
            all_sources.extend(sources)
            discovery_reports.append(discovered)
            crawl_reports.append(collected)
            print(f"{futures[future]}: sources={len(sources)}, saved={collected['articles_saved']}, seen={collected['already_seen']}, failures={collected['failures']}", flush=True)
            write_json("data/sources.json", {"ranking": registry["ranking"], "generated_at": now(), "sources": all_sources})
            write_json("data/discovery-report.json", {"generated_at": now(), "universities": discovery_reports})
            write_json("data/crawl-report.json", {"ranking": registry["ranking"], "generated_at": now(), "universities": crawl_reports})
    store = Store("data/news.sqlite")
    try:
        store.export("data/articles.jsonl")
    finally:
        store.close()


if __name__ == "__main__":
    main()
