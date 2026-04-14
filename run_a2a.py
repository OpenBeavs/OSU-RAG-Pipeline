"""
Run the OSU RAG agent as an A2A server on localhost:8000.

    uvicorn run_a2a:app --host localhost --port 8000

Routes served:
  GET  /.well-known/agent.json  — agent card (discovery)
  POST /                        — A2A JSON-RPC endpoint
"""

from a2a.types import AgentCard, AgentCapabilities, AgentSkill
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from osu_rag_agent.agent import root_agent

agent_card = AgentCard(
    name="OSU RAG Agent",
    description=(
        "An Oregon State University expert agent that answers questions "
        "using a RAG knowledge base of oregonstate.edu content."
    ),
    url="http://localhost:8000/",
    version="1.0.0",
    defaultInputModes=["text/plain"],
    defaultOutputModes=["text/plain"],
    capabilities=AgentCapabilities(),
    skills=[
        AgentSkill(
            id="osu_qa",
            name="OSU Knowledge Base Q&A",
            description="Answer questions about Oregon State University using scraped oregonstate.edu content.",
            tags=["oregon-state", "university", "education", "rag"],
        )
    ],
)

app = to_a2a(root_agent, agent_card=agent_card, port=8000)
