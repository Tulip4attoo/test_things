from __future__ import annotations

from collections import deque
import heapq
from importlib.resources import files
import json
import logging
from pathlib import Path
import re
from urllib.parse import urlsplit

from defusedxml.common import DefusedXmlException

from .http import BudgetExceeded, FetchError
from .dates import date_window
from .parsing import (allowed_url, extract_article, html_links, iso_date,
                      likely_article, looks_feed, looks_listing, normalize_url,
                      parse_feed, parse_sitemap_records, public_news_url)
from .store import now

LOG = logging.getLogger(__name__)


def load_registry(path=None):
    registry = json.loads(Path(path).read_text() if path else files("qs_news").joinpath("data/universities.json").read_text())
    ids = set()
    for uni in registry["universities"]:
        if uni["id"] in ids or not uni["domains"]:
            raise ValueError("University IDs must be unique and official domains must be supplied")
        ids.add(uni["id"])
        for url in [uni["homepage"], *uni["newsrooms"], *uni["discovery_pages"], *uni["feeds"]]:
            if not allowed_url(url, uni["domains"]):
                raise ValueError(f"Seed outside official domains: {url}")
    return registry


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def article_priority(url, hint, since, before):
    """Prefer the requested dates, without discarding URLs based on their path."""
    published = iso_date(hint.get("published_at"))
    if published:
        return 0
    updated = iso_date(hint.get("last_modified"))
    if updated and since and updated[:10] >= since and (not before or updated[:10] < before):
        return 1  # Scheduling hint only; never substitute this for publication date.
    path = urlsplit(url).path
    match = re.search(r"/((?:19|20)\d{2})(?:/|-)(?:(\d{2})(?:/|-))?", path)
    if not match or not since:
        return 3
    year, month = match.groups()
    if month:
        prefix = f"{year}-{month}"
        within_end = not before or prefix < before[:7] or (prefix == before[:7] and before[8:10] != "01")
        return 0 if since[:7] <= prefix and within_end else 4
    return 2 if since[:4] <= year <= (before or "9999")[:4] else 4


def discovery(university, client, *, max_pages=35, max_depth=3, max_sources=80):
    """Follow news links and faculty directories, preserving evidence per source."""
    sources, frontier, queued, visited = {}, deque(), set(), set()
    seeds = set(university["newsrooms"] + university["feeds"])

    def enqueue(url, depth, parent, reason):
        if url not in queued and depth <= max_depth and allowed_url(url, university["domains"]) and public_news_url(url):
            frontier.append((url, depth, parent, reason))
            queued.add(url)

    def add_source(url, kind, parent, reason, title="", status="pending"):
        key = (kind, url)
        if key not in sources and len(sources) < max_sources:
            sources[key] = {"university_id": university["id"], "university_name": university["name"],
                            "url": url, "kind": kind, "title": title, "enabled": True,
                            "discovered_from": parent, "method": reason, "status": status,
                            "verification": "curated_seed" if url in seeds else "university_domain_link",
                            "checked_at": None, "error": None}
        return sources.get(key)

    for url in university["newsrooms"]:
        add_source(url, "listing", university["homepage"], "curated_seed")
        enqueue(url, 0, university["homepage"], "curated_seed")
    for url in university["discovery_pages"]:
        enqueue(url, 0, university["homepage"], "curated_directory")
    for url in university["feeds"]:
        add_source(url, "feed", university["homepage"], "curated_seed")
        enqueue(url, 0, university["homepage"], "curated_seed")
    enqueue(university["homepage"], 0, university["homepage"], "homepage")

    while frontier and len(visited) < max_pages and not getattr(client, "exhausted", False):
        url, depth, parent, reason = frontier.popleft()
        if url in visited:
            continue
        visited.add(url)
        source = sources.get(("feed", url)) or sources.get(("listing", url))
        try:
            doc = client.get(url)
            feed = parse_feed(doc.body, doc.url) if (source and source["kind"] == "feed") or "html" not in doc.content_type else None
            if feed is not None:
                source = source or add_source(url, "feed", parent, reason)
                if source:
                    source.update(kind="feed", status="ok", title=feed[0], checked_at=now(), final_url=doc.url)
                continue
            if source and source["kind"] == "feed":
                raise FetchError("Advertised feed did not contain RSS/Atom")
            soup, links = html_links(doc.body, doc.url)
            if source:
                source.update(status="ok", title=soup.title.get_text(" ", strip=True) if soup.title else "", checked_at=now(), final_url=doc.url)
            is_directory = reason in {"curated_directory", "faculty_directory", "faculty_site", "homepage"}
            directory_links = []
            for link in links:
                target = link["url"]
                if not allowed_url(target, university["domains"]) or not public_news_url(target):
                    continue
                if link["type"] in {"application/rss+xml", "application/atom+xml", "application/rdf+xml"} or looks_feed(target):
                    if re.search(r"vacanc|studentship|job alert|^upcoming events$|^events$", link["label"], re.I):
                        continue
                    add_source(target, "feed", doc.url, "advertised_feed", link["label"])
                    enqueue(target, depth + 1, doc.url, "advertised_feed")
                elif looks_listing(target):
                    add_source(target, "listing", doc.url, "news_link", link["label"])
                    enqueue(target, depth + 1, doc.url, "news_link")
                elif (is_directory and not link["in_navigation"]
                      and len(urlsplit(target).path.strip("/").split("/")) <= 4
                      and len(urlsplit(target).path.rstrip("/").rsplit("/", 1)[-1]) < 55
                      and not re.search(r"/(?:19|20)\d{2}(?:/|-)", target)
                      and (re.search(r"facult|department|school|division|college|institut|academic", link["label"], re.I)
                           or re.fullmatch(r"(?:faculties|departments|schools|divisions|colleges|institutes|academics)(?:\.html)?", urlsplit(target).path.rstrip("/").rsplit("/", 1)[-1])
                           or (urlsplit(target).hostname != urlsplit(doc.url).hostname
                               and len(urlsplit(target).path.strip("/").split("/")) <= 1))):
                    directory_links.append(target)
            # Breadth-first traversal keeps faculty subtrees from starving seeds.
            for target in directory_links[:40]:
                if target not in queued and depth < max_depth:
                    frontier.append((target, depth + 1, doc.url, "faculty_site"))
                    queued.add(target)
        except (FetchError, ValueError, DefusedXmlException) as exc:
            if source:
                source.update(status="deferred" if isinstance(exc, BudgetExceeded) else "error", checked_at=now(), error=str(exc))

    # Robots-advertised sitemaps are optional crawl inputs; no guessed feed endpoints.
    for url in university["newsrooms"]:
        if getattr(client, "exhausted", False):
            break
        for sitemap in client.sitemap_urls(url):
            sitemap = normalize_url(sitemap, url)
            if sitemap and allowed_url(sitemap, university["domains"]):
                add_source(sitemap, "sitemap", url, "robots_sitemap")
    return {"university_id": university["id"], "pages_visited": len(visited),
            "pages_remaining": len(frontier), "bounded": bool(frontier),
            "source_limit_reached": len(sources) >= max_sources,
            "time_budget_exhausted": getattr(client, "exhausted", False) or getattr(client, "budget_deferred", False),
            "sources": list(sources.values()), "events": client.events}


