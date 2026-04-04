#!/usr/bin/env python3
"""
OSU RAG ETL Pipeline
====================
Scrapes *.oregonstate.edu pages, chunks the text, generates embeddings via
Google GenAI (gemini-embedding-001), and upserts them into Firestore
with vector search support.

Performance features:
  - Parallel URL processing via ThreadPoolExecutor (ETL_MAX_WORKERS workers)
  - Thread-safe StateManager with per-URL locking
  - Skips re-fetching pages whose text was already cached by crawler.py
  - Batched Firestore deletes (no more one-doc-at-a-time loop)

Designed to run as a cron job. Content-hash deduplication ensures only
changed pages are re-processed, and stale vectors are deleted before new
ones are upserted.

Usage:
    python etl_pipeline.py                 # full run (reads urls.txt)
    python etl_pipeline.py --crawl         # crawl first, then embed
    python etl_pipeline.py --dry-run       # skip embedding & Firestore calls

Environment variables (see .env.example):
    GOOGLE_API_KEY, GCP_PROJECT_ID, FIRESTORE_COLLECTION
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from google import genai
from google.cloud import firestore
from google.cloud.firestore_v1.vector import Vector
from langchain_text_splitters import RecursiveCharacterTextSplitter

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
load_dotenv()

ROOT_DIR            = Path(__file__).resolve().parent
HASH_STORE_PATH     = ROOT_DIR / "url_hashes.json"
URLS_FILE           = ROOT_DIR / "urls.txt"
DISCOVERED_URLS_FILE = ROOT_DIR / "discovered_urls.json"

EMBEDDING_MODEL     = "gemini-embedding-001"
EMBEDDING_DIMENSION = 768
EMBEDDING_BATCH_SIZE = 100          # Google GenAI max per request

CHUNK_SIZE          = 512           # tokens
CHUNK_OVERLAP       = 64            # tokens

REQUEST_TIMEOUT     = 30            # seconds
MAX_RETRIES         = 3
RETRY_BACKOFF       = 2             # seconds (exponential base)

# How many URLs to process in parallel
import os as _os
ETL_MAX_WORKERS = int(_os.environ.get("ETL_MAX_WORKERS", "25"))

# Tags whose entire subtree is stripped from the HTML before text extraction.
BOILERPLATE_TAGS: list[str] = [
    "nav", "footer", "header", "script", "style", "noscript",
    "aside", "form", "iframe",
]

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("osu-rag-etl")

# ──────────────────────────────────────────────
# Thread-local HTTP session (connection pooling)
# ──────────────────────────────────────────────
_thread_local = threading.local()


def _get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20)
        sess = requests.Session()
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        _thread_local.session = sess
    return _thread_local.session


# ══════════════════════════════════════════════
# 1.  State Management (Deduplication)
# ══════════════════════════════════════════════
class StateManager:
    """
    Persists `{url: content_hash}` to a local JSON file.
    Thread-safe: all mutations go through an internal lock.
    """

    def __init__(self, path: Path = HASH_STORE_PATH) -> None:
        self.path   = path
        self._lock  = threading.Lock()
        self.hashes: dict[str, str] = self._load()

    # --- persistence ---
    def _load(self) -> dict[str, str]:
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def save(self) -> None:
        with self._lock:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.hashes, f, indent=2)

    # --- hash helpers ---
    @staticmethod
    def compute_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def has_changed(self, url: str, content_hash: str) -> bool:
        with self._lock:
            return self.hashes.get(url) != content_hash

    def update(self, url: str, content_hash: str) -> None:
        with self._lock:
            self.hashes[url] = content_hash


# ══════════════════════════════════════════════
# 2.  Web Fetching
# ══════════════════════════════════════════════
def fetch_page(url: str) -> str | None:
    """Fetch raw HTML with retries and exponential backoff."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = _get_session().get(
                url,
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": "OSU-RAG-Bot/1.0 (+oregonstate.edu)"},
            )
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            backoff = RETRY_BACKOFF ** attempt
            log.warning(
                "Fetch attempt %d/%d failed for %s: %s -- retrying in %ds",
                attempt, MAX_RETRIES, url, exc, backoff,
            )
            time.sleep(backoff)
    log.error("All %d fetch attempts failed for %s -- skipping.", MAX_RETRIES, url)
    return None


# ══════════════════════════════════════════════
# 3.  HTML Cleaning & Text Extraction
# ══════════════════════════════════════════════
def clean_and_extract(html: str) -> tuple[str, str]:
    """
    Strip boilerplate tags, return (title, cleaned_body_text).
    """
    soup = BeautifulSoup(html, "lxml")

    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else "Untitled"

    for tag_name in BOILERPLATE_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    text = soup.get_text(separator="\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = text.strip()

    return title, text


# ══════════════════════════════════════════════
# 4.  Chunking
# ══════════════════════════════════════════════
def chunk_text(text: str) -> list[str]:
    """Split text into token-counted chunks using LangChain."""
    splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        encoding_name="cl100k_base",
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
    )
    return splitter.split_text(text)


