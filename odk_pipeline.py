#!/usr/bin/env python3
"""
ODK Document + Web Ingestion Pipeline
======================================
Two ingest paths, both writing to the Firestore 'odk-knowledge' collection
with a `source_type` field so the ODK agent searches a single collection:

  source_type = "document"  — uploaded PDFs / text files from the dashboard
  source_type = "web"       — pages crawled from the department's URL subtree

Document path (CLI):
    python odk_pipeline.py --file /path/to/doc.pdf \
                           --department-url advantage.oregonstate.edu/startups/

Web crawl path (CLI):
    python odk_pipeline.py --crawl-url advantage.oregonstate.edu/startups/ \
                           --max-pages 50

Both paths at once (what the dashboard does on Accept):
    python odk_pipeline.py --file /path/to/doc.pdf \
                           --department-url advantage.oregonstate.edu/startups/ \
                           --crawl-url advantage.oregonstate.edu/startups/

Environment variables (same .env as the ETL pipeline):
    GOOGLE_API_KEY, GCP_PROJECT_ID
    ODK_FIRESTORE_COLLECTION   (default: odk-knowledge)
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests as http_requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from langchain_text_splitters import RecursiveCharacterTextSplitter

load_dotenv()

log = logging.getLogger("odk-pipeline")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)

EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMENSION = 768
EMBEDDING_BATCH_SIZE = 100
CHUNK_SIZE = 512
CHUNK_OVERLAP = 64
ODK_COLLECTION = os.environ.get("ODK_FIRESTORE_COLLECTION", "odk-knowledge")

BOILERPLATE_TAGS = ["nav", "footer", "header", "script", "style", "noscript", "aside", "form", "iframe"]
SKIP_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".zip", ".docx", ".xlsx", ".pptx", ".mp4", ".mp3"}
CRAWL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (ODK-Crawler/1.0; +https://openbeavs.oregonstate.edu)",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Rate-limiting defaults (overridable via env vars)
CRAWL_DELAY = float(os.environ.get("ODK_CRAWL_DELAY", "1.0"))
CRAWL_JITTER = float(os.environ.get("ODK_CRAWL_JITTER", "0.5"))
CRAWL_MAX_DELAY = float(os.environ.get("ODK_CRAWL_MAX_DELAY", "30.0"))

# URL path substrings that indicate non-content pages — skip to avoid crawling
# noise and wasting crawl budget on 403-heavy paths.
SKIP_URL_PATTERNS = {
    "/tags/", "/tag/", "/topic/", "/author/", "/tncms/", "/boilerplate/",
    "/partners/video-elephant/", "/search/", "/rss-", "/subscribe", "/eedition",
    "/weather", "/watch", "/events", "/archive", "/contacts", "/journalists",
    "/faculty-and-staff", "/news/local/", "/news/nation-world/", "/news/wildfires/",
    "/news/video_", "/sports/", "/corvallis/news/", "/article_", "/gallery/",
    "/organization/", "/en/search/",
}


# ══════════════════════════════════════════════
# Shared utilities
# ══════════════════════════════════════════════

def _normalize_dept(url: str) -> str:
    """Strip scheme and trailing slash: 'advantage.oregonstate.edu/startups'."""
    return url.lower().replace("https://", "").replace("http://", "").rstrip("/")


def _doc_id_prefix(department_url: str, key: str) -> str:
    """Stable 12-char prefix for a (department, key) pair used in Firestore doc IDs."""
    return hashlib.md5(f"{department_url}::{key}".encode()).hexdigest()[:12]


def chunk_text(text: str) -> list[str]:
    """Split text into token-counted chunks."""
    splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        encoding_name="cl100k_base",
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
    )
    return splitter.split_text(text)


def embed_chunks(client: Any, chunks: list[str]) -> list[list[float]]:
    """Generate embeddings in batches using gemini-embedding-001."""
    all_embeddings: list[list[float]] = []
    for i in range(0, len(chunks), EMBEDDING_BATCH_SIZE):
        batch = chunks[i: i + EMBEDDING_BATCH_SIZE]
        result = client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=batch,
            config={"output_dimensionality": EMBEDDING_DIMENSION},
        )
        all_embeddings.extend([e.values for e in result.embeddings])
    return all_embeddings


# ══════════════════════════════════════════════
# Firestore operations
# ══════════════════════════════════════════════

def delete_old_vectors(collection: Any, department_url: str, key: str) -> int:
    """Delete all existing chunks for a (department_url, key) pair."""
    prefix = _doc_id_prefix(department_url, key)
    docs = list(collection.where("doc_prefix", "==", prefix).stream())
    if not docs:
        return 0

    deleted = 0
    batch = collection._client.batch()
    for i, doc in enumerate(docs):
        batch.delete(doc.reference)
        deleted += 1
        if (i + 1) % 499 == 0:
            batch.commit()
            batch = collection._client.batch()
    batch.commit()
    return deleted


def upsert_vectors(
    collection: Any,
    department_url: str,
    key: str,
    chunks: list[str],
    embeddings: list[list[float]],
    source_type: str = "document",
    source_url: str | None = None,
    title: str | None = None,
) -> int:
    """Upsert chunk documents into the ODK Firestore collection.

    Args:
        key:         Unique key for this source within the department.
                     For documents this is the filename; for web pages it's the page URL.
        source_type: "document" or "web".
        source_url:  Original URL (web pages only).
        title:       Page or document title.
    """
    from google.cloud.firestore_v1.vector import Vector

    prefix = _doc_id_prefix(department_url, key)
    now_utc = datetime.now(timezone.utc).isoformat()
    batch = collection._client.batch()

    for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
        doc_ref = collection.document(f"{prefix}#{i}")
        doc = {
            "department_url": department_url,
            "source_type": source_type,
            "doc_prefix": prefix,
            "text": chunk,
            "chunk_index": i,
            "indexed_at": now_utc,
            "embedding": Vector(emb),
        }
        if source_type == "document":
            doc["source_file"] = key
        else:
            doc["url"] = source_url or key
            doc["title"] = title or key

        batch.set(doc_ref, doc)
        if (i + 1) % 499 == 0:
            batch.commit()
            batch = collection._client.batch()

    batch.commit()
    return len(chunks)


def _init_clients() -> tuple[Any, Any]:
    """Return (genai_client, firestore_collection) or raise RuntimeError."""
    google_api_key = os.environ.get("GOOGLE_API_KEY")
    gcp_project_id = os.environ.get("GCP_PROJECT_ID")
    if not google_api_key or not gcp_project_id:
        raise RuntimeError("GOOGLE_API_KEY and GCP_PROJECT_ID must be set in the environment.")

    from google import genai
    from google.cloud import firestore

    genai_client = genai.Client(api_key=google_api_key)
    fs_client = firestore.Client(project=gcp_project_id)
    return genai_client, fs_client.collection(ODK_COLLECTION)


# ══════════════════════════════════════════════
# Path 1: Document ingestion
# ══════════════════════════════════════════════

def extract_text_from_file(file_path: Path) -> str:
    """Extract plain text from a PDF or text file."""
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(file_path)
    elif suffix in (".txt", ".md", ".rst"):
        return file_path.read_text(encoding="utf-8", errors="ignore")
    raise ValueError(f"Unsupported file type '{suffix}'. Supported: .pdf, .txt, .md, .rst")


def _extract_pdf(path: Path) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf:
            return "\n\n".join(p.extract_text() or "" for p in pdf.pages).strip()
    except ImportError:
        pass
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages).strip()
    except ImportError:
        pass
    raise ImportError("PDF extraction requires pdfplumber or pypdf. Install: pip install pdfplumber")


def ingest_document(
    file_path: str | Path,
    department_url: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Index a single uploaded document into odk-knowledge with source_type='document'."""
    path = Path(file_path)
    if not path.exists():
        return {"status": "error", "message": f"File not found: {path}"}

    dept = _normalize_dept(department_url)
    filename = path.name
    log.info("ODK doc ingest | file=%s | department=%s", filename, dept)

    try:
        text = extract_text_from_file(path)
    except Exception as exc:
        return {"status": "error", "message": f"Text extraction failed: {exc}"}

    if not text.strip():
        return {"status": "error", "message": "No extractable text in file."}

    chunks = chunk_text(text)
    log.info("  %d chunks from %d chars", len(chunks), len(text))

    if dry_run:
        return {"status": "ok", "chunks": len(chunks), "vectors_upserted": 0, "message": "dry-run"}

    try:
        genai_client, collection = _init_clients()
    except Exception as exc:
        return {"status": "error", "message": f"Client init failed: {exc}"}

    try:
        embeddings = embed_chunks(genai_client, chunks)
    except Exception as exc:
        return {"status": "error", "message": f"Embedding failed: {exc}"}

    deleted = delete_old_vectors(collection, dept, filename)
    if deleted:
        log.info("  Deleted %d stale document vectors", deleted)

    try:
        count = upsert_vectors(collection, dept, filename, chunks, embeddings, source_type="document")
        log.info("  Upserted %d document vectors", count)
    except Exception as exc:
        return {"status": "error", "message": f"Firestore upsert failed: {exc}"}

    return {"status": "ok", "chunks": len(chunks), "vectors_upserted": count, "message": f"Indexed {filename}"}


