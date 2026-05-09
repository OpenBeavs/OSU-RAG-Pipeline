# ODK Dashboard

Standalone MVP web app for the **OpenBeavs Department Knowledge (ODK)** system.

Department managers submit custom knowledge documents through the portal. Administrators review and approve submissions, which triggers background indexing into the Firestore `odk-knowledge` vector database. The [ODK Agent](../odk_agent/) then serves that knowledge alongside the public OSU web index.

## Architecture

```
Department Manager
       │
       ▼
  / (portal)          — upload form (email, dept name, URL subtree, file)
       │
       ▼
  SQLite (odk.db)     — submission record, status = "pending"
       │
       ▼
  /admin (review)     — admin lists, accepts, or rejects submissions
       │
       ▼
  odk_pipeline.py     — background task: PDF → chunks → embeddings → Firestore
       │
       ▼
  Firestore odk-knowledge  — department-scoped vector index
       │
       ▼
  ODK Agent           — queries both odk-knowledge + osu-knowledge at runtime
```

## Setup

```bash
# From the OSU-RAG-Pipeline/ directory
python3 -m venv venv_odk
source venv_odk/bin/activate        # Windows: venv_odk\Scripts\activate
pip install -r odk_dashboard/requirements.txt

# Copy and fill in the shared .env (one level up)
cp .env.example .env
# Required: GOOGLE_API_KEY, GCP_PROJECT_ID
```

## Run

```bash
cd odk_dashboard/
uvicorn app:app --reload --port 8001
```

Then open [http://localhost:8001](http://localhost:8001).

## Pages

| Route | Purpose |
|---|---|
| `/` | Submission portal — department managers upload documents |
| `/admin` | Admin review panel — accept or reject pending submissions |
| `/admin?filter=pending` | Filter by status: `pending`, `accepted`, `rejected` |

## Supported File Types

`.pdf`, `.txt`, `.md`, `.rst`

## Environment Variables

Reads from the parent `OSU-RAG-Pipeline/.env`:

| Variable | Default | Notes |
|---|---|---|
| `GOOGLE_API_KEY` | — | Google AI Studio key (embeddings) |
| `GCP_PROJECT_ID` | — | GCP project with Firestore in Native mode |
| `ODK_FIRESTORE_COLLECTION` | `odk-knowledge` | Firestore collection for department docs |
| `FIRESTORE_COLLECTION` | `osu-knowledge` | Firestore collection for public OSU web index |

## Data Storage

- **`odk.db`** — SQLite file created automatically on first run; tracks submission records
- **`uploads/`** — uploaded files stored locally until indexed; created automatically

## Deployment Notes

The MVP has no authentication. Before deploying to a shared environment:
- Add HTTP Basic Auth or an API key check to the `/admin` routes
- Consider moving file storage to GCS instead of the local filesystem
- Set `--workers 1` when running with Uvicorn to avoid SQLite write contention across processes
