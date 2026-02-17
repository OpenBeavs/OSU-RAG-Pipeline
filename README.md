# 🌲 OSU RAG Pipeline

A production-ready ETL pipeline that builds and maintains a **Vector Database** for an Oregon State University AI agent. It crawls `*.oregonstate.edu` pages, chunks the content, generates embeddings, and upserts them into Pinecone — with smart deduplication so only changed pages are re-processed.

---

## Features

- **BFS Web Crawler** — Automatically discovers all reachable pages within `*.oregonstate.edu`
- **Content-Hash Deduplication** — SHA256 hashing ensures unchanged pages are skipped on re-runs
- **Stale Vector Cleanup** — Old vectors are deleted before new ones are upserted (no ghost data)
- **Retry + Backoff** — Handles network timeouts common with `.edu` sites
- **Robots.txt Compliance** — Respects each subdomain's `robots.txt`
- **Configurable** — Depth limits, page caps, and crawl delays via environment variables
- **Cron-Ready** — Designed to run on a schedule (e.g., every 6 hours)

---

## Tech Stack

| Component       | Technology                                       |
| --------------- | ------------------------------------------------ |
| Language        | Python 3.10+                                     |
| Vector Database | [Pinecone](https://www.pinecone.io/)             |
| Embeddings      | Google GenAI (`models/text-embedding-004`, 768d)  |
| Text Splitting  | LangChain `RecursiveCharacterTextSplitter`       |
| Scraping        | Beautiful Soup 4 + Requests                      |
| Token Counting  | tiktoken (`cl100k_base`)                         |

---

## Prerequisites

- **Python 3.10+** installed
- A **Google AI Studio** API key ([get one here](https://aistudio.google.com/apikey))
- A **Pinecone** account and API key ([sign up here](https://app.pinecone.io/))
- A Pinecone index created with:
  - **Dimension:** `768`
  - **Metric:** `cosine`

---

## Local Development Setup

### 1. Clone the Repository

```bash
git clone https://github.com/Jamoo/OSU-RAG-Pipeline.git
cd OSU-RAG-Pipeline
```

### 2. Create a Virtual Environment

```bash
# Create
python -m venv venv

# Activate (Windows)
venv\Scripts\activate

# Activate (macOS/Linux)
source venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Environment Variables

```bash
# Copy the example env file
cp .env.example .env
```

Open `.env` and fill in your values:

```env
# Required — API Keys
GOOGLE_API_KEY=your-google-api-key-here
PINECONE_API_KEY=your-pinecone-api-key-here
PINECONE_INDEX_NAME=osu-knowledge

# Optional — Crawler Settings
CRAWL_MAX_DEPTH=3          # How deep to follow links (0 = seed URLs only)
CRAWL_MAX_PAGES=500        # Hard cap on pages to crawl
CRAWL_DELAY=1.0            # Seconds between requests (be polite)
CRAWL_STRIP_QUERY=true     # Strip ?query=params from URLs
```

### 5. Add Seed URLs

Edit `urls.txt` to add starting URLs (one per line):

```text
# These are the entry points for the crawler
https://oregonstate.edu
https://admissions.oregonstate.edu
https://financialaid.oregonstate.edu
https://registrar.oregonstate.edu
```

### 6. Create Your Pinecone Index

Go to the [Pinecone Console](https://app.pinecone.io/) and create an index:

| Setting   | Value         |
| --------- | ------------- |
| Name      | `osu-knowledge` (or your `PINECONE_INDEX_NAME`) |
| Dimension | `768`         |
| Metric    | `cosine`      |

---

## Usage

### Dry Run (No API Calls)

Test the full pipeline without hitting Pinecone or Google GenAI:

```bash
# Static URL list only
python etl_pipeline.py --dry-run

# With crawler (discovers pages, chunks them, but doesn't embed/upsert)
python etl_pipeline.py --crawl --dry-run
```

### Full Run

```bash
# Process only the URLs in urls.txt
python etl_pipeline.py

# Crawl *.oregonstate.edu first, then process all discovered pages
python etl_pipeline.py --crawl
```

### Run the Crawler Standalone

```bash
python crawler.py
```

This outputs `discovered_urls.json` which you can inspect or edit before running the pipeline.

---

## Scheduling (Cron)

To keep the knowledge base fresh, schedule the pipeline to run every 6 hours:

### Linux / macOS

```bash
crontab -e
```

Add this line:

```cron
0 */6 * * * cd /path/to/OSU-RAG-Pipeline && /path/to/venv/bin/python etl_pipeline.py --crawl >> etl.log 2>&1
```

### Windows (Task Scheduler)

1. Open **Task Scheduler** → Create Basic Task
2. Set trigger to repeat every **6 hours**
3. Action: Start a program
   - Program: `C:\path\to\venv\Scripts\python.exe`
   - Arguments: `etl_pipeline.py --crawl`
   - Start in: `C:\path\to\OSU-RAG-Pipeline`

---

## Project Structure

```
OSU-RAG-Pipeline/
├── etl_pipeline.py        # Main ETL pipeline (fetch → dedup → chunk → embed → upsert)
├── crawler.py             # BFS web crawler for *.oregonstate.edu
├── urls.txt               # Seed URLs for the crawler
├── requirements.txt       # Python dependencies
├── .env.example           # Environment variable template
├── .env                   # Your local config (gitignored)
├── .gitignore
├── url_hashes.json        # Auto-generated: content hash state (gitignored)
└── discovered_urls.json   # Auto-generated: crawler output (gitignored)
```

---

## How It Works

### Pipeline Flow

```
1. CRAWL (optional)     Discover all reachable *.oregonstate.edu pages via BFS
2. FETCH                Download HTML for each URL (with retry/backoff)
3. DEDUP                SHA256 hash check — skip if content hasn't changed
4. CLEAN                Strip navbars, footers, scripts, and HTML boilerplate
5. CHUNK                Split into ~512-token chunks (64-token overlap)
6. EMBED                Generate 768-dim embeddings via text-embedding-004
7. DELETE OLD VECTORS   Remove stale vectors for changed URLs (prefix-based)
8. UPSERT               Write new vectors + metadata to Pinecone
9. SAVE STATE           Persist content hashes for next run
```

### Vector Metadata

Each vector stored in Pinecone includes:

```json
{
  "url": "https://admissions.oregonstate.edu",
  "title": "Undergraduate Admissions | Oregon State University",
  "last_crawled": "2026-02-17T08:30:00+00:00",
  "text": "The actual chunk text for retrieval..."
}
```

### Deduplication Strategy

- **URL → content hash** mapping stored in `url_hashes.json`
- On each run, the HTML is fetched and hashed with SHA256
- If the hash matches the stored value → page is skipped entirely
- If it differs → old vectors are deleted, new ones are created

---

## Configuration Reference

| Variable             | Default          | Description                                    |
| -------------------- | ---------------- | ---------------------------------------------- |
| `GOOGLE_API_KEY`     | *(required)*     | Google AI Studio API key                       |
| `PINECONE_API_KEY`   | *(required)*     | Pinecone API key                               |
| `PINECONE_INDEX_NAME`| `osu-knowledge`  | Name of your Pinecone index                    |
| `CRAWL_MAX_DEPTH`    | `3`              | Max link-follow depth from seed URLs           |
| `CRAWL_MAX_PAGES`    | `500`            | Maximum number of pages to crawl               |
| `CRAWL_DELAY`        | `1.0`            | Seconds between HTTP requests                  |
| `CRAWL_STRIP_QUERY`  | `true`           | Strip query parameters from discovered URLs    |

---

## Tips for a Comprehensive Knowledge Base

1. **Increase `CRAWL_MAX_PAGES`** — OSU has thousands of pages. Set to `2000`+ for broad coverage.
2. **Increase `CRAWL_MAX_DEPTH`** — Depth `5` will reach most content. Higher values find deeply nested pages.
3. **Add more seed URLs** — Each subdomain (e.g., `engineering.oregonstate.edu`) should be a seed for best coverage.
4. **Re-run regularly** — Pages change. The cron schedule + dedup ensures your vectors stay fresh without reprocessing everything.
5. **Monitor the logs** — The pipeline logs every skip, update, and failure to stdout.

---

## 🤖 RAG Query Agent (Google ADK + A2A)

An AI agent that answers OSU questions by searching the Pinecone knowledge base. Built with [Google ADK](https://google.github.io/adk-docs/) and exposes an [A2A](https://a2aprotocol.org/) endpoint for inter-agent communication.

### Install Agent Dependencies

```bash
pip install -r agent_requirements.txt
```

### Run with ADK Dev UI

```bash
# From the project root (parent of osu_rag_agent/)
adk web
```

Select **osu_rag_agent** from the dropdown and start chatting.

> **Windows note:** If you hit `_make_subprocess_transport NotImplementedError`, use `adk web --no-reload`.

### Serve via A2A Protocol

```bash
adk api_server --a2a
```

The Agent Card is served at `http://localhost:8000/.well-known/agent.json`. Other agents (e.g., a Master Router) can discover and communicate with this agent using the A2A protocol.

### Project Structure (Agent)

```
OSU-RAG-Pipeline/
├── osu_rag_agent/
│   ├── __init__.py        # Package init
│   ├── agent.py           # ADK agent + Pinecone RAG search tool
│   └── agent.json         # A2A Agent Card
├── agent_requirements.txt # Agent dependencies
└── ...                    # ETL pipeline files
```

---

## License

MIT
