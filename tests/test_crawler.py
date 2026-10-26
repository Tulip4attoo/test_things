import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from qs_news.dates import date_window
from qs_news.http import BudgetExceeded, Client, Document, FetchError, RobotsDenied
from qs_news.parsing import (allowed_url, extract_article, iso_date, looks_feed,
                             normalize_url, parse_feed, parse_sitemap, parse_sitemap_records)
from qs_news.pipeline import crawl, discovery, load_registry
from qs_news.store import Store


UNI = {"id": "example", "rank": 2, "name": "Example University", "domains": ["example.edu"],
       "homepage": "https://example.edu/", "newsrooms": ["https://example.edu/news"],
       "discovery_pages": ["https://example.edu/schools"], "feeds": []}
ARTICLE_URL = "https://example.edu/news/2026/10/real-research-story"
WORDS = "Scientists at the university published results from their research. The findings explain how the new method improves reliable measurement in clinical studies. "
ARTICLE = f'''<html lang="en"><head><title>Study</title>
<link rel="canonical" href="{ARTICLE_URL}?utm_source=feed"/>
<script type="application/ld+json">{{"@graph":[{{"@type":"NewsArticle","headline":"A research breakthrough","datePublished":"2026-10-24T12:00:00Z","author":{{"name":"A Researcher"}}}}]}}</script>
</head><body><article><h1>A research breakthrough</h1><p>{WORDS * 8}</p></article></body></html>'''.encode()
RSS = f'''<?xml version="1.0"?><rss version="2.0"><channel><title>University News</title>
<item><title>A research breakthrough</title><link>{ARTICLE_URL}</link><pubDate>Sat, 24 Oct 2026 12:00:00 GMT</pubDate></item>
</channel></rss>'''.encode()
ATOM = f'''<feed xmlns="http://www.w3.org/2005/Atom"><title>Faculty News</title><entry><title>Research</title>
<link href="{ARTICLE_URL}"/><published>2026-10-24T12:00:00Z</published><updated>2026-11-02T12:00:00Z</updated></entry></feed>'''.encode()


class FakeClient:
    def __init__(self, docs):
        self.docs = docs
        self.events = []
        self.requests = 0
        self.visited = []

    def get(self, url):
        self.requests += 1
        self.visited.append(url)
        if url not in self.docs:
            raise FetchError(f"Missing fixture: {url}")
        body = self.docs[url]
        return Document(url, body, "application/xml" if body.startswith(b"<?xml") else "text/html")

    def sitemap_urls(self, url):
        return []


