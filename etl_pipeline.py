#!/usr/bin/env python3
"""
OSU RAG ETL Pipeline
====================
Scrapes *.oregonstate.edu pages, chunks the text, generates embeddings via
Google GenAI (text-embedding-004), and upserts them into Pinecone.

Designed to run as a cron job every 6 hours.  Content-hash deduplication
ensures only changed pages are re-processed, and stale vectors are deleted
before new ones are upserted.

Usage:
    python etl_pipeline.py                 # full run
    python etl_pipeline.py --dry-run       # skip embedding & Pinecone calls

Environment variables (see .env.example):
    GOOGLE_API_KEY, PINECONE_API_KEY, PINECONE_INDEX_NAME
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pinecone import Pinecone

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent
HASH_STORE_PATH = ROOT_DIR / "url_hashes.json"
URLS_FILE = ROOT_DIR / "urls.txt"

EMBEDDING_MODEL = "models/text-embedding-004"
EMBEDDING_DIMENSION = 768
EMBEDDING_BATCH_SIZE = 100          # Google GenAI max per request

CHUNK_SIZE = 512                    # tokens
CHUNK_OVERLAP = 64                  # tokens

REQUEST_TIMEOUT = 30                # seconds
MAX_RETRIES = 3
RETRY_BACKOFF = 2                   # seconds (exponential base)

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


# ══════════════════════════════════════════════
# 1.  State Management (Deduplication)
# ══════════════════════════════════════════════
class StateManager:
    """Persists `{url: content_hash}` to a local JSON file."""

    def __init__(self, path: Path = HASH_STORE_PATH) -> None:
        self.path = path
        self.hashes: dict[str, str] = self._load()

    # --- persistence ---
    def _load(self) -> dict[str, str]:
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.hashes, f, indent=2)

    # --- hash helpers ---
    @staticmethod
    def compute_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def has_changed(self, url: str, content_hash: str) -> bool:
        return self.hashes.get(url) != content_hash

    def update(self, url: str, content_hash: str) -> None:
        self.hashes[url] = content_hash


# ══════════════════════════════════════════════
# 2.  Web Fetching
# ══════════════════════════════════════════════
def fetch_page(url: str) -> str | None:
    """Fetch raw HTML with retries and exponential backoff."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url,
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": "OSU-RAG-Bot/1.0 (+oregonstate.edu)"},
            )
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            wait = RETRY_BACKOFF ** attempt
            log.warning(
                "Fetch attempt %d/%d failed for %s: %s -- retrying in %ds",
                attempt, MAX_RETRIES, url, exc, wait,
            )
            time.sleep(wait)
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

    # Extract title
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else "Untitled"

    # Remove boilerplate subtrees
    for tag_name in BOILERPLATE_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # Collapse whitespace
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
        batch = chunks[i : i + EMBEDDING_BATCH_SIZE]
        result = client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=batch,
        )
        all_embeddings.extend([e.values for e in result.embeddings])

    return all_embeddings


# ══════════════════════════════════════════════
# 6.  Pinecone Operations
# ══════════════════════════════════════════════
def _url_id_prefix(url: str) -> str:
    """Deterministic, short prefix for a URL used in vector IDs."""
    return hashlib.md5(url.encode()).hexdigest()[:12]


def delete_old_vectors(index: Any, url: str) -> int:
    """
    Delete all existing vectors for a URL using ID-prefix listing.
    Returns the count of deleted vectors.
    """
    prefix = _url_id_prefix(url)
    deleted = 0

    # Paginate through all matching IDs
    for ids_batch in index.list(prefix=prefix):
        if ids_batch:
            index.delete(ids=ids_batch)
            deleted += len(ids_batch)

    return deleted


