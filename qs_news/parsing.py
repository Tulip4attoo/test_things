"""Pure parsing helpers; no network access."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import gzip
from io import BytesIO
import json
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
import feedparser
import trafilatura


def normalize_url(url: str, base: str = "") -> str | None:
    try:
        p = urlsplit(urljoin(base, url.strip()))
        if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
            return None
        if p.port not in {None, 80, 443}:
            return None
        host = p.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        default_port = (p.scheme.lower(), p.port) in {("http", 80), ("https", 443)}
        netloc = host if p.port is None or default_port else f"{host}:{p.port}"
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
        return urlunsplit((p.scheme.lower(), netloc, p.path or "/", urlencode(query), ""))
    except ValueError:
        return None


def allowed_url(url: str, domains: list[str]) -> bool:
    clean = normalize_url(url)
    if not clean:
        return False
    host = urlsplit(clean).hostname
    return any(host == d or host.endswith("." + d) for d in domains)


def public_news_url(url: str) -> bool:
    """Exclude common personal hosting, proxies, comments, and binary assets."""
    p = urlsplit(url)
    return not (
        re.search(r"(?:libproxy|ezproxy|proxy\d*\.)", p.hostname or "", re.I)
        or re.search(r"/(?:~[^/]*|users?|people|personal|members|students)/", p.path, re.I)
        or "/~" in p.path or re.search(r"(?:comments|replytocom|login|wp-admin)", url, re.I)
        or re.search(r"\.(?:pdf|jpe?g|png|gif|svg|webp|zip|mp[34]|docx?|xlsx?)(?:$|\?)", url, re.I)
    )


def looks_feed(url: str) -> bool:
    path = urlsplit(url).path.rstrip("/")
    if path.endswith("/rss") and path.rsplit("/", 2)[-2] in {"news", "latest-news", "caltech-media"}:
        return True
    if re.fullmatch(r"rss(?:-?feeds)?(?:\.[a-z]{2})?(?:\.html)?", path.rsplit("/", 1)[-1]):
        return False  # Common human-readable feed directories.
    return bool(re.search(r"(?:/feed(?:/|\?|$)|[/.](?:rss|atom|xml|feed)(?:[/.?]|$)|[?&](?:format|output)=rss)", url, re.I))


def looks_listing(url: str) -> bool:
    p = urlsplit(url)
    if not public_news_url(url) or re.search(r"/(?:19|20)\d{2}(?:/|-)", p.path):
        return False
    path = p.path.rstrip("/").lower()
    tail = path.rsplit("/", 1)[-1]
    return (tail in {"news", "news.html", "latest-news", "newsroom", "insight", "insights", "today", "gazette", "rss", "rss-feeds", "rssfeeds", "rssfeeds.html", "press-releases", "eth-news.html", "news-and-events"}
            or (not path and (p.hostname or "").split(".")[0] in {"news", "newsroom"}))


def likely_article(url: str, source: str) -> bool:
    if not public_news_url(url) or looks_feed(url) or looks_listing(url) or url == source:
        return False
    p, s = urlsplit(url), urlsplit(source)
    if p.hostname != s.hostname:
        return False
    if re.search(r"/(?:19|20)\d{2}(?:/|-)\d{1,2}(?:/|-)\S+", p.path):
        return True
    yearly = re.search(r"/(?:19|20)\d{2}/([^/]+)", p.path)
    if yearly and yearly.group(1).lower() not in {"page", "category", "tag", "tags", "author", "feed"}:
        return True  # Some news CMSs put posts at /YEAR/slug, outside /news/.
    if not p.path.startswith(s.path.rstrip("/") + "/"):
        return False
    tail = p.path.rstrip("/").split("/")[-1].removesuffix(".html")
    return len(tail) > 18 and tail.count("-") >= 2 and not re.search(
        r"/(?:tags?|category|collection|topics?|archive|page|search|feed|authors?)(?:/|\?|$)", url, re.I)


def html_links(body: bytes | str, base: str) -> tuple[BeautifulSoup, list[dict]]:
    soup = BeautifulSoup(body, "html.parser")
    links = []
    for tag in soup.select("a[href], link[href]"):
        url = normalize_url(tag.get("href", ""), base)
        if url:
            links.append({"url": url, "label": tag.get_text(" ", strip=True) or tag.get("title", ""),
                          "rel": tag.get("rel", []), "type": tag.get("type", ""),
                          "in_navigation": tag.find_parent(["nav", "header", "footer"]) is not None})
    return soup, links


def parse_feed(body: bytes, base: str) -> tuple[str, list[dict]] | None:
    # Reject HTML/error documents even when a URL looks like a feed.
    try:
        root = ElementTree.fromstring(body)
    except (ElementTree.ParseError, ValueError, DefusedXmlException):
        return None
    if root.tag.rsplit("}", 1)[-1].lower() not in {"rss", "feed", "rdf"}:
        return None
    parsed = feedparser.parse(body)
    entries = []
    for entry in parsed.entries:
        url = normalize_url(entry.get("link", ""), base) if entry.get("link") else None
        if not url:
            continue
        entries.append({"url": url, "title": entry.get("title", ""),
                        "published_at": iso_date(entry.get("published")),
                        "author": entry.get("author"),
                        "summary": BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True)})
    return parsed.feed.get("title", base), entries


def parse_sitemap_records(body: bytes, base: str) -> tuple[str, list[dict]]:
    try:
        if body.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=BytesIO(body)) as compressed:
                body = compressed.read(5_000_001)
            if len(body) > 5_000_000:
                raise ValueError("Decompressed sitemap exceeds 5 MB")
        root = ElementTree.fromstring(body)
    except (ElementTree.ParseError, OSError, EOFError) as exc:
        raise ValueError("Invalid sitemap document") from exc
    kind = root.tag.rsplit("}", 1)[-1]
    if kind not in {"urlset", "sitemapindex"}:
        raise ValueError("Not a sitemap")
    records = []
    for node in root:
        record = {"url": None, "published_at": None, "last_modified": None}
        for child in node:
            local = child.tag.rsplit("}", 1)[-1]
            if local == "loc" and child.text:
                record["url"] = normalize_url(child.text, base)
            elif local == "lastmod":
                record["last_modified"] = iso_date(child.text)
            elif local == "news":
                for field in child:
                    if field.tag.rsplit("}", 1)[-1] == "publication_date":
                        record["published_at"] = iso_date(field.text)
        if record["url"]:
            records.append(record)
    return kind, records


def parse_sitemap(body: bytes, base: str) -> tuple[str, list[str]]:
    kind, records = parse_sitemap_records(body, base)
    return kind, [record["url"] for record in records]


def iso_date(value: str | None) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        try:
            dt = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    if dt.tzinfo is None:
        # Date-only and timezone-free metadata remain timezone-free.
        return dt.isoformat()
    return dt.astimezone(timezone.utc).isoformat()


def extract_article(body: bytes, url: str, hint: dict | None = None) -> dict | None:
    hint = hint or {}
    soup = BeautifulSoup(body, "html.parser")

    def meta(*keys: str) -> str | None:
        for key in keys:
            tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return None

    structured = []
    def walk(value):
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            types = value.get("@type", [])
            types = types if isinstance(types, list) else [types]
            if any(t in {"NewsArticle", "Article", "BlogPosting", "ReportageNewsArticle", "ScholarlyArticle"} for t in types):
                structured.append(value)
            if "@graph" in value:
                walk(value["@graph"])

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            walk(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError):
            continue
    article = structured[0] if structured else {}
    published = iso_date(article.get("datePublished") or meta("article:published_time", "date", "DC.date.issued"))
    time_tag = soup.find("time", datetime=True)
    published = published or (iso_date(time_tag["datetime"]) if time_tag else None) or hint.get("published_at")
    # A headline + body alone can describe a listing. Require article evidence.
    evidence = bool(structured or meta("og:type") == "article" or hint.get("title") or published)
    if not evidence:
        return None
    h1 = soup.find("h1")
    title = article.get("headline") or meta("og:title") or (h1.get_text(" ", strip=True) if h1 else None) or hint.get("title")
    text = trafilatura.extract(body, url=url, include_comments=False, include_tables=False, favor_precision=True)
    if not title or not text or len(text.split()) < 40:
        return None
    author = article.get("author")
    if isinstance(author, dict):
        author = author.get("name")
    elif isinstance(author, list):
        author = ", ".join(str(a.get("name", "")) if isinstance(a, dict) else str(a) for a in author)
    canonical = soup.find("link", rel="canonical")
    canonical_url = normalize_url(canonical.get("href", ""), url) if canonical else url
    lang = soup.html.get("lang") if soup.html else None
    return {"url": url, "canonical_url": canonical_url or url, "title": str(title),
            "published_at": published, "author": author or meta("author") or hint.get("author"),
            "summary": meta("description", "og:description") or hint.get("summary"),
            "language": lang, "text": text}