# ══════════════════════════════════════════════
# 5.  Embedding
# ══════════════════════════════════════════════
def embed_chunks(client: genai.Client, chunks: list[str]) -> list[list[float]]:
    """
    Generate embeddings in batches.
    Returns a flat list of embedding vectors aligned with `chunks`.
    """
    all_embeddings: list[list[float]] = []

    for i in range(0, len(chunks), EMBEDDING_BATCH_SIZE):
        batch  = chunks[i : i + EMBEDDING_BATCH_SIZE]
        result = client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=batch,
            config={"output_dimensionality": EMBEDDING_DIMENSION},
        )
        all_embeddings.extend([e.values for e in result.embeddings])

    return all_embeddings


# ══════════════════════════════════════════════
# 6.  Firestore Operations
# ══════════════════════════════════════════════
def _url_id_prefix(url: str) -> str:
    """Deterministic, short prefix for a URL used in document IDs."""
    return hashlib.md5(url.encode()).hexdigest()[:12]


def delete_old_vectors(collection: Any, url: str) -> int:
    """
    Delete all existing documents for a URL using batched Firestore writes.
    Much faster than one-at-a-time deletes.
    Returns the count of deleted documents.
    """
    url_hash = _url_id_prefix(url)
    docs     = list(collection.where("url_hash", "==", url_hash).stream())

    if not docs:
        return 0

    deleted = 0
    batch   = collection._client.batch()

    for i, doc in enumerate(docs):
        batch.delete(doc.reference)
        deleted += 1
        # Firestore batch limit is 500 writes; commit and start a fresh batch
        if (i + 1) % 499 == 0:
            batch.commit()
            batch = collection._client.batch()

    batch.commit()
    return deleted


def upsert_vectors(
    collection: Any,
    url: str,
    title: str,
    chunks: list[str],
    embeddings: list[list[float]],
) -> int:
    """
    Upsert chunk documents with embeddings to Firestore.
    Returns count of upserted documents.
    """
    url_hash = _url_id_prefix(url)
    now_utc  = datetime.now(timezone.utc).isoformat()
    batch    = collection._client.batch()

    for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
        doc_id  = f"{url_hash}#{i}"
        doc_ref = collection.document(doc_id)
        batch.set(doc_ref, {
            "url":          url,
            "title":        title,
            "text":         chunk,
            "last_crawled": now_utc,
            "url_hash":     url_hash,
            "chunk_index":  i,
            "embedding":    Vector(emb),
        })

        # Firestore batch limit is 500 writes
        if (i + 1) % 499 == 0:
            batch.commit()
            batch = collection._client.batch()

    batch.commit()
    return len(chunks)


