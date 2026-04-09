#!/usr/bin/env python3
"""
OSU Web Crawler
===============
BFS web spider that discovers all reachable pages within *.oregonstate.edu,
starting from seed URLs in urls.txt.

Outputs discovered_urls.json with all found URLs, metadata, AND cached page
text/title so the ETL pipeline can skip re-fetching.

Can be used standalone or called from etl_pipeline.py via --crawl flag.

Performance: uses a ThreadPoolExecutor for parallel fetching with per-domain
rate limiting so we stay polite without slowing down cross-domain crawls.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import sys
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait as cf_wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter

load_dotenv()

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent
URLS_FILE = ROOT_DIR / "urls.txt"
DISCOVERED_URLS_FILE = ROOT_DIR / "discovered_urls.json"
EXCLUSIONS_FILE = ROOT_DIR / "url_exclusions.txt"

CRAWL_MAX_DEPTH   = int(os.environ.get("CRAWL_MAX_DEPTH", "3"))
CRAWL_MAX_PAGES   = int(os.environ.get("CRAWL_MAX_PAGES", "5000"))
CRAWL_DELAY       = float(os.environ.get("CRAWL_DELAY", "1.0"))
CRAWL_JITTER      = float(os.environ.get("CRAWL_JITTER", "0.5"))   # ± random seconds added to each delay
CRAWL_STRIP_QUERY = os.environ.get("CRAWL_STRIP_QUERY", "true").lower() == "true"
CRAWL_MAX_WORKERS = int(os.environ.get("CRAWL_MAX_WORKERS", "5"))

# Set to "false" to disable robots.txt checking entirely (useful for sites
# that redirect the robots.txt fetch to a login page, blocking all crawling).
CRAWL_RESPECT_ROBOTS = os.environ.get("CRAWL_RESPECT_ROBOTS", "true").lower() == "true"

# Comma-separated domains to bypass robots.txt checking even when
# CRAWL_RESPECT_ROBOTS is true.  Example: "advantage.oregonstate.edu"
CRAWL_ROBOTS_IGNORE_DOMAINS: set[str] = {
    d.strip().lower()
    for d in os.environ.get("CRAWL_ROBOTS_IGNORE_DOMAINS", "").split(",")
    if d.strip()
}

BASE_URL_TO_SCRAPE = os.environ.get("BASE_URL_TO_SCRAPE", "oregonstate.edu")

# Pages with fewer than this many words after boilerplate removal are considered
# "thin" — they are still recorded in JSON but their outbound links are NOT
# enqueued, preventing stub/index pages from spawning huge BFS sub-trees.
MIN_TEXT_WORDS = int(os.environ.get("CRAWL_MIN_WORDS", "150"))

# Extra comma-separated exclusion patterns from environment (merged with exclusions file)
CRAWL_EXCLUDE_ENV = [
    p.strip() for p in os.environ.get("CRAWL_EXCLUDE_PATTERNS", "").split(",") if p.strip()
]

REQUEST_TIMEOUT = 30
MAX_RETRIES     = 2
RETRY_BACKOFF   = 2

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Headers that make requests look like a real browser navigation.
# WAFs (Cloudflare, Akamai, etc.) flag requests that are missing these.
BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# File extensions to skip (non-HTML resources)
SKIP_EXTENSIONS: set[str] = {
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".eot",
    ".zip", ".tar", ".gz", ".mp4", ".mp3", ".avi", ".mov",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".xml", ".rss", ".atom", ".json", ".csv",
}

# Boilerplate tags to strip before text extraction
BOILERPLATE_TAGS: tuple[str, ...] = (
    "nav", "footer", "header", "script", "style", "noscript", "aside"
)

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("osu-crawler")

# ──────────────────────────────────────────────
# Thread-local HTTP session (connection pooling)
# ──────────────────────────────────────────────
_thread_local = threading.local()


def _get_session() -> requests.Session:
    """Return a per-thread requests.Session with a persistent connection pool.

    Reusing sessions avoids the overhead of a fresh TCP handshake (and TLS
    negotiation) on every request.  With 60 workers each hitting the same
    subdomain repeatedly, this meaningfully reduces per-request latency.
    """
    if not hasattr(_thread_local, "session"):
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20)
        sess = requests.Session()
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        _thread_local.session = sess
    return _thread_local.session


# ══════════════════════════════════════════════
# URL Helpers
# ══════════════════════════════════════════════
def is_oregonstate_url(url: str) -> bool:
    """Check if a URL belongs to *.oregonstate.edu (or BASE_URL_TO_SCRAPE)."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return host == BASE_URL_TO_SCRAPE or host.endswith("." + BASE_URL_TO_SCRAPE)