def upsert_vectors(
    index: Any,
    url: str,
    title: str,
    chunks: list[str],
    embeddings: list[list[float]],
) -> int:
    """
    Upsert chunk vectors with metadata.  Returns count of upserted vectors.
    """
    prefix = _url_id_prefix(url)
    now_utc = datetime.now(timezone.utc).isoformat()

    vectors = [
        {
            "id": f"{prefix}#{i}",
            "values": emb,
            "metadata": {
                "url": url,
                "title": title,
                "last_crawled": now_utc,
                "text": chunk,           # store chunk text for retrieval
            },
        }
        for i, (chunk, emb) in enumerate(zip(chunks, embeddings))
    ]

    # Pinecone supports batches of up to 100 vectors
    UPSERT_BATCH = 100
    for i in range(0, len(vectors), UPSERT_BATCH):
        index.upsert(vectors=vectors[i : i + UPSERT_BATCH])

    return len(vectors)


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


# ══════════════════════════════════════════════
# 8.  Main Pipeline
# ══════════════════════════════════════════════
def run_pipeline(dry_run: bool = False) -> None:
    """Execute the full ETL pipeline."""
    log.info("=" * 60)
    log.info("OSU RAG ETL Pipeline -- starting run")
    log.info("=" * 60)

    # --- load URLs ---
    urls = load_urls()
    if not urls:
        log.warning("No URLs to process. Add URLs to %s", URLS_FILE)
        return
    log.info("Loaded %d URL(s) from %s", len(urls), URLS_FILE)

    # --- init state manager ---
    state = StateManager()

    # --- init external clients (skip in dry-run) ---
    genai_client: genai.Client | None = None
    pc_index: Any = None

    if not dry_run:
        import os

        google_api_key = os.environ.get("GOOGLE_API_KEY")
        pinecone_api_key = os.environ.get("PINECONE_API_KEY")
        pinecone_index_name = os.environ.get("PINECONE_INDEX_NAME", "osu-knowledge")

        if not google_api_key:
            log.error("GOOGLE_API_KEY not set. Aborting.")
            sys.exit(1)
        if not pinecone_api_key:
            log.error("PINECONE_API_KEY not set. Aborting.")
            sys.exit(1)

        genai_client = genai.Client(api_key=google_api_key)
        pc = Pinecone(api_key=pinecone_api_key)
        pc_index = pc.Index(pinecone_index_name)
        log.info("Connected to Pinecone index: %s", pinecone_index_name)

    # --- process each URL ---
    stats = {"skipped": 0, "updated": 0, "failed": 0, "vectors_upserted": 0}

    for i, url in enumerate(urls, 1):
        log.info("[%d/%d] Processing: %s", i, len(urls), url)

        # Fetch
        html = fetch_page(url)
        if html is None:
            stats["failed"] += 1
            continue

        # Dedup check
        content_hash = StateManager.compute_hash(html)
        if not state.has_changed(url, content_hash):
            log.info("  -> No Change -- skipping (hash match)")
            stats["skipped"] += 1
            continue

        # Clean & chunk
        title, text = clean_and_extract(html)
        if not text:
            log.warning("  -> No extractable text -- skipping")
            stats["failed"] += 1
            continue

        chunks = chunk_text(text)
        log.info("  -> Title: %s | Chunks: %d", title, len(chunks))

        if dry_run:
            log.info("  -> [DRY-RUN] Would embed %d chunks & upsert to Pinecone", len(chunks))
            state.update(url, content_hash)
            stats["updated"] += 1
            continue

        # Embed
        assert genai_client is not None
        try:
            embeddings = embed_chunks(genai_client, chunks)
        except Exception as exc:
            log.error("  -> Embedding failed: %s -- skipping", exc)
            stats["failed"] += 1
            continue

        # Delete old vectors, then upsert new ones
        assert pc_index is not None
        try:
            deleted = delete_old_vectors(pc_index, url)
            if deleted:
                log.info("  -> Deleted %d stale vector(s)", deleted)

            count = upsert_vectors(pc_index, url, title, chunks, embeddings)
            log.info("  -> Upserted %d vector(s)", count)
            stats["vectors_upserted"] += count
        except Exception as exc:
            log.error("  -> Pinecone operation failed: %s -- skipping", exc)
            stats["failed"] += 1
            continue

        # Persist hash
        state.update(url, content_hash)

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
        help="Run the pipeline without calling external APIs (embedding & Pinecone).",
    )
    args = parser.parse_args()
    run_pipeline(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
