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
def fetch_page(url: str) -> str | None:
    """Fetch a page with retries. Returns HTML or None."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url,
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
                allow_redirects=True,
            )
            resp.raise_for_status()

            # Only process HTML responses
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" not in content_type:
                log.debug("Skipping non-HTML: %s (%s)", url, content_type)
                return None

            return resp.text
        except requests.RequestException as exc:
            wait = RETRY_BACKOFF ** attempt
            log.debug("Fetch attempt %d failed for %s: %s", attempt, url, exc)
            time.sleep(wait)

    log.warning("Failed to fetch: %s", url)
    return None


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
    queue: deque[tuple[str, int]] = deque()  # (url, depth)

    # Seed the queue
    for url in seed_urls:
        norm = normalize_url(url)
        if norm not in visited:
            queue.append((norm, 0))
            visited.add(norm)

    pages_crawled = 0

    while queue and pages_crawled < CRAWL_MAX_PAGES:
        url, depth = queue.popleft()

        # Check robots.txt
        if not robots.can_fetch(url):
            log.debug("Blocked by robots.txt: %s", url)
            continue

        # Fetch
        html = fetch_page(url)
        if html is None:
            continue

        pages_crawled += 1
        now_utc = datetime.now(timezone.utc).isoformat()
        discovered[url] = {"depth": depth, "discovered_at": now_utc}

        if pages_crawled % 25 == 0 or pages_crawled <= 5:
            log.info("  [%d/%d] depth=%d %s",
                     pages_crawled, CRAWL_MAX_PAGES, depth, url)

        # Extract and enqueue links (if not at max depth)
        if depth < CRAWL_MAX_DEPTH:
            links = extract_links(html, url)
            new_links = 0
            for link in links:
                if link not in visited:
                    visited.add(link)
                    queue.append((link, depth + 1))
                    new_links += 1

        # Politeness delay
        time.sleep(CRAWL_DELAY)

    # Save results
    output = {
        "crawl_metadata": {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "seed_urls": seed_urls,
            "max_depth": CRAWL_MAX_DEPTH,
            "max_pages": CRAWL_MAX_PAGES,
            "pages_crawled": pages_crawled,
            "total_discovered": len(discovered),
        },
        "urls": discovered,
    }

    with open(DISCOVERED_URLS_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    log.info("=" * 60)
    log.info("Crawl complete -- %d pages crawled, %d URLs discovered",
             pages_crawled, len(discovered))
    log.info("Results saved to %s", DISCOVERED_URLS_FILE)
    log.info("=" * 60)

    return discovered


# ══════════════════════════════════════════════
# Standalone CLI
# ══════════════════════════════════════════════
if __name__ == "__main__":
    crawl()
