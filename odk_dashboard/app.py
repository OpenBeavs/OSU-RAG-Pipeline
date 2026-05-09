"""
ODK Dashboard — standalone MVP FastAPI app
==========================================
Pages:
  /             — submission portal (upload form)
  /admin        — admin review panel with live crawl status
  /admin/graph  — D3 knowledge graph built from crawl data via visualize.py

Run:
    cd odk_dashboard/
    uvicorn app:app --reload --port 8001

Needs .env in the parent OSU-RAG-Pipeline/ directory with:
    GOOGLE_API_KEY, GCP_PROJECT_ID
"""

import json
import logging
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import (
    BackgroundTasks,
    FastAPI,
    Form,
    HTTPException,
    Request,
    UploadFile,
    File,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import db

# ── env + path setup ─────────────────────────────────────────────────────────
RAG_ROOT = Path(__file__).parent.parent
load_dotenv(RAG_ROOT / ".env")
sys.path.insert(0, str(RAG_ROOT))

log = logging.getLogger("odk-dashboard")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md", ".rst"}

app = FastAPI(title="ODK Dashboard", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
db.init_db()

FILTER_TABS = [
    ("all", "All"),
    ("pending", "Pending"),
    ("accepted", "Accepted"),
    ("rejected", "Rejected"),
]


# ── Jinja2 helpers ───────────────────────────────────────────────────────────

def _fmt_time(ts: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

templates.env.filters["fmt_time"] = _fmt_time


# ── Background indexing ──────────────────────────────────────────────────────

def _run_indexing(sub_id: str, file_path: str, department_url: str) -> None:
    """Index the uploaded document AND crawl the department's web subtree."""
    from odk_pipeline import ingest_document, crawl_and_index_department

    db.upsert_crawl_run(sub_id, doc_status="running", started_at=int(time.time()))

    # 1. Document
    doc_result = ingest_document(file_path, department_url)
    if doc_result["status"] == "ok":
        db.upsert_crawl_run(
            sub_id,
            doc_status="done",
            doc_vectors=doc_result.get("vectors_upserted", 0),
        )
        log.info("submission %s — doc indexed: %d vectors", sub_id, doc_result.get("vectors_upserted", 0))
    else:
        db.upsert_crawl_run(sub_id, doc_status="failed", error=doc_result.get("message"))
        log.error("submission %s — doc failed: %s", sub_id, doc_result.get("message"))

    # 2. Web crawl
    db.upsert_crawl_run(sub_id, web_status="running")
    log.info("submission %s — starting web crawl for %s", sub_id, department_url)

    def _on_page(url: str, title: str, word_count: int, chunks: int, vectors: int, status: str) -> None:
        db.add_crawl_page(sub_id, url, title, word_count, chunks, vectors, status)
        db.increment_crawl_run(
            sub_id,
            pages_found=1,
            pages_indexed=(1 if status == "indexed" else 0),
            web_vectors=vectors,
        )

    web_result = crawl_and_index_department(
        department_url, max_pages=50, on_page_indexed=_on_page
    )
    if web_result["status"] == "ok":
        db.upsert_crawl_run(sub_id, web_status="done", finished_at=int(time.time()))
        log.info(
            "submission %s — crawl done: %d pages, %d vectors",
            sub_id, web_result.get("pages", 0), web_result.get("vectors_upserted", 0),
        )
    else:
        db.upsert_crawl_run(sub_id, web_status="failed",
                            error=web_result.get("message"), finished_at=int(time.time()))
        log.error("submission %s — crawl failed: %s", sub_id, web_result.get("message"))


# ── Routes: portal ───────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def portal(request: Request, success: str = "", error: str = ""):
    return templates.TemplateResponse(
        request=request,
        name="portal.html",
        context={"success": success, "error": error},
    )


@app.post("/submit")
async def submit(
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


# ── Routes: admin ────────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request, filter: str = "all"):
    status_filter = None if filter == "all" else filter
    submissions = db.list_submissions(status=status_filter)

    # Attach crawl_run data to each accepted submission
    crawl_runs = {}
    for sub in submissions:
        if sub["status"] == "accepted":
            run = db.get_crawl_run(sub["id"])
            if run:
                crawl_runs[sub["id"]] = run

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "submissions": submissions,
            "filter": filter,
            "tabs": FILTER_TABS,
            "crawl_runs": crawl_runs,
        },
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


# ── Routes: status API (polled by admin page JS) ─────────────────────────────

@app.get("/api/status/{sub_id}")
async def crawl_status(sub_id: str):
    """Return live crawl progress for a submission (polled every 3s by the admin UI)."""
    run = db.get_crawl_run(sub_id)
    if not run:
        return {"found": False}

    pages = db.get_crawl_pages(sub_id)
    return {
        "found": True,
        **run,
        "pages": pages,
    }


# ── Routes: knowledge graph (via visualize.py) ────────────────────────────────

@app.get("/admin/graph", response_class=HTMLResponse)
async def graph():
    """D3 force-directed graph of all crawled pages, built using visualize.py's template."""
    from visualize import build_graph_data, HTML_TEMPLATE

    all_pages = db.get_all_crawl_pages()

    if not all_pages:
        return HTMLResponse(
            "<html><body style='background:#0d1117;color:#e6edf3;font-family:sans-serif;"
            "display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
            "<div style='text-align:center'>"
            "<h2 style='color:#DC4405'>No crawl data yet</h2>"
            "<p style='color:#6e7681;margin-top:8px'>Accept a submission to trigger the first crawl.</p>"
            "<a href='/admin' style='color:#DC4405;margin-top:16px;display:block'>← Back to admin</a>"
            "</div></body></html>"
        )

    # Build discovered_urls.json-compatible structure from crawl_page rows
    urls_data: dict = {}
    domain_pages: dict[str, list] = defaultdict(list)

    for p in all_pages:
        parsed = urlparse(p["url"])
        domain = parsed.netloc
        urls_data[p["url"]] = {
            "domain": domain,
            "title": p["title"] or p["url"],
            "word_count": p["word_count"],
            "depth": 0,
            "thin": p["word_count"] < 80,
            "latency_ms": 0,
        }
        domain_pages[domain].append(p)

    domain_summary = {
        domain: {
            "pages": len(pages),
            "avg_latency_ms": 0,
            "redirected_count": 0,
        }
        for domain, pages in domain_pages.items()
    }

    total_pages = len(all_pages)
    raw = {
        "urls": urls_data,
        "failed": {},
        "domain_summary": domain_summary,
        "domain_graph": {},
        "crawl_metadata": {
            "started_at": "",
            "ended_at": "",
            "summary": {
                "pages_crawled": total_pages,
                "pages_failed": 0,
                "urls_discovered": total_pages,
            },
        },
    }

    graph_data = build_graph_data(raw)
    html = HTML_TEMPLATE.replace("__DATA__", json.dumps(graph_data, separators=(",", ":")))

    # Patch the title to say ODK instead of OSU
    html = html.replace("OSU Knowledge Graph", "ODK Knowledge Graph")
    return HTMLResponse(html)
