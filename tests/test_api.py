from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from api.app import create_app
from api.routes import _trim, AUDIT_FILE
from core.pii import scrub_pii


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("api.routes.AUDIT_FILE", tmp_path / "audit.jsonl")

    fake_graph = MagicMock()
    fake_graph.ainvoke = AsyncMock(return_value={
        "messages": [
            HumanMessage(content="why did M1 fail"),
            AIMessage(
                content="final answer with 123456789012 raw",
                tool_calls=[{"name": "run_rca_for_merchant", "args": {"merchant": "M1", "device_id": "9876543210"}, "id": "call_1", "type": "tool_call"}],
            ),
        ],
        "fallback": "data_constrained",
    })

    app = create_app()
    app.state.graph = fake_graph
    return TestClient(app)


def test_chat_round_trip(client, tmp_path):
    r = client.post("/chat", json={"session_id": "s1", "message": "why did M1 fail"})
    assert r.status_code == 200
    data = r.json()
    assert data["turn"] == 1
    assert data["session_id"] == "s1"
    assert "final answer" in data["response"]

    # Reply must be scrubbed
    assert "123456789012" not in data["response"]
    assert "[ANONYMIZED_UID]" in data["response"]


def test_audit_record_written_scrubbed(client, tmp_path):
    client.post("/chat", json={"session_id": "s2", "message": "check UID 123456789012 phone 9876543210"})
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    import json
    rec = json.loads(lines[0])
    assert rec["user_msg_scrubbed"] == "check UID [ANONYMIZED_UID] phone [ANONYMIZED_PHONE]"
    assert "[ANONYMIZED_UID]" in rec["reply_scrubbed"]
    assert "123456789012" not in rec["reply_scrubbed"]
    assert rec["tool_names"] == ["run_rca_for_merchant"]
    # tool args scrubbed
    joined = " ".join(rec["tool_args_scrubbed"])
    assert "9876543210" not in joined
    assert "[ANONYMIZED_PHONE]" in joined
    assert rec["fallback"] is True
    assert rec["latency_ms"] >= 0


def test_new_session(client):
    r = client.post("/session/new")
    assert r.status_code == 200
    assert r.json()["session_id"]


def test_health(client):
    r = client.get("/health")
    assert r.json() == {"status": "ok"}


def test_clear_session(client):
    client.post("/chat", json={"session_id": "s3", "message": "hi"})
    r = client.delete("/session/s3")
    assert r.json() == {"status": "cleared"}


# ---------- _trim ----------

def test_trim_truncates_tool_message_content():
    messages = [
        HumanMessage(content="q"),
        ToolMessage(content="x" * 500, tool_call_id="1"),
        AIMessage(content="a"),
    ]
    trimmed = _trim(messages)
    tool = trimmed[1]
    assert len(tool.content) <= 301
    assert tool.content.endswith("…")


def test_trim_handles_mcp_list_content():
    messages = [
        HumanMessage(content="q"),
        ToolMessage(content=[{"type": "text", "text": "y" * 400}], tool_call_id="1"),
        AIMessage(content="a"),
    ]
    trimmed = _trim(messages)
    tool = trimmed[1]
    assert isinstance(tool.content, str)
    assert len(tool.content) <= 301


def test_trim_keeps_last_six():
    messages = [HumanMessage(content=f"m{i}") for i in range(10)]
    trimmed = _trim(messages)
    assert len(trimmed) == 6
    assert trimmed[0].content == "m4"