def normalize_url(url: str) -> str:
    """
    Normalize a URL: lowercase scheme/host, strip fragment,
    optionally strip query params, remove trailing slash.
    """
    parsed = urlparse(url)

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path   = parsed.path.rstrip("/") or "/"
    query  = "" if CRAWL_STRIP_QUERY else parsed.query

    return urlunparse((scheme, netloc, path, parsed.params, query, ""))


def should_skip_url(url: str) -> bool:
    """Check if a URL points to a non-HTML resource by extension."""
    parsed     = urlparse(url)
    path_lower = parsed.path.lower()
    return any(path_lower.endswith(ext) for ext in SKIP_EXTENSIONS)


# Regex for low-quality URL segments: pure numeric IDs, UUIDs, hex hashes
_NUMERIC_SLUG = re.compile(r"/(\d{4,})(?:/|$)")
_UUID_SLUG    = re.compile(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:/|$)", re.I)
_HEX_SLUG     = re.compile(r"/[0-9a-f]{24,}(?:/|$)", re.I)
_MAX_PATH_SEGS = 8


def is_low_quality_url(url: str) -> str | None:
    """
    Cheap pre-fetch heuristic check. Returns a reason string if the URL looks
    low-quality (should be skipped), or None if it looks fine.
    """
    parsed = urlparse(url)
    path   = parsed.path

    if _NUMERIC_SLUG.search(path):
        return "numeric-slug"
    if _UUID_SLUG.search(path):
        return "uuid-slug"
    if _HEX_SLUG.search(path):
        return "hex-slug"
    segments = [s for s in path.split("/") if s]
    if len(segments) >= _MAX_PATH_SEGS:
        return f"path-too-deep ({len(segments)} segments)"
    return None