class ParsingTests(unittest.TestCase):
    def test_registry_is_pinned_and_supports_ties(self):
        data = load_registry()
        self.assertEqual(data["ranking"]["edition"], 2027)
        self.assertEqual(len(data["universities"]), 10)
        self.assertEqual([u["rank"] for u in data["universities"]], [1, 2, 2, 4, 5, 6, 7, 8, 8, 10])

    def test_url_scope_and_tracking(self):
        self.assertTrue(allowed_url("https://news.example.edu/post", ["example.edu"]))
        for url in ["https://example.edu.evil.org/", "https://evilexample.edu/", "https://example.edu@evil.org/", "file:///etc/passwd"]:
            self.assertFalse(allowed_url(url, ["example.edu"]))
        self.assertEqual(normalize_url("/news?page=2&utm_source=x#top", "https://example.edu/"), "https://example.edu/news?page=2")
        self.assertEqual(normalize_url("https://Example.edu:443/news"), "https://example.edu/news")

    def test_rss_atom_and_malformed_feed(self):
        for body in [RSS, ATOM]:
            result = parse_feed(body, "https://example.edu/feed")
            self.assertEqual(result[1][0]["url"], ARTICLE_URL)
            self.assertEqual(result[1][0]["published_at"], "2026-10-24T12:00:00+00:00")
        self.assertIsNone(parse_feed(b"<html><h1>Forbidden</h1></html>", "https://example.edu/feed"))
        self.assertIsNone(parse_feed(b'<!DOCTYPE rss [<!ENTITY a SYSTEM "file:///etc/passwd">]><rss>&a;</rss>', "https://example.edu/feed"))
        self.assertFalse(looks_feed("https://example.edu/rss"))
        self.assertFalse(looks_feed("https://example.edu/utils/rss.fr.html"))
        self.assertTrue(looks_feed("https://example.edu/news/_jcr_content.feed.html"))
        updated_only = ATOM.replace(b"<published>2026-10-24T12:00:00Z</published>", b"")
        self.assertIsNone(parse_feed(updated_only, "https://example.edu/feed")[1][0]["published_at"])

    def test_month_boundaries_and_invalid_ranges(self):
        self.assertEqual(date_window(month="2026-10"), ("2026-10-01", "2026-11-01"))
        self.assertEqual(date_window(month="2026-12"), ("2026-12-01", "2027-01-01"))
        self.assertEqual(date_window(month="2024-02"), ("2024-02-01", "2024-03-01"))
        for month in ["2026-13", "2026-00", "2026-1", "October 2026"]:
            with self.assertRaises(ValueError):
                date_window(month=month)
        with self.assertRaises(ValueError):
            date_window(month="2026-10", since="2026-10-01")
        with self.assertRaises(ValueError):
            date_window(since="2026-11-01", before="2026-10-01")

    def test_article_extraction_and_listing_rejection(self):
        article = extract_article(ARTICLE, ARTICLE_URL)
        self.assertEqual(article["author"], "A Researcher")
        self.assertEqual(article["canonical_url"], ARTICLE_URL)
        self.assertEqual(article["published_at"], "2026-10-24T12:00:00+00:00")
        self.assertGreater(len(article["text"]), 100)
        self.assertIsNone(extract_article(f"<html><h1>All News</h1><main>{WORDS * 8}</main></html>".encode(), "https://example.edu/news"))

    def test_dates_and_nested_sitemaps(self):
        self.assertIsNone(iso_date("not a date"))
        kind, urls = parse_sitemap(b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://example.edu/news.xml</loc></sitemap></sitemapindex>', "https://example.edu/sitemap.xml")
        self.assertEqual(kind, "sitemapindex")
        self.assertEqual(urls, ["https://example.edu/news.xml"])
        compressed = gzip.compress(b'<urlset><url><loc>https://example.edu/news/story</loc></url></urlset>')
        self.assertEqual(parse_sitemap(compressed, "https://example.edu/sitemap.xml.gz")[1], ["https://example.edu/news/story"])
        self.assertIsNone(iso_date(123))
        with self.assertRaises(ValueError):
            parse_sitemap(b"not XML", "https://example.edu/sitemap.xml")
        with self.assertRaises(ValueError):
            parse_sitemap(b"\x1f\x8bbad gzip", "https://example.edu/sitemap.xml.gz")

    def test_sitemap_preserves_distinct_publication_and_update_dates(self):
        xml = b'''<urlset xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"><url>
        <loc>https://example.edu/2025/older-news-story</loc><lastmod>2026-10-24</lastmod>
        <news:news><news:publication_date>2025-10-01</news:publication_date></news:news>
        </url><url><loc>https://example.edu/2026/undated-news-story</loc><lastmod>2026-10-24</lastmod></url></urlset>'''
        _, records = parse_sitemap_records(xml, "https://example.edu/sitemap.xml")
        self.assertTrue(records[0]["published_at"].startswith("2025-10-01"))
        self.assertTrue(records[0]["last_modified"].startswith("2026-10-24"))
        self.assertIsNone(records[1]["published_at"])


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "news.sqlite")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_discovers_department_news_and_feed_with_provenance(self):
        client = FakeClient({
            UNI["homepage"]: b'<a href="/schools">Schools</a>',
            "https://example.edu/news": b'<link rel="alternate" type="application/rss+xml" href="/feed"/><a href="/rssfeeds">RSS Feeds</a>',
            "https://example.edu/rssfeeds": b'<a href="/about/news/rss">all news</a>',
            "https://example.edu/about/news/rss": ATOM,
            "https://example.edu/schools": b'<a href="https://engineering.example.edu/">School of Engineering</a>',
            "https://engineering.example.edu/": b'<a href="/news">News</a>',
            "https://engineering.example.edu/news": b'<h1>Engineering News</h1>',
            "https://example.edu/feed": RSS,
        })
        result = discovery(UNI, client, max_pages=15)
        sources = {s["url"]: s for s in result["sources"]}
        self.assertEqual(sources["https://example.edu/feed"]["status"], "ok")
        self.assertEqual(sources["https://example.edu/about/news/rss"]["kind"], "feed")
        self.assertEqual(sources["https://example.edu/about/news/rss"]["status"], "ok")
        self.assertEqual(sources["https://example.edu/about/news/rss"]["discovered_from"], "https://example.edu/rssfeeds")
        department = sources["https://engineering.example.edu/news"]
        self.assertEqual(department["discovered_from"], "https://engineering.example.edu/")
        self.assertEqual(department["status"], "ok")

    def test_feed_collection_is_incremental_and_exports_provenance(self):
        feed_url = "https://example.edu/feed"
        sources = [{"university_id": "example", "url": feed_url, "kind": "feed"}]
        client = FakeClient({feed_url: RSS, ARTICLE_URL: ARTICLE})
        result = crawl(UNI, 2027, sources, client, self.store, max_articles=1)
        self.assertEqual(result["articles_saved"], 1)
        second = FakeClient({feed_url: RSS})
        result = crawl(UNI, 2027, sources, second, self.store, max_articles=1)
        self.assertEqual(result["articles_saved"], 0)
        self.assertEqual(result["already_seen"], 1)
        self.assertNotIn(ARTICLE_URL, second.visited)
        output = Path(self.temp.name) / "articles.jsonl"
        self.assertEqual(self.store.export(output), 1)
        article = json.loads(output.read_text())
        self.assertEqual(article["qs_edition"], 2027)
        self.assertEqual(article["source_urls"], [feed_url])

    def test_listing_pagination_and_since_filter(self):
        first, second = "https://example.edu/news", "https://example.edu/news?page=2"
        sources = [{"university_id": "example", "url": first, "kind": "listing"}]
        client = FakeClient({first: f'<a rel="next" href="{second}">Next</a>'.encode(),
                             second: f'<a href="{ARTICLE_URL}">Research</a>'.encode(), ARTICLE_URL: ARTICLE})
        result = crawl(UNI, 2027, sources, client, self.store, max_articles=1, since="2026-10-25")
        self.assertEqual(result["articles_saved"], 0)
        self.assertEqual(result["filtered_by_date"], 1)
        self.assertIn(second, client.visited)

    def test_month_collection_and_export_exclude_other_months_and_undated(self):
        feed_url = "https://example.edu/feed"
        dates = ["2026-09-30T23:59:59Z", "2026-10-01T00:00:00Z", "2026-10-31T23:59:59Z", "2026-11-01T00:00:00Z", None]
        docs, entries, articles = {}, [], []
        for index, published in enumerate(dates):
            url = ARTICLE_URL + f"-{index}"
            body = ARTICLE.replace(ARTICLE_URL.encode(), url.encode())
            if published:
                body = body.replace(b"2026-10-24T12:00:00Z", published.encode())
            else:
                body = body.replace(b',"datePublished":"2026-10-24T12:00:00Z"', b"")
            docs[url] = body
            timestamp = f"<published>{published}</published>" if published else ""
            entries.append(f'<entry><title>Research {index}</title><link href="{url}"/>{timestamp}</entry>')
            articles.append(extract_article(body, url))
        docs[feed_url] = ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>News</title>' + ''.join(entries) + '</feed>').encode()
        client = FakeClient(docs)
        since, before = date_window(month="2026-10")
        result = crawl(UNI, 2027, [{"university_id": "example", "url": feed_url, "kind": "feed"}], client,
                       self.store, max_articles=10, since=since, before=before, require_date=True)
        self.assertEqual(result["articles_saved"], 2)
        self.assertEqual(result["filtered_by_date"], 2)
        self.assertEqual(result["undated_skipped"], 1)
        self.assertNotIn(ARTICLE_URL + "-0", client.visited)
        self.assertNotIn(ARTICLE_URL + "-3", client.visited)
        # Export must also filter a shared database that contains other months.
        for article in articles:
            self.store.save(UNI, 2027, feed_url, article, article["url"])
        output = Path(self.temp.name) / "october.jsonl"
        self.assertEqual(self.store.export(output, since=since, before=before, require_date=True), 2)
        exported = [json.loads(line) for line in output.read_text().splitlines()]
        self.assertEqual({a["published_at"][:10] for a in exported}, {"2026-10-01", "2026-10-31"})

    def test_article_metadata_is_checked_when_feed_has_no_publication_date(self):
        feed_url = "https://example.edu/feed"
        updated_only = ATOM.replace(b"<published>2026-10-24T12:00:00Z</published>", b"")
        client = FakeClient({feed_url: updated_only, ARTICLE_URL: ARTICLE})
        # Mark the fixture as XML, matching an Atom server response.
        client.docs[feed_url] = b'<?xml version="1.0"?>' + updated_only
        since, before = date_window(month="2026-10")
        result = crawl(UNI, 2027, [{"university_id": "example", "url": feed_url, "kind": "feed"}], client,
                       self.store, max_articles=1, since=since, before=before, require_date=True)
        self.assertEqual(result["articles_saved"], 1)

    def test_sitemap_index_and_failure_do_not_stop_other_sources(self):
        listing, index, child = "https://example.edu/news", "https://example.edu/sitemap.xml", "https://example.edu/news.xml"
        sources = [{"university_id": "example", "url": listing, "kind": "listing"},
                   {"university_id": "example", "url": index, "kind": "sitemap"}]
        client = FakeClient({index: f'<sitemapindex><sitemap><loc>{child}</loc></sitemap></sitemapindex>'.encode(),
                             child: f'<urlset><url><loc>{ARTICLE_URL}</loc></url></urlset>'.encode(), ARTICLE_URL: ARTICLE})
        result = crawl(UNI, 2027, sources, client, self.store, max_articles=1, use_sitemaps=True)
        self.assertEqual(result["articles_saved"], 1)
        self.assertEqual(result["sitemaps_fetched"], 2)
        self.assertEqual(result["failures"], 1)

    def test_month_sitemap_prefers_target_month_over_old_posts(self):
        listing, sitemap = "https://example.edu/news", "https://example.edu/sitemap.xml"
        older_url = "https://example.edu/2025/old-research-story"
        current_url = "https://example.edu/2026/10/new-research-story"
        client = FakeClient({listing: b"<h1>News</h1>",
                             sitemap: f'<urlset><url><loc>{older_url}</loc></url><url><loc>{current_url}</loc></url></urlset>'.encode(),
                             current_url: ARTICLE.replace(ARTICLE_URL.encode(), current_url.encode())})
        sources = [{"university_id": "example", "url": listing, "kind": "listing"},
                   {"university_id": "example", "url": sitemap, "kind": "sitemap"}]
        result = crawl(UNI, 2027, sources, client, self.store, max_articles=1,
                       since="2026-10-01", before="2026-11-01", require_date=True, use_sitemaps=True)
        self.assertEqual(result["articles_saved"], 1)
        self.assertNotIn(older_url, client.visited)

    def test_article_budget_and_disabled_sources(self):
        sources = [{"university_id": "example", "url": "https://example.edu/disabled", "kind": "feed", "enabled": False},
                   {"university_id": "example", "url": "https://example.edu/feed", "kind": "feed"}]
        client = FakeClient({"https://example.edu/feed": RSS, ARTICLE_URL: ARTICLE})
        result = crawl(UNI, 2027, sources, client, self.store, max_articles=1)
        self.assertEqual(result["articles_saved"], 1)
        self.assertNotIn("https://example.edu/disabled", client.visited)


