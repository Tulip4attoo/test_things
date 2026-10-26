"""Bounded HTTP client with robots checks on every redirect target."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests

from .parsing import allowed_url, normalize_url

LOG = logging.getLogger(__name__)


class FetchError(Exception):
    pass


class RobotsDenied(FetchError):
    pass


class BudgetExceeded(FetchError):
    pass


@dataclass
class Document:
    url: str
    body: bytes
    content_type: str = ""


class Client:
    def __init__(self, domains, *, delay=1.0, timeout=20.0, retries=2,
                 user_agent="QSUniversityNewsBot/0.1", max_bytes=5_000_000, time_budget=None, state=None):
        self.domains = domains
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.max_bytes = max_bytes
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "text/html,application/xml,application/rss+xml,application/atom+xml;q=0.9,*/*;q=0.5"})
        self.user_agent = user_agent
        state = {} if state is None else state
        self.robots = state.setdefault("robots", {})
        self.last_request = state.setdefault("last_request", {})
        self.events = []
        self.requests = 0
        self.deadline = time.monotonic() + time_budget if time_budget else None
        self.budget_deferred = False

    @property
    def exhausted(self):
        return self.deadline is not None and time.monotonic() >= self.deadline

    def _check_budget(self, wait=0):
        if self.deadline is not None and time.monotonic() + wait >= self.deadline:
            self.budget_deferred = True
            raise BudgetExceeded("Time budget cannot accommodate this request or required wait; deferred")

    def close(self):
        self.session.close()

    def _pace(self, url):
        origin = self._origin(url)
        parser = self.robots.get(origin)
        crawl_delay = parser.crawl_delay(self.user_agent) if parser else None
        interval = max(self.delay, crawl_delay or 0)
        if parser:
            rate = parser.request_rate(self.user_agent)
            if rate and rate.requests:
                interval = max(interval, rate.seconds / rate.requests)
        remaining = interval - (time.monotonic() - self.last_request.get(origin, 0))
        self._check_budget(max(remaining, 0))
        if remaining > 0:
            if remaining > 5:
                LOG.warning("Waiting %.0fs to respect crawl limits for %s", remaining, origin)
            time.sleep(remaining)
        self.last_request[origin] = time.monotonic()

    @staticmethod
    def _origin(url):
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}"

    def _raw(self, url):
        for attempt in range(self.retries + 1):
            self._pace(url)
            self.requests += 1
            try:
                timeout = min(self.timeout, max(0.1, self.deadline - time.monotonic())) if self.deadline else self.timeout
                response = self.session.get(url, timeout=timeout, allow_redirects=False, stream=True)
            except requests.RequestException as exc:
                if attempt == self.retries:
                    raise FetchError(str(exc)) from exc
                self._check_budget(2 ** attempt)
                time.sleep(2 ** attempt)
                continue
            if response.status_code in {429, 500, 502, 503, 504} and attempt < self.retries:
                retry = response.headers.get("Retry-After", "")
                response.close()
                try:
                    wait = float(retry)
                except ValueError:
                    try:
                        wait = (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError, OverflowError):
                        wait = 2 ** attempt
                # Do not retry early when a site asks for a long pause.
                if wait > 30:
                    raise FetchError(f"Server requested retry after {wait:.0f}s; try a later run: {url}")
                self._check_budget(max(wait, 0))
                time.sleep(max(wait, 0))
                continue
            return response
        raise FetchError(url)

    def _body(self, response):
        chunks, size = [], 0
        try:
            for chunk in response.iter_content(65536):
                self._check_budget()
                size += len(chunk)
                if size > self.max_bytes:
                    raise FetchError(f"Response exceeds {self.max_bytes} bytes: {response.url}")
                chunks.append(chunk)
            return b"".join(chunks)
        except requests.RequestException as exc:
            raise FetchError(str(exc)) from exc
        finally:
            response.close()

    def _policy(self, url):
        origin = self._origin(url)
        if origin in self.robots:
            return self.robots[origin]
        robot_url = origin + "/robots.txt"
        parser = RobotFileParser(robot_url)
        try:
            # Fetch redirects explicitly; never follow an external robots redirect.
            target = robot_url
            for _ in range(6):
                if not allowed_url(target, self.domains):
                    raise FetchError("robots.txt redirects outside university domains")
                response = self._raw(target)
                if response.is_redirect or response.is_permanent_redirect:
                    next_url = normalize_url(response.headers.get("Location", ""), target)
                    response.close()
                    if not next_url:
                        raise FetchError("Invalid robots redirect")
                    target = next_url
                    continue
                if response.status_code in {404, 410}:
                    response.close()
                    parser.parse([])
                elif response.status_code == 200:
                    body = self._body(response).decode("utf-8", "replace")
                    if "<html" in body[:500].lower():
                        raise FetchError("robots.txt returned HTML")
                    parser.parse(body.splitlines())
                else:
                    code = response.status_code
                    response.close()
                    raise FetchError(f"robots.txt HTTP {code}")
                self.robots[origin] = parser
                return parser
            raise FetchError("Too many robots redirects")
        except BudgetExceeded:
            raise
        except FetchError as exc:
            parser.parse(["User-agent: *", "Disallow: /"])
            self.robots[origin] = parser
            self.events.append({"url": robot_url, "status": "robots_unavailable", "error": str(exc)})
            LOG.warning("%s: %s", robot_url, exc)
            return parser

    def sitemap_urls(self, url):
        return self._policy(url).site_maps() or []

    def get(self, url):
        target = normalize_url(url)
        try:
            for _ in range(6):
                if not target or not allowed_url(target, self.domains):
                    raise FetchError(f"Outside allowed university domains: {target}")
                if not self._policy(target).can_fetch(self.user_agent, target):
                    raise RobotsDenied(f"robots.txt disallows {target} (or policy unavailable)")
                response = self._raw(target)
                if response.is_redirect or response.is_permanent_redirect:
                    next_url = normalize_url(response.headers.get("Location", ""), target)
                    response.close()
                    target = next_url
                    continue
                if response.status_code != 200:
                    code = response.status_code
                    response.close()
                    raise FetchError(f"HTTP {code}: {target}")
                doc = Document(target, self._body(response), response.headers.get("Content-Type", ""))
                self.events.append({"url": url, "final_url": target, "status": "ok"})
                return doc
            raise FetchError(f"Too many redirects: {url}")
        except FetchError as exc:
            status = "deferred" if isinstance(exc, BudgetExceeded) else "blocked" if isinstance(exc, RobotsDenied) else "error"
            self.events.append({"url": url, "status": status, "error": str(exc)})
            LOG.warning("%s", exc)
            raise