# ══════════════════════════════════════════════
# Path 2: Web crawl ingestion
# ══════════════════════════════════════════════

def crawl_department_subtree(base_url: str, max_pages: int = 50) -> list[dict[str, str]]:
    """BFS-crawl all pages within the department's URL subtree.

    Only follows links that stay within the same host + path prefix.
    Skips binary files, thin pages (<50 words), and already-visited URLs.

    Returns:
        List of {url, title, text} dicts, one per crawled page.
    """
    if not base_url.startswith("http"):
        base_url = "https://" + base_url

    # Normalise the base prefix for subtree filtering
    parsed_base = urlparse(base_url)
    base_prefix = f"{parsed_base.scheme}://{parsed_base.netloc}{parsed_base.path}".rstrip("/")

    visited: set[str] = set()
    queue: list[str] = [base_url]
    results: list[dict[str, str]] = []
    current_delay = CRAWL_DELAY  # adaptive — doubles on 4xx, resets on success

    log.info("ODK web crawl | base=%s | max_pages=%d | delay=%.1fs±%.1fs",
             base_url, max_pages, CRAWL_DELAY, CRAWL_JITTER)

    while queue and len(results) < max_pages:
        url = queue.pop(0)
        canonical = url.split("?")[0].split("#")[0].rstrip("/")
        if canonical in visited:
            continue
        visited.add(canonical)

        # Skip non-HTML file extensions
        path_lower = urlparse(url).path.lower()
        if any(path_lower.endswith(ext) for ext in SKIP_EXTENSIONS):
            continue

        # Skip known noise paths (tag pages, author archives, news articles, etc.)
        if any(pat in url for pat in SKIP_URL_PATTERNS):
            log.debug("  skip pattern match %s", url)
            continue

        # Polite delay with jitter — never hammer the server
        sleep_time = current_delay + random.uniform(-CRAWL_JITTER, CRAWL_JITTER)
        time.sleep(max(0.1, sleep_time))

        try:
            resp = http_requests.get(url, headers=CRAWL_HEADERS, timeout=15)
            resp.raise_for_status()
            if "text/html" not in resp.headers.get("content-type", ""):
                current_delay = max(CRAWL_DELAY, current_delay * 0.75)  # ease back after non-html
                continue
        except http_requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status in (403, 429, 503):
                # Rate-limit signal: back off exponentially
                current_delay = min(current_delay * 2, CRAWL_MAX_DELAY)
                log.warning("  Fetch %d %s — delay now %.1fs", status, url, current_delay)
            else:
                log.warning("  Fetch failed %s: %s", url, exc)
            continue
        except Exception as exc:
            log.warning("  Fetch failed %s: %s", url, exc)
            continue

        # Successful fetch — slowly ease back toward baseline
        current_delay = max(CRAWL_DELAY, current_delay * 0.9)

        soup = BeautifulSoup(resp.text, "lxml")

        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else url

        for tag in soup.find_all(BOILERPLATE_TAGS):
            tag.decompose()

        text = re.sub(r"\n{3,}", "\n\n", soup.get_text(separator="\n")).strip()
        word_count = len(text.split())

        if word_count >= 50:
            results.append({"url": url, "title": title, "text": text})
            log.info("  + %s (%d words)", url, word_count)
        else:
            log.info("  ~ thin page skipped %s (%d words)", url, word_count)

        # Enqueue in-subtree links
        for a_tag in soup.find_all("a", href=True):
            href = str(a_tag["href"]).strip()
            if href.startswith(("mailto:", "tel:", "javascript:")):
                continue
            full = urljoin(url, href).split("#")[0].rstrip("/")
            full_canonical = full.split("?")[0]
            if (
                full_canonical not in visited
                and full not in queue
                and full_canonical.startswith(base_prefix)
            ):
                queue.append(full)

    log.info("Crawl done: %d pages collected for %s", len(results), base_url)
    return results