def response(status, body=b"", headers=None):
    result = Mock()
    result.status_code = status
    result.headers = headers or {}
    result.is_redirect = status in {301, 302, 303, 307, 308}
    result.is_permanent_redirect = status in {301, 308}
    result.iter_content.return_value = iter([body])
    return result


class HttpTests(unittest.TestCase):
    def test_pacing_and_robots_are_shared_between_stages(self):
        state = {}
        discover_client = Client(["example.edu"], delay=0, retries=0, state=state)
        discover_client.session.get = Mock(side_effect=[response(200, b"User-agent: *\nCrawl-delay: 120\n")])
        discover_client._policy("https://example.edu/news")
        crawl_client = Client(["example.edu"], delay=0, retries=0, time_budget=1, state=state)
        crawl_client.session.get = Mock()
        with self.assertRaises(BudgetExceeded):
            crawl_client.get("https://example.edu/news")
        crawl_client.session.get.assert_not_called()

    def test_time_budget_defers_long_robots_wait(self):
        client = Client(["example.edu"], delay=0, retries=0, time_budget=1)
        client.session.get = Mock(return_value=response(200, b"User-agent: *\nCrawl-delay: 120\n"))
        with self.assertRaises(BudgetExceeded):
            client.get("https://example.edu/news")
        self.assertEqual(client.session.get.call_count, 1)
        self.assertTrue(client.budget_deferred)
        self.assertEqual(client.events[-1]["status"], "deferred")

    def test_denies_robots_and_external_redirect_before_fetch(self):
        client = Client(["example.edu"], delay=0, retries=0)
        client.session.get = Mock(side_effect=[response(200, b"User-agent: *\nDisallow: /private\n"),
                                                response(302, headers={"Location": "https://evil.org/news"})])
        with self.assertRaises(RobotsDenied):
            client.get("https://example.edu/private/data")
        with self.assertRaises(FetchError):
            client.get("https://example.edu/news")
        self.assertEqual(client.session.get.call_count, 2)

    def test_redirect_target_gets_its_own_robots_check(self):
        client = Client(["example.edu"], delay=0, retries=0)
        client.session.get = Mock(side_effect=[response(404), response(302, headers={"Location": "https://faculty.example.edu/news"}),
                                                response(200, b"User-agent: *\nDisallow: /\n")])
        with self.assertRaises(RobotsDenied):
            client.get("https://example.edu/news")
        self.assertEqual(client.session.get.call_count, 3)

    def test_robot_unavailable_fails_closed(self):
        client = Client(["example.edu"], delay=0, retries=0)
        client.session.get = Mock(return_value=response(503))
        with self.assertRaises(RobotsDenied):
            client.get("https://example.edu/news")
        self.assertEqual(client.session.get.call_count, 1)
        self.assertEqual(client.events[0]["status"], "robots_unavailable")

    @patch("qs_news.http.time.sleep")
    def test_retry_after_and_response_limit(self, sleep):
        client = Client(["example.edu"], delay=0, retries=1, max_bytes=4)
        client.session.get = Mock(side_effect=[response(404), response(429, headers={"Retry-After": "2"}), response(200, b"too long")])
        with self.assertRaises(FetchError):
            client.get("https://example.edu/news")
        sleep.assert_any_call(2.0)


if __name__ == "__main__":
    unittest.main()
