"""
OSU RAG Query Agent
===================
A Google ADK agent that answers Oregon State University questions
by searching a Pinecone vector database populated by the ETL pipeline.

Exposes an A2A-compatible endpoint for inter-agent communication.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.adk.agents import Agent
from pinecone import Pinecone

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

# Load .env from project root (one level up from this package)
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_PATH)

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
PINECONE_API_KEY = os.environ.get("PINECONE_API_KEY", "")
PINECONE_INDEX_NAME = os.environ.get("PINECONE_INDEX_NAME", "osu-archiving")

EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMENSION = 768

# ──────────────────────────────────────────────
# Clients (lazy-initialized)
# ──────────────────────────────────────────────

_genai_client: genai.Client | None = None
_pc_index = None


def _get_genai_client() -> genai.Client:
    global _genai_client
    if _genai_client is None:
        _genai_client = genai.Client(api_key=GOOGLE_API_KEY)
    return _genai_client


def _get_pinecone_index():
    global _pc_index
    if _pc_index is None:
        pc = Pinecone(api_key=PINECONE_API_KEY)
        _pc_index = pc.Index(PINECONE_INDEX_NAME)
    return _pc_index


# ──────────────────────────────────────────────
# RAG Search Tool
# ──────────────────────────────────────────────

def search_osu_knowledge(query: str, top_k: int = 5) -> dict:
    """Search the OSU knowledge base for information relevant to the query.

    Embeds the query and performs a semantic search against the Pinecone
    vector database containing chunked content from *.oregonstate.edu pages.

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

        # Query Pinecone
        index = _get_pinecone_index()
        results = index.query(
            vector=query_embedding,
            top_k=top_k,
            include_metadata=True,
        )

        # Format results
        matches = []
        for match in results.get("matches", []):
            meta = match.get("metadata", {})
            matches.append({
                "text": meta.get("text", ""),
                "url": meta.get("url", ""),
                "title": meta.get("title", ""),
                "score": round(match.get("score", 0.0), 4),
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
You are the **Oregon State University Expert**, an AI assistant with access to \
a comprehensive knowledge base of content from oregonstate.edu.

## How to Answer

1. **Always search first.** For any OSU-related question, call the \
`search_osu_knowledge` tool before answering. Do NOT rely on your training \
data for OSU-specific facts.
2. **Cite your sources.** When using information from the knowledge base, \
include the source URL(s) so the user can verify.
3. **Synthesize clearly.** Combine information from multiple search results \
into a clear, well-organized answer.
4. **Be honest about gaps.** If the knowledge base doesn't contain the answer, \
say so clearly and suggest the user visit oregonstate.edu directly.
5. **Stay on topic.** You specialize in Oregon State University. For unrelated \
questions, politely redirect the user.

## Formatting

- Use markdown for readability (headers, bullet points, bold text).
- Place source links at the end of your answer in a "Sources" section.
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