def crawl_and_index_department(
    department_url: str,
    max_pages: int = 50,
    dry_run: bool = False,
    on_page_indexed=None,
) -> dict[str, Any]:
    """Crawl the department's public URL subtree and index every page into odk-knowledge.

    Each page is stored with source_type='web' and department_url set, so the
    ODK agent finds both web content and uploaded documents in a single search.

    Args:
        department_url:   The department's oregonstate.edu URL subtree.
        max_pages:        Maximum number of pages to crawl (default 50).
        dry_run:          If True, crawl but skip embedding and Firestore writes.
        on_page_indexed:  Optional callback(url, title, word_count, chunks, vectors, status)
                          called after each page is processed.
    """
    dept = _normalize_dept(department_url)
    pages = crawl_department_subtree(department_url, max_pages=max_pages)

    if not pages:
        return {"status": "ok", "pages": 0, "vectors_upserted": 0, "message": "No crawlable pages found."}

    if dry_run:
        total_chunks = sum(len(chunk_text(p["text"])) for p in pages)
        return {"status": "ok", "pages": len(pages), "vectors_upserted": 0,
                "message": f"dry-run: {len(pages)} pages, ~{total_chunks} chunks"}

    try:
        genai_client, collection = _init_clients()
    except Exception as exc:
        return {"status": "error", "message": f"Client init failed: {exc}"}

    total_vectors = 0
    for page in pages:
        url = page["url"]
        title = page["title"]
        text = page["text"]
        word_count = len(text.split())

        chunks = chunk_text(text)
        if not chunks:
            if on_page_indexed:
                on_page_indexed(url, title, word_count, 0, 0, "skipped")
            continue

        try:
            embeddings = embed_chunks(genai_client, chunks)
        except Exception as exc:
            log.error("  Embedding failed for %s: %s", url, exc)
            if on_page_indexed:
                on_page_indexed(url, title, word_count, len(chunks), 0, "failed")
            continue

        deleted = delete_old_vectors(collection, dept, url)
        if deleted:
            log.info("  Deleted %d stale web vectors for %s", deleted, url)

        try:
            count = upsert_vectors(
                collection, dept, url, chunks, embeddings,
                source_type="web", source_url=url, title=title,
            )
            total_vectors += count
            log.info("  Upserted %d vectors for %s", count, url)
            if on_page_indexed:
                on_page_indexed(url, title, word_count, len(chunks), count, "indexed")
        except Exception as exc:
            log.error("  Firestore upsert failed for %s: %s", url, exc)
            if on_page_indexed:
                on_page_indexed(url, title, word_count, len(chunks), 0, "failed")

    return {
        "status": "ok",
        "pages": len(pages),
        "vectors_upserted": total_vectors,
        "message": f"Indexed {len(pages)} web pages for {dept}",
    }