# ══════════════════════════════════════════════
# 7.  URL Loader
# ══════════════════════════════════════════════
def load_urls(path: Path = URLS_FILE) -> list[str]:
    """Read URLs from a text file (one per line, # comments allowed)."""
    if not path.exists():
        log.error("URL file not found: %s", path)
        return []
    urls: list[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


def load_discovered_urls(path: Path = DISCOVERED_URLS_FILE) -> dict[str, dict[str, Any]]:
    """
    Load URLs from the crawler's discovered_urls.json output.

    Returns a dict mapping url → {text, title, ...} so the ETL pipeline
    can skip re-fetching pages that were already captured during crawling.
    Only non-thin pages with cached text are included.
    """
    if not path.exists():
        log.error("Discovered URLs file not found: %s", path)
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    url_entries: dict[str, Any] = data.get("urls", {})
    return url_entries


# ══════════════════════════════════════════════
# 8.  Per-URL Worker
# ══════════════════════════════════════════════
def _process_url(
    url:          str,
    cached_meta:  dict[str, Any] | None,
    state:        StateManager,
    genai_client: genai.Client | None,
    fs_collection: Any,
    dry_run:      bool,
    idx:          int,
    total:        int,
) -> dict[str, Any]:
    """
    Process a single URL through the full ETL pipeline.
    Safe to call from multiple threads simultaneously.

    Returns a stats dict: {"status": "updated"|"skipped"|"failed", "vectors": int}
    """
    log.info("[%d/%d] Processing: %s", idx, total, url)

    # ── Fetch or use cached text ─────────────────────────────────────────────
    cached_text  = (cached_meta or {}).get("text") or ""
    cached_title = (cached_meta or {}).get("title") or ""

    if cached_text:
        # Crawler already fetched this page — skip the network round-trip
        title = cached_title or "Untitled"
        text  = cached_text
        log.info("  -> Using cached text from crawler (%d words)", len(text.split()))
    else:
        html = fetch_page(url)
        if html is None:
            return {"status": "failed", "vectors": 0}
        title, text = clean_and_extract(html)

    # ── Dedup check ──────────────────────────────────────────────────────────
    content_hash = StateManager.compute_hash(text)
    if not state.has_changed(url, content_hash):
        log.info("  -> No Change -- skipping (hash match)")
        return {"status": "skipped", "vectors": 0}

    if not text:
        log.warning("  -> No extractable text -- skipping")
        return {"status": "failed", "vectors": 0}

    # ── Chunk ────────────────────────────────────────────────────────────────
    chunks = chunk_text(text)
    log.info("  -> Title: %s | Chunks: %d", title, len(chunks))

    if dry_run:
        log.info("  -> [DRY-RUN] Would embed %d chunks & upsert to Firestore", len(chunks))
        state.update(url, content_hash)
        return {"status": "updated", "vectors": 0}

    # ── Embed ────────────────────────────────────────────────────────────────
    assert genai_client is not None
    try:
        embeddings = embed_chunks(genai_client, chunks)
    except Exception as exc:
        log.error("  -> Embedding failed: %s -- skipping", exc)
        return {"status": "failed", "vectors": 0}

    # ── Firestore write ──────────────────────────────────────────────────────
    assert fs_collection is not None
    try:
        deleted = delete_old_vectors(fs_collection, url)
        if deleted:
            log.info("  -> Deleted %d stale document(s)", deleted)

        count = upsert_vectors(fs_collection, url, title, chunks, embeddings)
        log.info("  -> Upserted %d document(s)", count)
    except Exception as exc:
        log.error("  -> Firestore operation failed: %s -- skipping", exc)
        return {"status": "failed", "vectors": 0}

    # ── Persist hash ─────────────────────────────────────────────────────────
    state.update(url, content_hash)
    return {"status": "updated", "vectors": count}


# ══════════════════════════════════════════════
# 9.  Main Pipeline
# ══════════════════════════════════════════════
def run_pipeline(dry_run: bool = False, use_crawler: bool = False) -> None:
    """Execute the full ETL pipeline with parallel URL processing."""
    log.info("=" * 60)
    log.info("OSU RAG ETL Pipeline -- starting run (workers=%d)", ETL_MAX_WORKERS)
    log.info("=" * 60)

    # --- crawl (if requested) ---
    url_cache: dict[str, dict[str, Any]] = {}
    if use_crawler:
        from crawler import crawl
        crawl()
        url_cache = load_discovered_urls()
        urls      = list(url_cache.keys())
        source    = DISCOVERED_URLS_FILE
    else:
        urls   = load_urls()
        source = URLS_FILE

    if not urls:
        log.warning("No URLs to process. Add URLs to %s", source)
        return
    log.info("Loaded %d URL(s) from %s", len(urls), source)

    # --- init state manager ---
    state = StateManager()

    # --- init external clients (skip in dry-run) ---
    genai_client:  genai.Client | None = None
    fs_collection: Any                 = None

    if not dry_run:
        import os

        google_api_key     = os.environ.get("GOOGLE_API_KEY")
        gcp_project_id     = os.environ.get("GCP_PROJECT_ID")
        firestore_collection = os.environ.get("FIRESTORE_COLLECTION", "osu-knowledge")

        if not google_api_key:
            log.error("GOOGLE_API_KEY not set. Aborting.")
            sys.exit(1)
        if not gcp_project_id:
            log.error("GCP_PROJECT_ID not set. Aborting.")
            sys.exit(1)

        genai_client  = genai.Client(api_key=google_api_key)
        fs_client     = firestore.Client(project=gcp_project_id)
        fs_collection = fs_client.collection(firestore_collection)
        log.info("Connected to Firestore collection: %s", firestore_collection)

    # --- parallel processing ---
    stats = {"skipped": 0, "updated": 0, "failed": 0, "vectors_upserted": 0}
    total = len(urls)

    with ThreadPoolExecutor(max_workers=ETL_MAX_WORKERS) as executor:
        future_to_url = {
            executor.submit(
                _process_url,
                url,
                url_cache.get(url),    # pass cached text/title if available
                state,
                genai_client,
                fs_collection,
                dry_run,
                idx,
                total,
            ): url
            for idx, url in enumerate(urls, 1)
        }

        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                result = future.result()
            except Exception as exc:
                log.error("Unhandled error processing %s: %s", url, exc)
                result = {"status": "failed", "vectors": 0}

            stats[result["status"]] += 1
            stats["vectors_upserted"] += result.get("vectors", 0)

    # --- save state ---
    state.save()
    log.info("State saved to %s", HASH_STORE_PATH)

    # --- summary ---
    log.info("=" * 60)
    log.info(
        "Done -- Updated: %d | Skipped (no change): %d | Failed: %d | Vectors upserted: %d",
        stats["updated"],
        stats["skipped"],
        stats["failed"],
        stats["vectors_upserted"],
    )
    log.info("=" * 60)


# ══════════════════════════════════════════════
# CLI Entry Point
# ══════════════════════════════════════════════
def main() -> None:
    parser = argparse.ArgumentParser(description="OSU RAG ETL Pipeline")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the pipeline without calling external APIs (embedding & Firestore).",
    )
    parser.add_argument(
        "--crawl",
        action="store_true",
        help="Run the BFS web crawler first to discover URLs before processing.",
    )
    args = parser.parse_args()
    run_pipeline(dry_run=args.dry_run, use_crawler=args.crawl)


if __name__ == "__main__":
    main()
