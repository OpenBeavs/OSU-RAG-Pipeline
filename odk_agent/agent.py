"""
OpenBeavs Department Knowledge (ODK) Agent
==========================================
Extends the OSU Expert with a second search tool that retrieves custom
knowledge documents submitted by a specific department, keyed by URL subtree.

A single agent is registered once in the Agent Registry. At conversation
time the embedding page passes its URL as context (e.g., in the first
message or a system prompt prefix), and the agent resolves the matching
department knowledge accordingly.

Exposes an A2A-compatible endpoint for inter-agent communication.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.adk.agents import Agent
from google.cloud import firestore
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_PATH)

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")
OSU_COLLECTION = os.environ.get("FIRESTORE_COLLECTION", "osu-knowledge")
ODK_COLLECTION = os.environ.get("ODK_FIRESTORE_COLLECTION", "odk-knowledge")

EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMENSION = 768

# ──────────────────────────────────────────────
# Clients (lazy-initialized)
# ──────────────────────────────────────────────

_genai_client: genai.Client | None = None
_osu_collection = None
_odk_collection = None


def _get_genai_client() -> genai.Client:
    global _genai_client
    if _genai_client is None:
        _genai_client = genai.Client(api_key=GOOGLE_API_KEY)
    return _genai_client


def _get_osu_collection():
    global _osu_collection
    if _osu_collection is None:
        fs = firestore.Client(project=GCP_PROJECT_ID)
        _osu_collection = fs.collection(OSU_COLLECTION)
    return _osu_collection


def _get_odk_collection():
    global _odk_collection
    if _odk_collection is None:
        fs = firestore.Client(project=GCP_PROJECT_ID)
        _odk_collection = fs.collection(ODK_COLLECTION)
    return _odk_collection


# ──────────────────────────────────────────────
# Shared embedding helper
# ──────────────────────────────────────────────

def _embed_query(query: str) -> list[float]:
    """Embed a single query string and return the vector."""
    client = _get_genai_client()
    result = client.models.embed_content(
        model=EMBEDDING_MODEL,
        contents=[query],
        config={"output_dimensionality": EMBEDDING_DIMENSION},
    )
    return result.embeddings[0].values


# ──────────────────────────────────────────────
# RAG Tools
# ──────────────────────────────────────────────

def search_osu_knowledge(query: str, top_k: int = 5) -> dict:
    """Search the public OSU knowledge base for information relevant to the query.

    Embeds the query and performs a semantic vector search against the
    Firestore collection containing chunked content from *.oregonstate.edu.

    Args:
        query: The search query describing what information to find.
        top_k: Number of results to return (1-10). Defaults to 5.

    Returns:
        dict with 'status' and either 'results' (list of matching chunks
        with text, url, title) or 'error_message'.
    """
    top_k = max(1, min(top_k, 10))
    try:
        query_embedding = _embed_query(query)
        collection = _get_osu_collection()
        vector_query = collection.find_nearest(
            vector_field="embedding",
            query_vector=Vector(query_embedding),
            distance_measure=DistanceMeasure.COSINE,
            limit=top_k,
        )
        matches = [
            {"text": d.get("text", ""), "url": d.get("url", ""), "title": d.get("title", "")}
            for doc in vector_query.stream()
            for d in [doc.to_dict()]
        ]
        if not matches:
            return {"status": "no_results", "message": "No relevant OSU information found."}
        return {"status": "success", "results": matches}
    except Exception as exc:
        return {"status": "error", "error_message": f"OSU knowledge search failed: {exc}"}


def search_department_knowledge(query: str, department_url: str, top_k: int = 5) -> dict:
    """Search the department-specific knowledge base for the given URL subtree.

    The knowledge base contains two source types stored in the same collection:
      - source_type="document": PDFs and text files uploaded by department managers
      - source_type="web": pages crawled from the department's public URL subtree

    Both are searched together, giving comprehensive coverage of both internal
    know-how and public web content for the department.

    Args:
        query: The search query describing what information to find.
        department_url: The department's oregonstate.edu URL subtree, e.g.
            'advantage.oregonstate.edu/startups/' or 'beavsbuild.oregonstate.edu'.
        top_k: Number of results to return (1-10). Defaults to 5.

    Returns:
        dict with 'status' and either 'results' (list of matching chunks with
        text, source_type, and source_file or url) or 'error_message'.
    """
    top_k = max(1, min(top_k, 10))
    if not department_url:
        return {"status": "no_results", "message": "No department URL provided."}

    # Normalize: strip protocol, trailing slash
    dept = department_url.lower().replace("https://", "").replace("http://", "").rstrip("/")

    try:
        query_embedding = _embed_query(query)
        collection = _get_odk_collection()

        # Filter by department URL subtree, then rank by vector similarity.
        # Firestore vector queries don't support .where() pre-filtering, so we
        # fetch a larger pool and filter in-memory.
        vector_query = collection.find_nearest(
            vector_field="embedding",
            query_vector=Vector(query_embedding),
            distance_measure=DistanceMeasure.COSINE,
            limit=top_k * 4,
        )

        matches = []
        for doc in vector_query.stream():
            data = doc.to_dict()
            doc_dept = (data.get("department_url") or "").lower().rstrip("/")
            if dept in doc_dept or doc_dept in dept:
                result = {
                    "text": data.get("text", ""),
                    "source_type": data.get("source_type", "unknown"),
                    "department_url": data.get("department_url", ""),
                }
                # Include the appropriate source identifier
                if data.get("source_type") == "web":
                    result["url"] = data.get("url", "")
                    result["title"] = data.get("title", "")
                else:
                    result["source_file"] = data.get("source_file", "")
                matches.append(result)
            if len(matches) >= top_k:
                break

        if not matches:
            return {
                "status": "no_results",
                "message": f"No department-specific knowledge found for '{department_url}'.",
            }
        return {"status": "success", "results": matches}
    except Exception as exc:
        return {"status": "error", "error_message": f"Department knowledge search failed: {exc}"}


# ──────────────────────────────────────────────
# Agent Definition
# ──────────────────────────────────────────────

AGENT_INSTRUCTION = """\
You are the OpenBeavs Department Knowledge (ODK) assistant for Oregon State University.

At the start of every conversation the system will include a line like:
  [Department URL: advantage.oregonstate.edu/startups/]

Rules:
1. Extract the department URL from the system context.
2. ALWAYS call `search_department_knowledge` first using that department URL.
3. THEN call `search_osu_knowledge` to supplement with public OSU web content.
4. Synthesize both results into a short, direct answer (2-4 sentences max).
5. End with the most relevant source (department doc name or OSU URL).
6. If neither search finds anything relevant, say so in one sentence and direct
   the user to oregonstate.edu or their department contact.
7. Decline off-topic questions in one sentence.
"""

root_agent = Agent(
    name="odk_agent",
    model="gemini-2.5-flash",
    description=(
        "OpenBeavs Department Knowledge agent that answers questions using "
        "both the public OSU web knowledge base and department-submitted "
        "custom documents, resolved by page URL context."
    ),
    instruction=AGENT_INSTRUCTION,
    tools=[search_department_knowledge, search_osu_knowledge],
)
