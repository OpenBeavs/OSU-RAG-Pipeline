"""
OSU RAG Query Agent
===================
A Google ADK agent that answers Oregon State University questions
by searching a Firestore vector database populated by the ETL pipeline.

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

# Load .env from project root (one level up from this package)
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_PATH)

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")
FIRESTORE_COLLECTION = os.environ.get("FIRESTORE_COLLECTION", "osu-knowledge")

EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMENSION = 768

# ──────────────────────────────────────────────
# Clients (lazy-initialized)
# ──────────────────────────────────────────────

_genai_client: genai.Client | None = None
_fs_collection = None


def _get_genai_client() -> genai.Client:
    global _genai_client
    if _genai_client is None:
        _genai_client = genai.Client(api_key=GOOGLE_API_KEY)
    return _genai_client


def _get_firestore_collection():
    global _fs_collection
    if _fs_collection is None:
        fs_client = firestore.Client(project=GCP_PROJECT_ID)
        _fs_collection = fs_client.collection(FIRESTORE_COLLECTION)
    return _fs_collection


# ──────────────────────────────────────────────
# RAG Search Tool
# ──────────────────────────────────────────────

def search_osu_knowledge(query: str, top_k: int = 5) -> dict:
    """Search the OSU knowledge base for information relevant to the query.

    Embeds the query and performs a semantic vector search against the
    Firestore collection containing chunked content from *.oregonstate.edu.

    Args:
        query: The search query describing what information to find.
        top_k: Number of results to return (1-10). Defaults to 5.

    Returns:
        dict: A dictionary with 'status' and either 'results' (a list of
              matching chunks with text, url, title, and score) or
              'error_message'.
    """
    top_k = max(1, min(top_k, 10))

    try:
        # Generate embedding for the query
        client = _get_genai_client()
        embed_result = client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=[query],
            config={"output_dimensionality": EMBEDDING_DIMENSION},
        )
        query_embedding = embed_result.embeddings[0].values

        # Query Firestore with vector search
        collection = _get_firestore_collection()
        vector_query = collection.find_nearest(
            vector_field="embedding",
            query_vector=Vector(query_embedding),
            distance_measure=DistanceMeasure.COSINE,
            limit=top_k,
        )

        # Format results
        matches = []
        docs = vector_query.stream()
        for doc in docs:
            data = doc.to_dict()
            matches.append({
                "text": data.get("text", ""),
                "url": data.get("url", ""),
                "title": data.get("title", ""),
            })

        if not matches:
            return {
                "status": "no_results",
                "message": "No relevant information found in the OSU knowledge base for this query.",
            }

        return {
            "status": "success",
            "results": matches,
        }

    except Exception as exc:
        return {
            "status": "error",
            "error_message": f"Knowledge base search failed: {exc}",
        }


# ──────────────────────────────────────────────
# Agent Definition
# ──────────────────────────────────────────────

AGENT_INSTRUCTION = """\
You are the OSU Expert, an AI assistant for Oregon State University.

Rules:
- Always call `search_osu_knowledge` before answering any OSU question.
- Give short, direct answers — 2-4 sentences max unless a list is clearly needed.
- End with one source URL (the most relevant). No "Sources" section header.
- If nothing is found, say so in one sentence and point to oregonstate.edu.
- Decline off-topic questions in one sentence.
"""

root_agent = Agent(
    name="osu_rag_agent",
    model="gemini-2.5-flash",
    description=(
        "An Oregon State University expert agent that answers questions "
        "using a RAG knowledge base of oregonstate.edu content."
    ),
    instruction=AGENT_INSTRUCTION,
    tools=[search_osu_knowledge],
)