def load_seed_urls(path: Path = URLS_FILE) -> list[str]:
    """Read seed URLs from a text file (one per line, # comments allowed)."""
    if not path.exists():
        log.error("Seed URL file not found: %s", path)
        return []
    urls: list[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


def load_exclusions(path: Path = EXCLUSIONS_FILE) -> list[str]:
    """
    Load URL exclusion patterns from a text file.
    Each non-comment line is treated as a substring/prefix that, if found
    anywhere in a URL, causes that URL to be skipped.
    Patterns from CRAWL_EXCLUDE_PATTERNS env var are merged in.
    """
    patterns: list[str] = list(CRAWL_EXCLUDE_ENV)
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
    return patterns


# Module-level exclusions loaded once at import time
_EXCLUSIONS: list[str] = load_exclusions()


def is_excluded_url(url: str) -> bool:
    """Return True if the URL matches any exclusion pattern."""
    url_lower = url.lower()
    return any(pat.lower() in url_lower for pat in _EXCLUSIONS)


# ══════════════════════════════════════════════
# Robots.txt
# ══════════════════════════════════════════════
class RobotsChecker:
    """Caches robots.txt parsers per domain. Thread-safe."""

    def __init__(self) -> None:
        self._cache: dict[str, RobotFileParser] = {}
        self._lock  = threading.Lock()

    def can_fetch(self, url: str) -> bool:
        # Global bypass
        if not CRAWL_RESPECT_ROBOTS:
            return True

        parsed = urlparse(url)
        domain = (parsed.hostname or "").lower()
        origin = f"{parsed.scheme}://{parsed.netloc}"

        # Per-domain bypass
        if domain in CRAWL_ROBOTS_IGNORE_DOMAINS:
            return True

        with self._lock:
            if origin not in self._cache:
                rp = RobotFileParser()
                robots_url = f"{origin}/robots.txt"
                try:
                    rp.set_url(robots_url)
                    rp.read()
                    # Sanity-check: if the parser blocks everything including the root,
                    # the robots.txt was likely a login redirect page — treat as allow-all.
                    if not rp.can_fetch("*", origin + "/"):
                        log.debug(
                            "robots.txt for %s blocks root — likely a login redirect, allowing",
                            origin,
                        )
                        rp = RobotFileParser()
                        rp.allow_all = True
                except Exception:
                    log.debug("Could not fetch robots.txt for %s -- allowing", origin)
                    rp = RobotFileParser()
                    rp.allow_all = True
                self._cache[origin] = rp

            return self._cache[origin].can_fetch(USER_AGENT, url)


# ══════════════════════════════════════════════
# Domain-level Rate Limiter
# ══════════════════════════════════════════════
class DomainRateLimiter:
    """
    Enforces a minimum delay between fetches to the same domain.
    Thread-safe: multiple workers share one instance.
    """

    def __init__(self, delay_s: float = CRAWL_DELAY, jitter_s: float = CRAWL_JITTER) -> None:
        self._delay   = delay_s
        self._jitter  = jitter_s
        self._last:   dict[str, float] = {}
        self._locks:  dict[str, threading.Lock] = {}
        self._global  = threading.Lock()

    def _domain_lock(self, domain: str) -> threading.Lock:
        with self._global:
            if domain not in self._locks:
                self._locks[domain] = threading.Lock()
            return self._locks[domain]

    def wait(self, url: str) -> None:
        """Block until it is polite to fetch `url`, then add random jitter."""
        if self._delay <= 0:
            return
        domain  = urlparse(url).hostname or url
        lock    = self._domain_lock(domain)
        with lock:
            now     = time.monotonic()
            elapsed = now - self._last.get(domain, 0.0)
            wait    = self._delay - elapsed
            if self._jitter > 0:
                wait += random.uniform(0, self._jitter)
            if wait > 0:
                time.sleep(wait)
            self._last[domain] = time.monotonic()


# ══════════════════════════════════════════════
# Fetcher
# ══════════════════════════════════════════════
@dataclass
class FetchResult:
    """All metadata returned from a single page fetch."""
    html:         str | None = None
    text:         str | None = None   # cleaned body text (cached for ETL reuse)
    title:        str        = ""
    status_code:  int | None = None
    final_url:    str        = ""
    content_type: str        = ""
    latency_ms:   int        = 0
    redirected:   bool       = False
    error:        str | None = None


def _extract_text_and_title(html: str) -> tuple[str, str]:
    """Strip boilerplate tags, return (title, cleaned_body_text)."""
    soup = BeautifulSoup(html, "lxml")
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""
    for tag in BOILERPLATE_TAGS:
        for t in soup.find_all(tag):
            t.decompose()
    text = soup.get_text(separator=" ")
    return title, text


def _build_headers(url: str, referrer: str | None) -> dict[str, str]:
    """Build request headers with correct Referer and Sec-Fetch-Site for this navigation."""
    headers = dict(BROWSER_HEADERS)
    if not referrer or referrer == "[seed]":
        headers["Sec-Fetch-Site"] = "none"
        return headers

    ref_host = urlparse(referrer).hostname or ""
    req_host = urlparse(url).hostname or ""

    # Determine eTLD+1 by taking last two labels (works for .edu)
    def etld1(host: str) -> str:
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host

    if ref_host == req_host:
        fetch_site = "same-origin"
    elif etld1(ref_host) == etld1(req_host):
        fetch_site = "same-site"
    else:
        fetch_site = "cross-site"

    headers["Sec-Fetch-Site"] = fetch_site
    headers["Referer"] = referrer
    return headers


def fetch_page(
    url: str,
    rate_limiter: DomainRateLimiter | None = None,
    referrer: str | None = None,
) -> FetchResult:
    """Fetch a page with retries. Respects domain rate limiter if provided."""
    headers = _build_headers(url, referrer)
    for attempt in range(1, MAX_RETRIES + 1):
        if rate_limiter:
            rate_limiter.wait(url)
        t0 = time.monotonic()
        try:
            resp = _get_session().get(
                url,
                timeout=REQUEST_TIMEOUT,
                headers=headers,
                allow_redirects=True,
            )
            latency_ms   = int((time.monotonic() - t0) * 1000)
            content_type = resp.headers.get("Content-Type", "")
            final_url    = resp.url
            redirected   = final_url.rstrip("/") != url.rstrip("/")

            if resp.status_code >= 400:
                # Some WAFs (e.g. Akamai) return 403 with a Set-Cookie challenge
                # and expect a retry with the cookie.  If we got cookies, retry once.
                if resp.status_code == 403 and resp.cookies and attempt < MAX_RETRIES:
                    log.debug("403 with cookies on %s, retrying with WAF cookie", url)
                    continue
                return FetchResult(
                    status_code=resp.status_code,
                    final_url=final_url,
                    content_type=content_type,
                    latency_ms=latency_ms,
                    redirected=redirected,
                    error=f"HTTP {resp.status_code}",
                )

            if "text/html" not in content_type:
                log.debug("Skipping non-HTML: %s (%s)", url, content_type)
                return FetchResult(
                    status_code=resp.status_code,
                    final_url=final_url,
                    content_type=content_type,
                    latency_ms=latency_ms,
                    redirected=redirected,
                    error="non-html",
                )

            # Extract cleaned text + title eagerly so ETL can skip re-fetching
            title, text = _extract_text_and_title(resp.text)

            return FetchResult(
                html=resp.text,
                text=text,
                title=title,
                status_code=resp.status_code,
                final_url=final_url,
                content_type=content_type,
                latency_ms=latency_ms,
                redirected=redirected,
            )

        except requests.RequestException as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            backoff = RETRY_BACKOFF ** attempt
            log.debug("Fetch attempt %d failed for %s: %s", attempt, url, exc)
            if attempt < MAX_RETRIES:
                time.sleep(backoff)
            else:
                log.warning("Failed to fetch: %s", url)
                return FetchResult(error=str(exc), latency_ms=latency_ms)


# ══════════════════════════════════════════════
# Link Extractor
# ══════════════════════════════════════════════
def extract_links(html: str, base_url: str) -> list[str]:
    """Extract and resolve all <a href> links from HTML."""
    soup  = BeautifulSoup(html, "lxml")
    links: list[str] = []

    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()

        if re.match(r"^(javascript|mailto|tel|data|ftp):", href, re.IGNORECASE):
            continue

        absolute   = urljoin(base_url, href)
        normalized = normalize_url(absolute)

        if is_oregonstate_url(normalized) and not should_skip_url(normalized):
            links.append(normalized)

    return links


# ══════════════════════════════════════════════
# Parallel BFS Crawler
# ══════════════════════════════════════════════

@dataclass
class _WorkItem:
    url:      str
    depth:    int
    referrer: str


def crawl(seed_urls: list[str] | None = None) -> dict[str, Any]:
    """
    Parallel BFS crawl starting from seed URLs.

    Uses a ThreadPoolExecutor (CRAWL_MAX_WORKERS workers) so multiple
    pages are fetched concurrently while per-domain rate limiting keeps
    the crawl polite.

    Returns a dict of {url: metadata} for all successfully crawled URLs.
    The metadata includes 'text' and 'title' so the ETL pipeline can skip
    re-fetching pages that were already fetched during crawling.
    """
    if seed_urls is None:
        seed_urls = load_seed_urls()

    if not seed_urls:
        log.warning("No seed URLs provided.")
        return {}

    log.info("=" * 60)
    log.info("OSU Web Crawler -- starting parallel BFS crawl")
    log.info(
        "  Seeds: %d | Max depth: %d | Max pages: %d | Workers: %d | Delay: %.2fs ± %.2fs jitter",
        len(seed_urls), CRAWL_MAX_DEPTH, CRAWL_MAX_PAGES, CRAWL_MAX_WORKERS, CRAWL_DELAY, CRAWL_JITTER,
    )
    if _EXCLUSIONS:
        log.info("  Exclusion patterns: %d (see url_exclusions.txt)", len(_EXCLUSIONS))
    log.info("=" * 60)

    robots        = RobotsChecker()
    rate_limiter  = DomainRateLimiter(delay_s=CRAWL_DELAY)

    # Shared mutable state — all guarded by _state_lock
    _state_lock   = threading.Lock()
    visited:       set[str]          = set()
    pending_queue: deque[_WorkItem]  = deque()
    discovered:    dict[str, Any]    = {}
    failed:        dict[str, Any]    = {}
    skipped_robots: list[str]        = []
    domain_counts: dict[str, int]    = {}
    domain_graph:  dict[str, set[str]] = defaultdict(set)  # from_domain → {to_domain}

    pages_crawled = 0
    in_flight     = 0  # futures currently running

    crawl_started_at = datetime.now(timezone.utc).isoformat()

    # Seed the queue
    for url in seed_urls:
        norm = normalize_url(url)
        if norm not in visited:
            pending_queue.append(_WorkItem(url=norm, depth=0, referrer="[seed]"))
            visited.add(norm)

    def _process_one(item: _WorkItem) -> tuple[_WorkItem, FetchResult]:
        """Worker: fetch one URL and return its result. Run in thread pool."""
        return item, fetch_page(item.url, rate_limiter=rate_limiter, referrer=item.referrer)

    with ThreadPoolExecutor(max_workers=CRAWL_MAX_WORKERS) as executor:
        active_futures: dict[Future, _WorkItem] = {}

        def _submit_pending() -> None:
            """Submit as many queued items as we have worker budget for."""
            nonlocal pages_crawled
            while pending_queue:
                with _state_lock:
                    if pages_crawled + len(active_futures) >= CRAWL_MAX_PAGES:
                        break
                item = pending_queue.popleft()

                # Robots check (fast, cached after first domain hit)
                if not robots.can_fetch(item.url):
                    log.debug("  [robots] Blocked: %s", item.url)
                    with _state_lock:
                        skipped_robots.append(item.url)
                    continue

                fut = executor.submit(_process_one, item)
                active_futures[fut] = item

        _submit_pending()

        while active_futures:
            # Block until at least one future finishes (no busy-wait / sleep loop).
            done_set, _ = cf_wait(list(active_futures.keys()), return_when=FIRST_COMPLETED, timeout=1.0)

            for fut in done_set:
                item   = active_futures.pop(fut)
                result: FetchResult
                try:
                    _, result = fut.result()
                except Exception as exc:
                    log.warning("Unexpected error fetching %s: %s", item.url, exc)
                    result = FetchResult(error=str(exc))

                now_utc = datetime.now(timezone.utc).isoformat()
                domain  = urlparse(item.url).hostname or ""

                # ── Error / non-HTML ────────────────────────────────────────
                if result.html is None:
                    is_dead = result.error not in (None, "non-html")
                    if is_dead:
                        log.warning("  [DEAD] %s -- %s", result.error, item.url)
                        log.warning("         Linked from: %s", item.referrer)
                    else:
                        log.debug("  [skip] %s -- %s", result.error, item.url)
                    with _state_lock:
                        failed[item.url] = {
                            "depth":        item.depth,
                            "domain":       domain,
                            "status_code":  result.status_code,
                            "final_url":    result.final_url,
                            "content_type": result.content_type,
                            "latency_ms":   result.latency_ms,
                            "redirected":   result.redirected,
                            "error":        result.error,
                            "referrer":     item.referrer,
                            "attempted_at": now_utc,
                        }
                    continue

                # ── Successful HTML page ────────────────────────────────────
                text  = result.text or ""
                title = result.title or ""
                word_count = len(text.split())
                is_thin    = word_count < MIN_TEXT_WORDS

                with _state_lock:
                    pages_crawled += 1
                    domain_counts[domain] = domain_counts.get(domain, 0) + 1
                    pc = pages_crawled

                if is_thin:
                    log.debug("  [thin] %d words, skipping child links: %s", word_count, item.url)

                # ── Enqueue child links ─────────────────────────────────────
                new_links        = 0
                new_cross_domain = 0
                links_found      = 0

                if item.depth < CRAWL_MAX_DEPTH and not is_thin:
                    links       = extract_links(result.html, item.url)
                    links_found = len(links)
                    with _state_lock:
                        for link in links:
                            link_domain = urlparse(link).hostname or ""
                            # Record domain-level edge for every outbound link
                            if link_domain and link_domain != domain:
                                domain_graph[domain].add(link_domain)
                            if link not in visited:
                                lq = is_low_quality_url(link)
                                if lq:
                                    log.debug("  [lq-url] %s -- %s", lq, link)
                                    visited.add(link)
                                    continue
                                if is_excluded_url(link):
                                    log.debug("  [excluded] %s", link)
                                    visited.add(link)
                                    continue
                                visited.add(link)
                                pending_queue.append(_WorkItem(url=link, depth=item.depth + 1, referrer=item.url))
                                new_links += 1
                                if link_domain != domain:
                                    new_cross_domain += 1

                with _state_lock:
                    discovered[item.url] = {
                        "depth":            item.depth,
                        "domain":           domain,
                        "status_code":      result.status_code,
                        "final_url":        result.final_url,
                        "content_type":     result.content_type,
                        "latency_ms":       result.latency_ms,
                        "redirected":       result.redirected,
                        "word_count":       word_count,
                        "thin":             is_thin,
                        "title":            title,
                        # ── Cached content for ETL (avoids re-fetch) ──────
                        "text":             text,
                        # ──────────────────────────────────────────────────
                        "links_found":      links_found,
                        "new_links":        new_links,
                        "new_cross_domain": new_cross_domain,
                        "crawled_at":       now_utc,
                    }

                cross_note = f"  ({new_cross_domain} cross-domain)" if new_cross_domain else ""
                if pc % 25 == 0 or pc <= 5:
                    log.info(
                        "  [%d/%d] d=%d  %dms  +%d links%s  %s",
                        pc, CRAWL_MAX_PAGES, item.depth,
                        result.latency_ms, new_links, cross_note, item.url,
                    )

                # Submit more work now that we have capacity
                _submit_pending()

    crawl_ended_at = datetime.now(timezone.utc).isoformat()

    # ── Build domain summary ─────────────────────────────────────────────────
    domain_summary: dict[str, Any] = {}
    for url_entry, meta in discovered.items():
        d = meta["domain"]
        if d not in domain_summary:
            domain_summary[d] = {
                "pages":            0,
                "avg_latency_ms":   0,
                "total_latency_ms": 0,
                "redirected_count": 0,
            }
        domain_summary[d]["pages"]            += 1
        domain_summary[d]["total_latency_ms"] += meta["latency_ms"]
        if meta["redirected"]:
            domain_summary[d]["redirected_count"] += 1
    for d, ds in domain_summary.items():
        ds["avg_latency_ms"] = round(ds["total_latency_ms"] / ds["pages"], 1)
        del ds["total_latency_ms"]

    # ── Save results ─────────────────────────────────────────────────────────
    output = {
        "crawl_metadata": {
            "started_at": crawl_started_at,
            "ended_at":   crawl_ended_at,
            "seed_urls":  seed_urls,
            "config": {
                "max_depth":     CRAWL_MAX_DEPTH,
                "max_pages":     CRAWL_MAX_PAGES,
                "crawl_delay_s": CRAWL_DELAY,
                "strip_query":   CRAWL_STRIP_QUERY,
                "max_workers":   CRAWL_MAX_WORKERS,
            },
            "summary": {
                "pages_crawled":        pages_crawled,
                "pages_failed":         len(failed),
                "pages_skipped_robots": len(skipped_robots),
                "urls_discovered":      len(visited),
            },
            "pages_by_domain": domain_counts,
        },
        "domain_summary":  domain_summary,
        "domain_graph":    {k: sorted(v) for k, v in domain_graph.items()},
        "urls":            discovered,
        "failed":          failed,
        "skipped_robots":  skipped_robots,
    }

    with open(DISCOVERED_URLS_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    log.info("")
    log.info("=" * 60)
    log.info(
        "Crawl complete -- %d pages crawled, %d failed, %d robot-blocked",
        pages_crawled, len(failed), len(skipped_robots),
    )
    log.info("Pages by domain:")
    for d, count in sorted(domain_counts.items(), key=lambda x: -x[1]):
        info = domain_summary[d]
        log.info("  %-45s  %4d page(s)  avg %dms", d, count, info["avg_latency_ms"])
    log.info("Results saved to %s (includes cached text for ETL reuse)", DISCOVERED_URLS_FILE)
    log.info("=" * 60)

    return discovered


# ══════════════════════════════════════════════
# Standalone CLI
# ══════════════════════════════════════════════
if __name__ == "__main__":
    crawl()
