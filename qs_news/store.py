from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from .dates import date_window


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA foreign_keys = ON;
          CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY, university_id TEXT NOT NULL,
            university_name TEXT NOT NULL, qs_rank INTEGER NOT NULL,
            qs_edition INTEGER NOT NULL, url TEXT NOT NULL,
            title TEXT NOT NULL, published_at TEXT, author TEXT, summary TEXT,
            language TEXT, text TEXT NOT NULL, content_hash TEXT NOT NULL,
            first_seen_at TEXT NOT NULL, fetched_at TEXT NOT NULL,
            UNIQUE(university_id, url)
          );
          CREATE TABLE IF NOT EXISTS article_urls (
            university_id TEXT NOT NULL, url TEXT NOT NULL,
            article_id INTEGER NOT NULL REFERENCES articles(id),
            PRIMARY KEY(university_id, url)
          );
          CREATE TABLE IF NOT EXISTS article_sources (
            article_id INTEGER NOT NULL REFERENCES articles(id),
            source_url TEXT NOT NULL, PRIMARY KEY(article_id, source_url)
          );
        """)

    def close(self):
        self.db.close()

    def seen(self, university_id, url, source_url=None):
        row = self.db.execute("SELECT article_id FROM article_urls WHERE university_id=? AND url=?", (university_id, url)).fetchone()
        if row and source_url:
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO article_sources VALUES (?, ?)", (row[0], source_url))
        return bool(row)

    def save(self, university, edition, source_url, article, requested_url):
        canonical = article["canonical_url"]
        timestamp = now()
        digest = hashlib.sha256(article["text"].encode()).hexdigest()
        with self.db:
            self.db.execute("""
              INSERT INTO articles (university_id, university_name, qs_rank, qs_edition, url,
                title, published_at, author, summary, language, text, content_hash, first_seen_at, fetched_at)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
              ON CONFLICT(university_id, url) DO UPDATE SET
                university_name=excluded.university_name, qs_rank=excluded.qs_rank,
                qs_edition=excluded.qs_edition, title=excluded.title,
                published_at=excluded.published_at, author=excluded.author,
                summary=excluded.summary, language=excluded.language,
                text=excluded.text, content_hash=excluded.content_hash,
                fetched_at=excluded.fetched_at
            """, (university["id"], university["name"], university["rank"], edition, canonical,
                  article["title"], article["published_at"], article["author"], article["summary"],
                  article["language"], article["text"], digest, timestamp, timestamp))
            article_id = self.db.execute("SELECT id FROM articles WHERE university_id=? AND url=?", (university["id"], canonical)).fetchone()[0]
            for url in {canonical, article["url"], requested_url}:
                self.db.execute("INSERT OR REPLACE INTO article_urls VALUES (?, ?, ?)", (university["id"], url, article_id))
            self.db.execute("INSERT OR IGNORE INTO article_sources VALUES (?, ?)", (article_id, source_url))

    def export(self, path, *, since=None, before=None, require_date=False):
        since, before = date_window(since=since, before=before)
        clauses, parameters = [], []
        if require_date:
            clauses.append("published_at IS NOT NULL")
        for bound, operator in ((since, ">="), (before, "<")):
            if bound:
                clauses.append(f"(published_at IS NULL OR substr(published_at, 1, 10) {operator} ?)")
                parameters.append(bound)
        query = "SELECT * FROM articles"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY university_id, published_at DESC, id"
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with path.open("w", encoding="utf-8") as f:
            for row in self.db.execute(query, parameters):
                article = dict(row)
                article["source_urls"] = [r[0] for r in self.db.execute("SELECT source_url FROM article_sources WHERE article_id=? ORDER BY source_url", (row["id"],))]
                f.write(json.dumps(article, ensure_ascii=False) + "\n")
                count += 1
        return count
