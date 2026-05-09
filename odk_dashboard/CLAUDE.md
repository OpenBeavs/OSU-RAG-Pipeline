# CLAUDE.md — ODK Dashboard

Standalone FastAPI MVP for the OpenBeavs Department Knowledge system.
Read this before touching any code.

## What This App Does

- **Submission portal** (`/`) — department managers upload PDF/text knowledge documents tagged with their `oregonstate.edu` URL subtree.
- **Admin panel** (`/admin`) — OpenBeavs admins review, accept, or reject submissions. Accepting triggers background indexing into Firestore via `odk_pipeline.py`.
- **ODK Agent** (`../odk_agent/`) — a Google ADK agent that searches both the department-specific Firestore collection and the public OSU knowledge base at query time.

## File Map

```
odk_dashboard/
├── app.py              # FastAPI routes (portal, admin, accept, reject, delete)
├── db.py               # Plain sqlite3 helpers — no ORM
├── templates/
│   ├── portal.html     # Submission upload form (Tailwind + Jinja2)
│   └── admin.html      # Review table with accept/reject/delete actions
├── requirements.txt
├── README.md
└── CLAUDE.md           # This file

../odk_pipeline.py      # Document ingestion: file → chunks → embeddings → Firestore
../odk_agent/agent.py   # ADK agent with search_department_knowledge + search_osu_knowledge
```

## Tech Stack

| Layer | Technology |
|---|---|
| Web framework | FastAPI (sync routes, multipart form handling) |
| Templating | Jinja2 with Tailwind CSS (CDN) |
| Database | SQLite via plain `sqlite3` — no ORM |
| File storage | Local `uploads/` directory |
| Embeddings | Google GenAI `gemini-embedding-001` (768-dim) |
| Vector DB | Firestore `odk-knowledge` collection |
| PDF parsing | pdfplumber (falls back to pypdf) |

## Key Design Decisions

- **No ORM.** `db.py` uses plain `sqlite3.connect()` — simple enough that an ORM adds no value.
- **No auth in MVP.** The admin panel is intentionally unauthenticated. Add HTTP Basic Auth before any shared deployment.
- **Background indexing.** Firestore writes happen in a FastAPI `BackgroundTask` after the admin clicks Accept — the HTTP response returns immediately.
- **Separate app.** This is NOT integrated into GENESIS-AI-Hub. It runs standalone on port 8001.
- **Shared `.env`.** Reads from the parent `OSU-RAG-Pipeline/.env`, not its own env file.

## Running Locally

```bash
# From OSU-RAG-Pipeline/
source venv_odk/bin/activate
cd odk_dashboard/
uvicorn app:app --reload --port 8001
```

## Common Tasks

### Add a new file type
In `app.py`, add the extension to `ALLOWED_EXTENSIONS`.
In `odk_pipeline.py`, add a branch in `extract_text_from_file()`.

### Add admin authentication
Wrap the `/admin*` routes with an HTTP Basic Auth dependency using FastAPI's `HTTPBasic` security scheme. Add `ADMIN_USERNAME` / `ADMIN_PASSWORD` to `.env`.

### Change the Firestore collection
Set `ODK_FIRESTORE_COLLECTION` in `.env`. The pipeline and agent both read this env var.

### Re-index an already-accepted document
Run `odk_pipeline.py` directly from the CLI — it deletes stale vectors before upserting.

## Conventions

- Python: type hints on all function signatures, `log.info/warning/error` (never `print`).
- HTML templates: Tailwind utility classes only, no inline styles, no external JS beyond Tailwind CDN.
- No new dependencies without updating `requirements.txt`.
