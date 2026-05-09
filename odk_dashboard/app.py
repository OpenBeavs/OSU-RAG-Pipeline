"""
ODK Dashboard — standalone MVP FastAPI app
==========================================
Two pages:
  /          — submission portal (upload form)
  /admin     — admin review panel (list, accept, reject)

Run:
    cd odk_dashboard/
    uvicorn app:app --reload --port 8001

Needs .env in the parent OSU-RAG-Pipeline/ directory with:
    GOOGLE_API_KEY, GCP_PROJECT_ID
"""

import logging
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import (
    BackgroundTasks,
    FastAPI,
    Form,
    HTTPException,
    Request,
    UploadFile,
    File,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import db

# ── load env from parent directory ──────────────────────────────────────────
load_dotenv(Path(__file__).parent.parent / ".env")

# ── add parent dir to path so odk_pipeline is importable ────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

log = logging.getLogger("odk-dashboard")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md", ".rst"}

app = FastAPI(title="ODK Dashboard", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
db.init_db()


# ── helpers ──────────────────────────────────────────────────────────────────

def _fmt_time(ts: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


templates.env.filters["fmt_time"] = _fmt_time


def _run_indexing(sub_id: str, file_path: str, department_url: str) -> None:
    """Background task: index the uploaded document AND crawl the department's web subtree."""
    try:
        from odk_pipeline import ingest_document, crawl_and_index_department

        # 1. Index the uploaded document (source_type="document")
        doc_result = ingest_document(file_path, department_url)
        if doc_result["status"] == "ok":
            log.info("submission %s — doc indexed: %s vectors", sub_id, doc_result.get("vectors_upserted"))
        else:
            log.error("submission %s — doc indexing failed: %s", sub_id, doc_result.get("message"))

        # 2. Crawl the department's public web subtree (source_type="web")
        log.info("submission %s — starting web crawl for %s", sub_id, department_url)
        web_result = crawl_and_index_department(department_url, max_pages=50)
        if web_result["status"] == "ok":
            log.info(
                "submission %s — web crawl done: %d pages, %d vectors",
                sub_id, web_result.get("pages", 0), web_result.get("vectors_upserted", 0),
            )
        else:
            log.error("submission %s — web crawl failed: %s", sub_id, web_result.get("message"))

    except Exception as exc:
        log.exception("Indexing raised for submission %s: %s", sub_id, exc)


# ── routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def portal(request: Request, success: str = "", error: str = ""):
    """Submission portal — department managers upload documents here."""
    return templates.TemplateResponse(
        request=request,
        name="portal.html",
        context={"success": success, "error": error},
    )


@app.post("/submit")
async def submit(
    request: Request,
    submitter_email: str = Form(...),
    department_name: str = Form(...),
    department_url: str = Form(...),
    file: UploadFile = File(...),
):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return RedirectResponse(
            f"/?error=Unsupported+file+type+'{suffix}'.+Allowed:+{','.join(sorted(ALLOWED_EXTENSIONS))}",
            status_code=303,
        )

    sub_id = str(uuid.uuid4())
    safe_name = f"{sub_id}_{Path(file.filename or 'doc').name}"
    dest = UPLOAD_DIR / safe_name
    dest.write_bytes(await file.read())

    dept = department_url.strip().lower().replace("https://", "").replace("http://", "").rstrip("/")

    db.create_submission(
        submitter_email=submitter_email,
        department_name=department_name,
        department_url=dept,
        filename=file.filename or safe_name,
        file_path=str(dest),
    )

    return RedirectResponse("/?success=Your+document+has+been+submitted+for+review.", status_code=303)


FILTER_TABS = [
    ("all", "All"),
    ("pending", "Pending"),
    ("accepted", "Accepted"),
    ("rejected", "Rejected"),
]


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request, filter: str = "all"):
    status_filter = None if filter == "all" else filter
    submissions = db.list_submissions(status=status_filter)
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={"submissions": submissions, "filter": filter, "tabs": FILTER_TABS},
    )


@app.post("/admin/{sub_id}/accept")
async def accept(sub_id: str, background_tasks: BackgroundTasks):
    sub = db.get_submission(sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")
    if sub["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Already '{sub['status']}'")

    db.update_status(sub_id, "accepted")
    background_tasks.add_task(_run_indexing, sub_id, sub["file_path"], sub["department_url"])
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/{sub_id}/reject")
async def reject(sub_id: str, reason: str = Form("")):
    sub = db.get_submission(sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")
    if sub["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Already '{sub['status']}'")

    db.update_status(sub_id, "rejected", rejection_reason=reason or None)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/{sub_id}/delete")
async def delete(sub_id: str):
    sub = db.get_submission(sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")

    path = Path(sub["file_path"])
    if path.exists():
        path.unlink(missing_ok=True)

    db.delete_submission(sub_id)
    return RedirectResponse("/admin", status_code=303)
