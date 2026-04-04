# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

An ETL pipeline that crawls `*.oregonstate.edu`, chunks the content, generates embeddings via Google GenAI (`gemini-embedding-001`, 768d), and upserts them into **Firestore** with vector search. A companion Google ADK agent (`osu_rag_agent/`) serves the knowledge base over the A2A protocol.

## Setup

```bash
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt                    # ETL pipeline
pip install -r agent_requirements.txt             # RAG agent (separate)
cp .env.example .env                              # fill in GOOGLE_API_KEY, GCP_PROJECT_ID
gcloud auth application-default login
```

**Required env vars** (`.env`):
- `GOOGLE_API_KEY` — Google AI Studio key
- `GCP_PROJECT_ID` — GCP project with Firestore in Native mode
- `FIRESTORE_COLLECTION` — defaults to `osu-knowledge`

## Commands

```bash
# ETL pipeline
python etl_pipeline.py                   # process urls.txt only
python etl_pipeline.py --crawl           # BFS crawl first, then embed
python etl_pipeline.py --dry-run         # no API calls (test chunking logic)
python etl_pipeline.py --crawl --dry-run

# Crawler standalone
python crawler.py                        # outputs discovered_urls.json

# RAG agent dev UI (run from project root)
adk web                                  # Windows: adk web --no-reload
adk api_server --a2a                     # serve A2A endpoint at :8000

# Deploy agent to Cloud Run
python deploy_agent.py osu_rag_agent
python deploy_agent.py osu_rag_agent --project my-project --region us-west1
```

## Architecture

### ETL Pipeline (`etl_pipeline.py`)

Nine-stage pipeline running with `ThreadPoolExecutor` (`ETL_MAX_WORKERS`, default 10):

1. **CRAWL** (optional) — calls `crawler.crawl()`, writes `discovered_urls.json` with cached text/title
2. **FETCH** — downloads HTML (skipped if crawler cached the text)
3. **DEDUP** — SHA256 hash of extracted text checked against `url_hashes.json`; unchanged pages are skipped entirely
4. **CLEAN** — strips `nav/footer/header/script/style/noscript/aside/form/iframe` via BeautifulSoup
5. **CHUNK** — `RecursiveCharacterTextSplitter` with tiktoken `cl100k_base`, 512-token chunks, 64-token overlap
6. **EMBED** — batched calls to `gemini-embedding-001` (100 chunks/request), 768-dim output
7. **DELETE** — batched Firestore deletes of stale docs for changed URLs (`url_hash` field query, 499 ops/batch)
8. **UPSERT** — writes `{url, title, text, last_crawled, url_hash, chunk_index, embedding: Vector(...)}` (499 ops/batch)
9. **STATE** — persists updated hashes to `url_hashes.json`

Key class: `StateManager` — thread-safe `{url → sha256}` dict backed by `url_hashes.json`, protected by `threading.Lock`.

Document ID format: `{md5(url)[:12]}#{chunk_index}` — enables prefix-based stale-vector cleanup.

### Crawler (`crawler.py`)

Parallel BFS using `ThreadPoolExecutor` (`CRAWL_MAX_WORKERS`, default 20) with per-domain rate limiting (`DomainRateLimiter`). Key behaviors:

- Scoped to `*.oregonstate.edu` (configurable via `BASE_URL_TO_SCRAPE`)
- Skips non-HTML extensions, low-quality URL slugs (numeric IDs ≥4 digits, UUIDs, hex hashes, paths ≥8 segments)
- "Thin" pages (<80 words) are recorded but their outbound links are not enqueued
- Exclusion patterns loaded from `url_exclusions.txt` and `CRAWL_EXCLUDE_PATTERNS` env var
- Robots.txt checked per domain with caching; domains that block root are assumed allow-all (login redirect detection)
- Outputs `discovered_urls.json` — the ETL pipeline reads this to skip re-fetching crawled pages

### RAG Agent (`osu_rag_agent/`)

Google ADK agent (`google-adk[a2a]`) with one tool: `search_osu_knowledge(query, top_k=5)`.

- Embeds the query with `gemini-embedding-001` and runs `collection.find_nearest()` (cosine distance) against Firestore
- Model: `gemini-2.5-flash`
- A2A agent card at `osu_rag_agent/agent.json`; deployed card URL: `https://osu-rag-agent-716080272371.us-west1.run.app/a2a/osu_rag_agent`
- Lazy-initialized clients (`_genai_client`, `_fs_collection`) to avoid cold-start overhead

### Deployment (`deploy_agent.py`)

Wraps `gcloud run deploy --source=<agent_dir>` with auto-detected project/region from `gcloud config`. Always deploys with `--allow-unauthenticated` (required for A2A agent card discovery). Re-applies `allUsers` IAM binding after deploy since `gcloud run deploy` resets it.

## Key Configuration

| Env Var | Default | Notes |
|---|---|---|
| `CRAWL_MAX_DEPTH` | `3` | BFS link depth |
| `CRAWL_MAX_PAGES` | `2000` | Hard cap |
| `CRAWL_DELAY` | `0.5` | Seconds between requests per domain |
| `CRAWL_MAX_WORKERS` | `20` | Concurrent crawler threads |
| `ETL_MAX_WORKERS` | `10` | Concurrent ETL threads |
| `CRAWL_RESPECT_ROBOTS` | `true` | Set `false` to bypass robots.txt globally |
| `CRAWL_ROBOTS_IGNORE_DOMAINS` | `""` | Comma-separated domains to bypass |
| `CRAWL_MIN_WORDS` | `80` | Minimum words for non-thin pages |

## Important Files

- `urls.txt` — seed URLs for the crawler (one per line, `#` comments)
- `url_exclusions.txt` — substring patterns to skip during crawl (see file for categories)
- `url_hashes.json` — auto-generated dedup state; delete to force full re-processing
- `discovered_urls.json` — auto-generated crawler output with cached page text
- `sa-key.json` — service account key (gitignored by `.gitattributes`)