# ══════════════════════════════════════════════
# CLI Entry Point
# ══════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="ODK Document + Web Ingestion Pipeline")
    parser.add_argument("--file", help="Path to the document (PDF, txt, md)")
    parser.add_argument("--department-url", help="Department URL subtree (required with --file)")
    parser.add_argument("--crawl-url", help="URL subtree to crawl and index as web content")
    parser.add_argument("--max-pages", type=int, default=50, help="Max pages to crawl (default: 50)")
    parser.add_argument("--dry-run", action="store_true", help="Skip embedding and Firestore calls")
    args = parser.parse_args()

    if not args.file and not args.crawl_url:
        parser.error("Provide --file, --crawl-url, or both.")

    if args.file:
        if not args.department_url:
            parser.error("--department-url is required with --file")
        result = ingest_document(args.file, args.department_url, dry_run=args.dry_run)
        if result["status"] == "ok":
            log.info("Document done: %s", result["message"])
        else:
            log.error("Document failed: %s", result["message"])
            sys.exit(1)

    if args.crawl_url:
        result = crawl_and_index_department(
            args.crawl_url, max_pages=args.max_pages, dry_run=args.dry_run
        )
        if result["status"] == "ok":
            log.info("Crawl done: %s", result["message"])
        else:
            log.error("Crawl failed: %s", result["message"])
            sys.exit(1)


if __name__ == "__main__":
    main()
