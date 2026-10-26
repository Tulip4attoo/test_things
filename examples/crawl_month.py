"""Collect a calendar month across the pinned universities and write a short report.

Universities run concurrently; each client's requests remain sequential and paced.
Run from the project root: python examples/crawl_month.py --month 2026-10
"""
from __future__ import annotations

import argparse
import calendar
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import csv
import json
import logging
from pathlib import Path
import sqlite3
import time

from qs_news.dates import date_window
from qs_news.http import Client
from qs_news.pipeline import crawl, load_registry, write_json
from qs_news.store import Store, now


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", default="2026-10")
    parser.add_argument("--sources", type=Path, default=Path("data/sources.json"))
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--seconds-per-university", type=int, default=1200)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--crawl-pages", type=int, default=300)
    parser.add_argument("--max-sitemaps", type=int, default=50)
    args = parser.parse_args()
    if min(args.seconds_per_university, args.workers, args.crawl_pages, args.max_sitemaps) < 1:
        parser.error("All resource limits must be positive")
    since, before = date_window(month=args.month)
    registry = load_registry()
    source_data = json.loads(args.sources.read_text())
    if registry["ranking"] != source_data["ranking"]:
        parser.error("Source registry and ranking snapshot differ")
    output = args.output_dir or Path("data") / f"{calendar.month_name[int(args.month[-2:])].lower()}-{args.month[:4]}"
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "sources.json", source_data)
    logging.basicConfig(filename=output / "crawl.log", level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    db_path = output / "news.sqlite"
    store = Store(db_path)
    mismatched = store.db.execute("SELECT count(*) FROM articles WHERE published_at IS NULL OR substr(published_at,1,10)<? OR substr(published_at,1,10)>=?", (since, before)).fetchone()[0]
    if mismatched:
        store.close()
        parser.error("Output database contains articles outside this month; choose a different --output-dir")
    # Reuse matching articles and original URL aliases from previous collections.
    shared_db = Path("data/news.sqlite")
    if shared_db.exists() and shared_db.resolve() != db_path.resolve():
        store.db.execute("ATTACH DATABASE ? AS previous", (str(shared_db.resolve()),))
        with store.db:
            columns = [row[1] for row in store.db.execute("PRAGMA table_info(articles)") if row[1] != "id"]
            names = ",".join(columns)
            store.db.execute(f"INSERT OR IGNORE INTO articles ({names}) SELECT {names} FROM previous.articles WHERE published_at IS NOT NULL AND substr(published_at,1,10)>=? AND substr(published_at,1,10)<?", (since, before))
            store.db.execute("""INSERT OR IGNORE INTO article_urls
                SELECT old.university_id, old.url, current.id FROM previous.article_urls old
                JOIN previous.articles original ON original.id=old.article_id
                JOIN articles current ON current.university_id=original.university_id AND current.url=original.url""")
            store.db.execute("""INSERT OR IGNORE INTO article_sources
                SELECT current.id, old.source_url FROM previous.article_sources old
                JOIN previous.articles original ON original.id=old.article_id
                JOIN articles current ON current.university_id=original.university_id AND current.url=original.url""")
        store.db.execute("DETACH DATABASE previous")
    initial = dict(store.db.execute("SELECT university_id,count(*) FROM articles GROUP BY university_id"))
    store.close()
    started_at = now()
    started = time.monotonic()
    clients, reports = {}, {}
    options = {"since": since, "before": before, "require_date": True,
               "max_articles": 10000, "max_pages": args.crawl_pages,
               "max_sitemaps": args.max_sitemaps, "use_sitemaps": True}

    def worker(uni):
        client = Client(uni["domains"], timeout=20, retries=1,
                        time_budget=args.seconds_per_university)
        clients[uni["id"]] = client
        database = Store(db_path)
        worker_start = time.monotonic()
        try:
            result = crawl(uni, registry["ranking"]["edition"], source_data["sources"],
                           client, database, **options)
            result["elapsed_seconds"] = round(time.monotonic() - worker_start, 1)
            result["status"] = "finished"
            return result
        except Exception as exc:
            logging.exception("University crawl failed: %s", uni["id"])
            return {"university_id": uni["id"], "status": "error", "error": str(exc),
                    "events": client.events, "http_requests": client.requests,
                    "elapsed_seconds": round(time.monotonic() - worker_start, 1)}
        finally:
            database.close()
            client.close()

    def snapshot(state):
        database = sqlite3.connect(db_path)
        counts = dict(database.execute("SELECT university_id,count(*) FROM articles GROUP BY university_id"))
        database.close()
        progress = {"state": state, "started_at": started_at, "updated_at": now(),
                    "month": args.month, "date_window": {"since": since, "before": before},
                    "ranking": registry["ranking"], "initial_articles_by_university": initial,
                    "articles_by_university": counts, "total_articles": sum(counts.values()),
                    "elapsed_seconds": round(time.monotonic() - started, 1),
                    "limits": {"seconds_per_university": args.seconds_per_university, **options},
                    "http_requests_by_university": {uid: client.requests for uid, client in list(clients.items())},
                    "recent_requests": {uid: client.events[-3:] for uid, client in list(clients.items())},
                    "completed_universities": list(reports), "universities": list(reports.values())}
        write_json(output / "crawl-report.json", progress)
        return progress

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending = {pool.submit(worker, uni): uni["id"] for uni in registry["universities"]}
        last_print = 0
        while pending:
            done, _ = wait(pending, timeout=10, return_when=FIRST_COMPLETED)
            for future in done:
                uid = pending.pop(future)
                reports[uid] = future.result()
                print(f"Finished {uid}: {reports[uid].get('articles_saved', 0)} new articles, {reports[uid].get('failures', 0)} failed attempts", flush=True)
            progress = snapshot("running")
            if done or time.monotonic() - last_print >= 30:
                print(f"Progress: {progress['total_articles']} articles; completed {len(reports)}/10; {progress['elapsed_seconds']:.0f}s", flush=True)
                last_print = time.monotonic()

    database = Store(db_path)
    database.export(output / "articles.jsonl", since=since, before=before, require_date=True)
    with (output / "articles.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["university_id", "university_name", "published_at", "title", "url", "language", "author", "summary"])
        for row in database.db.execute("SELECT university_id,university_name,published_at,title,url,language,author,summary FROM articles ORDER BY university_id,published_at DESC"):
            writer.writerow(tuple(row))
    database.close()
    progress = snapshot("finished")
    lines = [f"BÁO CÁO CRAWL TIN ĐẠI HỌC — {args.month}",
             f"Bắt đầu: {started_at}; kết thúc: {progress['updated_at']}.",
             f"Bộ lọc: ngày đăng từ {since} (gồm) đến trước {before}; loại bài không rõ ngày.",
             f"Tổng: {progress['total_articles']} bài, gồm {sum(initial.values())} bài tái sử dụng từ lần kiểm tra trước.",
             "Phạm vi: 10 trường trong QS World University Rankings 2027; nguồn trung tâm và khoa/bộ môn đã khám phá.", ""]
    for uni in registry["universities"]:
        uid = uni["id"]
        report = reports[uid]
        limitations = []
        if report.get("time_budget_exhausted"):
            limitations.append("hết thời gian")
        if report.get("remaining_candidates") or report.get("remaining_source_pages"):
            limitations.append("còn hàng đợi chưa duyệt")
        if report.get("sitemaps_fetched", 0) >= args.max_sitemaps:
            limitations.append("chạm giới hạn sitemap")
        if report.get("source_pages_fetched", 0) >= args.crawl_pages:
            limitations.append("chạm giới hạn trang")
        if report.get("failures") or report.get("status") == "error":
            limitations.append(f"{report.get('failures', 1)} lần thử lỗi")
        status = "; ".join(limitations) or "đã duyệt hết hàng đợi tìm thấy"
        lines.append(f"- {uni['name']}: {progress['articles_by_university'].get(uid, 0)} bài; {status}.")
    lines.extend(["", "Lưu ý: đây là số bài lấy được, không phải chứng nhận đầy đủ mọi bài trong tháng.",
                  "Tháng chưa kết thúc tại thời điểm chạy. Nguồn không truy cập được, nguồn chưa phát hiện,",
                  "bài ngoài phạm vi feed/phân trang/sitemap và các giới hạn chạy có thể làm thiếu dữ liệu.",
                  "Tuân thủ robots.txt và crawl-delay; không vượt hạn chế truy cập.",
                  "JSONL chứa toàn văn; CSV chứa metadata; SQLite chứa dữ liệu và nguồn gốc.",
                  "Chi tiết request/lỗi/giới hạn: crawl-report.json và crawl.log."])
    (output / "report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Done: {progress['total_articles']} articles; report: {output / 'report.txt'}", flush=True)


if __name__ == "__main__":
    main()
