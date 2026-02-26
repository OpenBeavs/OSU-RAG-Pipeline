#!/usr/bin/env python3
"""
OSU Web Crawler
===============
BFS web spider that discovers all reachable pages within *.oregonstate.edu,
starting from seed URLs in urls.txt.

Outputs discovered_urls.json with all found URLs and metadata.

Can be used standalone or called from etl_pipeline.py via --crawl flag.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent
URLS_FILE = ROOT_DIR / "urls.txt"
DISCOVERED_URLS_FILE = ROOT_DIR / "discovered_urls.json"

CRAWL_MAX_DEPTH = int(os.environ.get("CRAWL_MAX_DEPTH", "3"))
CRAWL_MAX_PAGES = int(os.environ.get("CRAWL_MAX_PAGES", "500"))
CRAWL_DELAY = float(os.environ.get("CRAWL_DELAY", "1.0"))
CRAWL_STRIP_QUERY = os.environ.get("CRAWL_STRIP_QUERY", "true").lower() == "true"

REQUEST_TIMEOUT = 30
MAX_RETRIES = 2
RETRY_BACKOFF = 2

USER_AGENT = "OSU-RAG-Bot/1.0 (+oregonstate.edu)"

# File extensions to skip (non-HTML resources)
SKIP_EXTENSIONS: set[str] = {
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".eot",
    ".zip", ".tar", ".gz", ".mp4", ".mp3", ".avi", ".mov",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".xml", ".rss", ".atom", ".json", ".csv",
}

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


# ══════════════════════════════════════════════
# URL Helpers
# ══════════════════════════════════════════════
def is_oregonstate_url(url: str) -> bool:
    """Check if a URL belongs to *.oregonstate.edu."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return host == "oregonstate.edu" or host.endswith(".oregonstate.edu")


def normalize_url(url: str) -> str:
    """
    Normalize a URL: lowercase scheme/host, strip fragment,
    optionally strip query params, remove trailing slash.
    """
    parsed = urlparse(url)

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"
    query = "" if CRAWL_STRIP_QUERY else parsed.query

    return urlunparse((scheme, netloc, path, parsed.params, query, ""))


def should_skip_url(url: str) -> bool:
    """Check if a URL points to a non-HTML resource."""
    parsed = urlparse(url)
    path_lower = parsed.path.lower()
    return any(path_lower.endswith(ext) for ext in SKIP_EXTENSIONS)


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


