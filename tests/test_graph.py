import asyncio
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.graph import build_graph
from tests.fake_llm import BindableFakeChatModel


@pytest.fixture(scope="module", autouse=True)
def _ensure_db():
    """Graph tests exercise the real tools → repository → DuckDB path, so the
    DB must exist (built from the committed CSV by run.py / setup())."""
    import os
    from db import database
    if not os.path.exists(database.DB_PATH):
        database.setup()


def run_graph(script: list, user_msg: str = "Run RCA for MERCHANT_ERROR_404"):
    fake = BindableFakeChatModel(responses=script)
    with patch("agent.graph.build_llm", return_value=fake):
        graph = build_graph(mcp_tools=None)

    async def _run():
        return await graph.ainvoke(
            {"messages": [HumanMessage(content=user_msg)], "fallback": "", "fallback_reason": ""},
            config={"recursion_limit": 12},
        )

    return asyncio.run(_run())


def test_graph_tool_call_loop():
    """Agent calls a tool, gets a result, then answers."""
    script = [
        AIMessage(content="", tool_calls=[{
            "name": "list_districts", "args": {"date": "2026-08-25"}, "id": "call_1", "type": "tool_call",
        }]),
        AIMessage(content="BHIWANI, REWARI and 6 more districts."),
    ]
    state = run_graph(script, user_msg="Which districts exist?")
    assert isinstance(state["messages"][-1], AIMessage)
    assert "BHIWANI" in state["messages"][-1].content
    # Tool result should be in the message history
    assert any(isinstance(m, ToolMessage) for m in state["messages"])


def test_graph_fallback_routing():
    """Missing telemetry → InsufficientTelemetryError → data_constrained fallback node."""
    script = [
        AIMessage(content="", tool_calls=[{
            "name": "run_rca_for_merchant",
            "args": {"merchant": "MERCHANT_ERROR_404", "date": "2026-08-25"},
            "id": "call_2", "type": "tool_call",
        }]),
        AIMessage(content="This should not be reached."),
    ]
    state = run_graph(script, user_msg="Why is MERCHANT_ERROR_404 failing?")
    # Graph terminates at fallback node; final message is the data-constrained notice
    assert "Insufficient terminal logs" in state["messages"][-1].content
