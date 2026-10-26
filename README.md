# QS university news crawler

A Python command-line tool that starts with university names and verified domain
roots, discovers their news sources, then collects article metadata and text.
It includes central newsrooms and faculty/department sites.

The bundled list is pinned to **QS World University Rankings 2027**, checked on
2026-10-25. Ranking year means the edition, not the current calendar year.
[QS ranking](https://www.topuniversities.com/qs-top-uni-wur) ·
[QS release](https://www.qs.com/en-us/insights%2Fqs-world-university-rankings)

| QS rank | ID | University | Central newsroom |
| --- | --- | --- | --- |
| 1 | `mit` | Massachusetts Institute of Technology | https://news.mit.edu/ |
| =2 | `imperial` | Imperial College London | https://www.imperial.ac.uk/news/ |
| =2 | `stanford` | Stanford University | https://news.stanford.edu/ |
| 4 | `oxford` | University of Oxford | https://www.ox.ac.uk/news |
| 5 | `harvard` | Harvard University | https://news.harvard.edu/gazette/ |
| 6 | `cambridge` | University of Cambridge | https://www.cam.ac.uk/news |
| 7 | `caltech` | California Institute of Technology | https://www.caltech.edu/about/news |
| =8 | `eth` | ETH Zurich | https://ethz.ch/en/news-and-events/eth-news.html |
| =8 | `ucl` | UCL | https://www.ucl.ac.uk/news/ |
| 10 | `nus` | National University of Singapore | https://news.nus.edu.sg/ |

## Install

Python 3.11 or newer is required. With [uv](https://docs.astral.sh/uv/):

```sh
uv sync
uv run qs-news --help
```

Or with pip:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

The examples below use `python -m qs_news` with the virtual environment activated.
You can also use `uv run qs-news` or the installed `qs-news` command.

## Discover first, then crawl

```sh
# Inspect university names, ranks, domain roots, and seed pages.
python -m qs_news universities

# Discover newsrooms, feeds, and department sources for all ten universities.
python -m qs_news discover

# Review data/sources.json; set enabled=false for any unwanted sources.
# Crawl up to 20 new articles per university into SQLite.
python -m qs_news crawl --max-articles 20

# Export everything collected so far.
python -m qs_news export --output data/articles.jsonl
```

Or run both stages together:

```sh
python -m qs_news run --max-articles 20
```

## Collect a calendar month

```sh
# Collect October 2026 publications from the discovered sources.
python -m qs_news crawl --month 2026-10 --max-articles 1000 --crawl-pages 200 --sitemaps --max-sitemaps 20

# Export only October articles, even if the database contains other months.
python -m qs_news export --month 2026-10 --output data/october-2026/articles.jsonl
```

`--month 2026-10` means publication dates from **2026-10-01 inclusive** to
**2026-11-01 exclusive**. Articles without a known publication date are excluded
and counted as `undated_skipped` during collection. Feed update timestamps are
not treated as publication dates; the article page is checked when its feed has
no publication date. Timestamps with a timezone are normalized to UTC, while
timezone-free dates use their stated calendar day.

For a custom window, use `--since 2026-10-01 --before 2026-11-01`. These options
cannot be combined with `--month`. Unlike strict month mode, a custom window
retains undated articles for compatibility with the original `--since` behavior.

The limit above is 1,000 new articles per university; page, attempt, and sitemap
budgets still apply. Filtering by month does not guarantee complete historical
coverage: feeds can omit older stories, listings may require additional archive
adapters, and access restrictions still apply. Repeated collection skips stored
articles; the filtered export includes matching articles from earlier runs too.

For the concurrent monthly run with a short Vietnamese report and CSV export:

```sh
python examples/crawl_month.py --month 2026-10 --output-dir data/october-2026
```

This uses the existing source registry, imports matching articles from
`data/news.sqlite`, and resumes its own database on later runs. Its defaults are
10 university workers, up to 20 minutes per university, 300 feed/listing pages,
50 sitemap documents, and 10,000 new articles per university. Requests within a
university still follow robots rules and crawl delays. The output directory
contains the source snapshot, SQLite database, full-text JSONL, metadata CSV,
`report.txt`, request logs, and a `crawl-report.json` progress/completion record.
These resource limits and source discovery boundaries can leave coverage partial;
the report does not certify that every October article was found.

Start with one university for a small trial:

```sh
python -m qs_news run --university mit --discovery-pages 12 --max-articles 3
python -m qs_news run --university mit --time-budget 60 --max-articles 3
```

Select multiple universities with repeated `--university` flags. Partial discovery
preserves sources for unselected universities and explicit enable/disable choices.
Use different `--sources`, `--db`, and report paths for independent datasets.

## How discovery works

1. Read the versioned university registry in `qs_news/data/universities.json`.
   University names, official domains, and initial central/faculty newsrooms are
   curated rather than guessed from a name. This makes ownership and ranking
   provenance inspectable and avoids an external search API dependency.
2. Visit newsrooms, official feed directories, university homepages, and
   faculty/department directories. Follow links to additional faculty sites and
   news listings within the university's allowlisted domain roots.
3. Discover RSS/Atom links from HTML `<link>` tags and ordinary feed links.
   The bundled directories include [MIT's feeds](https://news.mit.edu/rss),
   [Harvard's feeds](https://news.harvard.edu/gazette/rss-feeds/),
   [UCL's faculty news channels](https://www.ucl.ac.uk/news/ucl-news-channels),
   [ETH's feeds](https://ethz.ch/en/utils/rss.html), and
   [Cambridge Judge's feeds](https://www.jbs.cam.ac.uk/rss-feeds/), plus
   [Caltech's directory](https://www.caltech.edu/rssfeeds) and
   [all-news Atom feed](https://www.caltech.edu/about/news/rss).
4. Record each source's discovery URL, discovery method, title, validation status,
   and check time in `data/sources.json`. Record robots-advertised sitemaps too.
   Reports list request failures and pages left unexplored when budgets are reached.

`verification=curated_seed` identifies a bundled newsroom/feed seed.
`verification=university_domain_link` means an automatically discovered link under
an official root; it does **not** certify institutional editorial ownership.
University domains can also contain affiliated or personal material. Common
personal/proxy paths are excluded, but the source file remains reviewable.
`status=pending` means discovery found the link but did not validate it within its
page budget; crawl will still try enabled sources. `ok` validates the response and
format, not exhaustive coverage or editorial ownership.

There is no reliable method to guarantee **all** official sources: departments can
use unlinked or separate domains. Increase discovery limits, inspect the report,
and extend the registry with known faculty newsrooms and directories. Add separate
official domain roots only after checking ownership. The tool never follows
third-party search results, social accounts, or externally hosted feeds by default.

## Collection and output

The crawler alternates feeds and HTML listings, follows explicit next-page links,
and can traverse sitemap indexes with `--sitemaps`. Sitemap article filtering uses
discovered newsroom URL patterns. Each candidate article is fetched and its title,
date, author, summary, language, canonical URL, and body text are extracted using
structured metadata and Trafilatura. Listing pages without article evidence and
pages with fewer than 40 extracted words are skipped.

SQLite stores articles, URL aliases, and source associations. Unique university
and canonical-URL keys prevent duplicate rows; original, redirected, and canonical
URLs are remembered so later runs skip known articles. Run with `--refresh` to
update stored articles. Content hashes are saved for downstream change detection;
syndicated copies at different canonical URLs are not merged by text similarity.

`data/articles.jsonl` contains one JSON object per article, with fields:

```text
university_id, university_name, qs_rank, qs_edition, url, title,
published_at, author, summary, language, text, content_hash,
first_seen_at, fetched_at, source_urls
```

Source provenance is also available in `data/sources.json`. Crawl and discovery
reports contain per-university counts and individual HTTP errors. A failed source
does not stop the other universities. The crawl command exits with status 2 if it
neither saves nor encounters any already collected articles; inspect the report
for partial failures even when it exits successfully.

## Limits and operational settings

```sh
python -m qs_news discover --discovery-pages 100 --depth 4 --max-sources 300
python -m qs_news crawl --max-articles 100 --crawl-pages 80 --sitemaps --max-sitemaps 10 --since 2026-10-01
python -m qs_news crawl --university mit --user-agent 'UniversityResearchBot/1.0 (+https://your-domain.example/contact)'
```

- Defaults per university: 35 discovery pages, depth 3, up to 80 recorded sources;
  30 feed/listing pages and 20 successfully saved articles per crawl. At most five
  candidate article attempts per requested article bound failed extraction work.
  Robots requests, redirects, and retries are additional HTTP requests.
- Requests are sequential, with at least one second between requests per origin.
  Robots crawl delays/request rates can make a run substantially slower. Requests
  time out after 20 seconds and retry transient failures up to twice. Retry-After
  waits exceeding 30 seconds are deferred to a future run instead of retried early.
- `--time-budget` sets a best-effort wall-clock limit per university per stage.
  Required waits that cannot fit are deferred. With `run`, discovery and collection
  have separate budgets. An in-flight connection/read timeout can slightly overrun
  the limit. The default has no wall-clock limit beyond page and article budgets.
- Robots rules apply to feeds, listings, articles, and each redirect target. If
  robots.txt cannot be read, the host is skipped. Access restrictions and 403s are
  reported; they are not bypassed. Bodies are capped at 5 MB.
- `--month` requires a known publication date within the selected month.
  `--since` and `--before` filter known dates while retaining undated articles.
  Timezone-free dates remain timezone-free.
- This is bounded recent-news collection, not a complete historical archive.
  Feeds usually contain only recent stories. Unlinked pagination, JavaScript-only
  listings, authenticated content, and external sources require additional adapters.
- Rerun `crawl` through cron or another scheduler for incremental collection.
  Rerun `discover` periodically when you want to expand or recheck source coverage.

## Tests

```sh
python -m unittest discover -s tests -v
```

Tests use local fixtures and mocked HTTP, so they require no network access.

Automated validation: all 21 offline tests pass. Live checks on 2026-10-25 collected
articles from nine universities, including Oxford and Stanford department
sources. Caltech's news listing returned 403, but its official Atom feed and
a linked full article were accessible. UCL's robots.txt remained inaccessible. Generated
artifacts are in `data/sources.json`, `data/articles.jsonl`,
`data/validation-summary.json`, and the discovery/crawl reports. Discovery
includes pending links and category feeds; recorded source URLs are not a count
of verified, distinct news outlets.

The completed October crawl on 2026-10-26 contains **256 articles from nine
universities**, including 13 reused from earlier checks. It used 10 concurrent
university workers with a 20-minute limit per university. Several sources were
blocked or still had work queued when the limits were reached, so this is a
partial month snapshot rather than a complete archive.

Included results: [short Vietnamese report](data/october-2026/report.txt),
[metadata CSV](data/october-2026/articles.csv),
[extracted article text as JSONL](data/october-2026/articles.jsonl),
[request report](data/october-2026/crawl-report.json), and
[download bundle](data/october-2026.zip). Crawl results and SQLite databases are
intentionally tracked in Git; local environments and credentials are ignored.

For a bounded live integration check, `python examples/live_smoke.py` checks four
universities concurrently, with 35 seconds per university per stage (100 seconds
for Stanford to accommodate its 30-second crawl delay). It replaces
the default source/report files and collects up to one article per university.
Requests within each university still obey its robots policy and pacing.