def crawl(university, edition, sources, client, store, *, max_articles=20,
          max_pages=30, max_sitemaps=5, use_sitemaps=False, since=None, before=None,
          require_date=False, refresh=False):
    since, before = date_window(since=since, before=before)
    stats = {"university_id": university["id"], "source_pages_fetched": 0,
             "article_attempts": 0, "articles_saved": 0, "already_seen": 0,
             "filtered_by_date": 0, "undated_skipped": 0, "not_articles": 0, "failures": 0, "deferred_by_budget": 0,
             "date_filter": {"since": since, "before": before, "require_date": require_date}}
    candidates = []
    scheduled = set()
    attempted = set()
    hints = {}
    feed_sources = {}
    source_queue = deque()
    source_seen = set()
    sitemap_queue = deque()
    def outside_window(value):
        value = iso_date(value)
        return bool(value and ((since and value[:10] < since) or (before and value[:10] >= before)))

    def candidate(url, source, hint=None):
        url = normalize_url(url)
        if not url or not allowed_url(url, university["domains"]) or not public_news_url(url):
            return
        if not refresh and store.seen(university["id"], url, source):
            stats["already_seen"] += 1
            return
        if hint:
            hints[url] = hint
        feed_sources.setdefault(url, set()).add(source)
        if url not in scheduled:
            scheduled.add(url)
            heapq.heappush(candidates, (article_priority(url, hint or {}, since, before), len(scheduled), url, source))

    active = [s for s in sources if s.get("enabled", True) and s["university_id"] == university["id"]]
    # Round-robin feeds and listings so one large feed directory cannot starve faculties.
    feeds = deque(s for s in active if s["kind"] == "feed")
    listings = deque(s for s in active if s["kind"] == "listing")
    while feeds or listings:
        for group in (feeds, listings):
            if group:
                source = group.popleft()
                source_queue.append((source["url"], source["kind"], source["url"]))
    if use_sitemaps:
        sitemap_queue.extend((s["url"], s["url"]) for s in active if s["kind"] == "sitemap")

    maps_seen = set()
    # Alternate discovery and article collection, stopping at bounded successful output.
    while ((source_queue or candidates or sitemap_queue) and stats["articles_saved"] < max_articles
           and not getattr(client, "exhausted", False)):
        if source_queue and stats["source_pages_fetched"] < max_pages:
            url, kind, origin = source_queue.popleft()
            if url in source_seen:
                continue
            source_seen.add(url)
            stats["source_pages_fetched"] += 1
            try:
                doc = client.get(url)
                feed = parse_feed(doc.body, doc.url) if kind == "feed" or "xml" in doc.content_type else None
                if feed is not None:
                    # Prefer newer entries when the feed provides publication metadata.
                    for item in sorted(feed[1], key=lambda i: i.get("published_at") or "", reverse=True):
                        if outside_window(item.get("published_at")):
                            stats["filtered_by_date"] += 1
                        else:
                            candidate(item["url"], origin, item)
                elif kind == "feed":
                    raise FetchError("Expected RSS/Atom feed, received a different document")
                else:
                    _, links = html_links(doc.body, doc.url)
                    for link in links:
                        target = link["url"]
                        if not allowed_url(target, university["domains"]):
                            continue
                        if "next" in link["rel"] or re.fullmatch(r"(?:next(?: page)?|older(?: posts)?|[›»→])", link["label"].strip(), re.I):
                            if urlsplit(target).hostname == urlsplit(doc.url).hostname:
                                source_queue.append((target, "listing", origin))
                        elif likely_article(target, doc.url) or likely_article(target, origin):
                            candidate(target, origin)
            except (FetchError, ValueError, DefusedXmlException) as exc:
                LOG.warning("Source %s: %s", url, exc)
                stats["deferred_by_budget" if isinstance(exc, BudgetExceeded) else "failures"] += 1
        elif not candidates and sitemap_queue and len(maps_seen) < max_sitemaps:
            url, origin = sitemap_queue.popleft()
            if url in maps_seen:
                continue
            maps_seen.add(url)
            try:
                doc = client.get(url)
                kind, records = parse_sitemap_records(doc.body, doc.url)
                for item in sorted(records, key=lambda r: r.get("last_modified") or "", reverse=True):
                    target = item["url"]
                    if not allowed_url(target, university["domains"]):
                        continue
                    if kind == "sitemapindex":
                        sitemap_queue.append((target, origin))
                    elif any(likely_article(target, s["url"]) for s in active if s["kind"] == "listing"):
                        if outside_window(item.get("published_at")):
                            stats["filtered_by_date"] += 1
                        else:
                            candidate(target, origin, item)
            except (FetchError, ValueError, DefusedXmlException) as exc:
                LOG.warning("Sitemap %s: %s", url, exc)
                stats["deferred_by_budget" if isinstance(exc, BudgetExceeded) else "failures"] += 1
        elif not candidates:
            break

        # Bound attempts independently of successes to handle archives/failed extraction.
        for _ in range(min(3, len(candidates))):
            if stats["articles_saved"] >= max_articles or stats["article_attempts"] >= max_articles * 5:
                break
            _, _, url, source = heapq.heappop(candidates)
            if url in attempted:
                continue
            attempted.add(url)
            hint = hints.get(url, {})
            if outside_window(hint.get("published_at")):
                stats["filtered_by_date"] += 1
                continue
            stats["article_attempts"] += 1
            try:
                doc = client.get(url)
                article = extract_article(doc.body, doc.url, hint)
                if not article:
                    stats["not_articles"] += 1
                    continue
                if require_date and not iso_date(article.get("published_at")):
                    stats["undated_skipped"] += 1
                    continue
                if outside_window(article.get("published_at")):
                    stats["filtered_by_date"] += 1
                    continue
                if not allowed_url(article["canonical_url"], university["domains"]) or not public_news_url(article["canonical_url"]):
                    article["canonical_url"] = doc.url
                existing = store.seen(university["id"], article["canonical_url"], source)
                store.save(university, edition, source, article, url)
                for provenance in feed_sources.get(url, set()):
                    store.seen(university["id"], url, provenance)
                if existing and not refresh:
                    stats["already_seen"] += 1
                else:
                    stats["articles_saved"] += 1
            except (FetchError, ValueError, DefusedXmlException) as exc:
                LOG.warning("Article %s: %s", url, exc)
                stats["deferred_by_budget" if isinstance(exc, BudgetExceeded) else "failures"] += 1
        if stats["article_attempts"] >= max_articles * 5:
            break
    stats.update(events=client.events, http_requests=client.requests,
                 time_budget_exhausted=getattr(client, "exhausted", False) or getattr(client, "budget_deferred", False),
                 remaining_candidates=len(candidates), remaining_source_pages=len(source_queue),
                 remaining_sitemaps=len(sitemap_queue), sitemaps_fetched=len(maps_seen))
    return stats