# ══════════════════════════════════════════════
# Robots.txt
# ══════════════════════════════════════════════
class RobotsChecker:
    """Caches robots.txt parsers per domain."""

    def __init__(self) -> None:
        self._cache: dict[str, RobotFileParser] = {}

    def can_fetch(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        if origin not in self._cache:
            rp = RobotFileParser()
            robots_url = f"{origin}/robots.txt"
            try:
                rp.set_url(robots_url)
                rp.read()
            except Exception:
                # If robots.txt is unreachable, allow crawling
                log.debug("Could not fetch robots.txt for %s -- allowing", origin)
                rp = RobotFileParser()
                rp.allow_all = True
            self._cache[origin] = rp

        return self._cache[origin].can_fetch(USER_AGENT, url)


# ══════════════════════════════════════════════
# Fetcher
# ══════════════════════════════════════════════
@dataclass
class FetchResult:
    """All metadata returned from a single page fetch."""
    html:         str | None = None
    status_code:  int | None = None
    final_url:    str        = ""
    content_type: str        = ""
    latency_ms:   int        = 0
    redirected:   bool       = False
    error:        str | None = None


def fetch_page(url: str) -> FetchResult:
    """Fetch a page with retries. Returns a FetchResult."""
    for attempt in range(1, MAX_RETRIES + 1):
        t0 = time.monotonic()
        try:
            resp = requests.get(
                url,
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
                allow_redirects=True,
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
            content_type = resp.headers.get("Content-Type", "")
            final_url = resp.url
            redirected = final_url.rstrip("/") != url.rstrip("/")

            if resp.status_code >= 400:
                return FetchResult(
                    status_code=resp.status_code,
                    final_url=final_url,
                    content_type=content_type,
                    latency_ms=latency_ms,
                    redirected=redirected,
                    error=f"HTTP {resp.status_code}",
                )

            # Only process HTML responses
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

            return FetchResult(
                html=resp.text,
                status_code=resp.status_code,
                final_url=final_url,
                content_type=content_type,
                latency_ms=latency_ms,
                redirected=redirected,
            )

        except requests.RequestException as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            wait = RETRY_BACKOFF ** attempt
            log.debug("Fetch attempt %d failed for %s: %s", attempt, url, exc)
            if attempt < MAX_RETRIES:
                time.sleep(wait)
            else:
                log.warning("Failed to fetch: %s", url)
                return FetchResult(error=str(exc), latency_ms=latency_ms)


# ══════════════════════════════════════════════
# Link Extractor
# ══════════════════════════════════════════════
def extract_links(html: str, base_url: str) -> list[str]:
    """Extract and resolve all <a href> links from HTML."""
    soup = BeautifulSoup(html, "lxml")
    links: list[str] = []

    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()

        # Skip javascript:, mailto:, tel:, etc.
        if re.match(r"^(javascript|mailto|tel|data|ftp):", href, re.IGNORECASE):
            continue

        # Resolve relative URLs
        absolute = urljoin(base_url, href)
        normalized = normalize_url(absolute)

        # Only keep oregonstate.edu URLs
        if is_oregonstate_url(normalized) and not should_skip_url(normalized):
            links.append(normalized)

    return links


# ══════════════════════════════════════════════
# BFS Crawler
# ══════════════════════════════════════════════
def crawl(seed_urls: list[str] | None = None) -> dict[str, Any]:
    """
    BFS crawl starting from seed URLs.

    Returns a dict of {url: {depth, discovered_at}} for all discovered URLs.
    """
    if seed_urls is None:
        seed_urls = load_seed_urls()

    if not seed_urls:
        log.warning("No seed URLs provided.")
        return {}

    log.info("=" * 60)
    log.info("OSU Web Crawler -- starting BFS crawl")
    log.info("  Seeds: %d | Max depth: %d | Max pages: %d | Delay: %.1fs",
             len(seed_urls), CRAWL_MAX_DEPTH, CRAWL_MAX_PAGES, CRAWL_DELAY)
    log.info("=" * 60)

    robots = RobotsChecker()
    visited: set[str] = set()
    discovered: dict[str, Any] = {}
    failed: dict[str, Any] = {}
    skipped_robots: list[str] = []
    queue: deque[tuple[str, int, str]] = deque()  # (url, depth, referrer)

    crawl_started_at = datetime.now(timezone.utc).isoformat()

    # Seed the queue
    for url in seed_urls:
        norm = normalize_url(url)
        if norm not in visited:
            queue.append((norm, 0, "[seed]"))
            visited.add(norm)

    pages_crawled = 0
    current_domain: str = ""
    domain_counts: dict[str, int] = {}

    while queue and pages_crawled < CRAWL_MAX_PAGES:
        url, depth, referrer = queue.popleft()

        # ── Domain-transition banner ──────────────────────────────
        domain = urlparse(url).hostname or ""
        if domain != current_domain:
            log.info("")
            log.info("── ▶  %s  (queue=%d, crawled=%d)",
                     domain, len(queue), pages_crawled)
            log.info("─" * 60)
            current_domain = domain

        # Check robots.txt
        if not robots.can_fetch(url):
            log.debug("  [robots] Blocked: %s", url)
            skipped_robots.append(url)
            continue

        # Fetch
        result = fetch_page(url)

        now_utc = datetime.now(timezone.utc).isoformat()

        # Error / non-HTML → record in failed list, don’t count as crawled
        if result.html is None:
            # Only warn for real errors (4xx/5xx/network), not non-html skips
            is_dead = result.error not in (None, "non-html")
            if is_dead:
                log.warning("  [DEAD] %s -- %s", result.error, url)
                log.warning("         Linked from: %s", referrer)
            else:
                log.debug("  [skip] %s -- %s", result.error, url)
            failed[url] = {
                "depth":        depth,
                "domain":       domain,
                "status_code":  result.status_code,
                "final_url":    result.final_url,
                "content_type": result.content_type,
                "latency_ms":   result.latency_ms,
                "redirected":   result.redirected,
                "error":        result.error,
                "referrer":     referrer,
                "attempted_at": now_utc,
            }
            continue

        pages_crawled += 1
        domain_counts[domain] = domain_counts.get(domain, 0) + 1

        # Extract links before storing so we can record link count
        new_links = 0
        new_cross_domain = 0
        links_found = 0
        if depth < CRAWL_MAX_DEPTH:
            links = extract_links(result.html, url)
            links_found = len(links)
            for link in links:
                if link not in visited:
                    visited.add(link)
                    queue.append((link, depth + 1, url))  # url is the referrer
                    new_links += 1
                    link_domain = urlparse(link).hostname or ""
                    if link_domain != domain:
                        new_cross_domain += 1
            if new_links:
                log.debug("    +%d new link(s) queued (%d cross-domain)", new_links, new_cross_domain)

        discovered[url] = {
            "depth":             depth,
            "domain":            domain,
            "status_code":       result.status_code,
            "final_url":         result.final_url,
            "content_type":      result.content_type,
            "latency_ms":        result.latency_ms,
            "redirected":        result.redirected,
            "links_found":       links_found,
            "new_links":         new_links,
            "new_cross_domain":  new_cross_domain,
            "crawled_at":        now_utc,
        }

        cross_note = f"  ({new_cross_domain} cross-domain)" if new_cross_domain else ""
        log.info("  [%d/%d] d=%d  %dms  +%d links%s  %s",
                 pages_crawled, CRAWL_MAX_PAGES, depth,
                 result.latency_ms, new_links, cross_note, url)

        # Politeness delay
        time.sleep(CRAWL_DELAY)

    crawl_ended_at = datetime.now(timezone.utc).isoformat()

    # ── Build domain summary for JSON ────────────────────────────
    domain_summary: dict[str, Any] = {}
    for url_entry, meta in discovered.items():
        d = meta["domain"]
        if d not in domain_summary:
            domain_summary[d] = {
                "pages": 0,
                "avg_latency_ms": 0,
                "total_latency_ms": 0,
                "redirected_count": 0,
            }
        domain_summary[d]["pages"] += 1
        domain_summary[d]["total_latency_ms"] += meta["latency_ms"]
        if meta["redirected"]:
            domain_summary[d]["redirected_count"] += 1
    for d, ds in domain_summary.items():
        ds["avg_latency_ms"] = round(ds["total_latency_ms"] / ds["pages"], 1)
        del ds["total_latency_ms"]

    # ── Save results ────────────────────────────────────────
    output = {
        "crawl_metadata": {
            "started_at":      crawl_started_at,
            "ended_at":        crawl_ended_at,
            "seed_urls":       seed_urls,
            "config": {
                "max_depth":       CRAWL_MAX_DEPTH,
                "max_pages":       CRAWL_MAX_PAGES,
                "crawl_delay_s":   CRAWL_DELAY,
                "strip_query":     CRAWL_STRIP_QUERY,
            },
            "summary": {
                "pages_crawled":   pages_crawled,
                "pages_failed":    len(failed),
                "pages_skipped_robots": len(skipped_robots),
                "urls_discovered": len(visited),
            },
            "pages_by_domain": domain_counts,
        },
        "domain_summary":   domain_summary,
        "urls":             discovered,
        "failed":           failed,
        "skipped_robots":   skipped_robots,
    }

    with open(DISCOVERED_URLS_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    log.info("")
    log.info("=" * 60)
    log.info("Crawl complete -- %d pages crawled, %d failed, %d robot-blocked",
             pages_crawled, len(failed), len(skipped_robots))
    log.info("Pages by domain:")
    for d, count in sorted(domain_counts.items(), key=lambda x: -x[1]):
        info = domain_summary[d]
        log.info("  %-45s  %4d page(s)  avg %dms", d, count, info["avg_latency_ms"])
    log.info("Results saved to %s", DISCOVERED_URLS_FILE)
    log.info("=" * 60)

    return discovered


# ══════════════════════════════════════════════
# Standalone CLI
# ══════════════════════════════════════════════
if __name__ == "__main__":
    crawl()
