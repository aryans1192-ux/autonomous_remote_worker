import json
import os
import time
import uuid
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from langchain_core.messages import HumanMessage, ToolMessage

from api.schemas import ChatRequest, ChatResponse, NewSessionResponse
from core.pii import scrub_pii

router = APIRouter()

_sessions: dict[str, list] = {}

RESPONSE_TIMEOUT = 75   # per attempt
MAX_RETRIES = 2
MAX_HISTORY = 6
TOOL_RESULT_MAX_CHARS = 300

AUDIT_DIR = Path(os.path.join(os.path.dirname(__file__), "..", "logs"))
AUDIT_FILE = AUDIT_DIR / "audit.jsonl"


def _trim(messages: list) -> list:
    recent = messages[-MAX_HISTORY:] if len(messages) > MAX_HISTORY else messages
    result = []
    for m in recent:
        if isinstance(m, ToolMessage):
            content = m.content
            # MCP tools return list[{'type': 'text', 'text': '...'}]; direct tools return str
            if isinstance(content, list):
                text = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
            else:
                text = str(content)
            if len(text) > TOOL_RESULT_MAX_CHARS:
                m = m.model_copy(update={"content": text[:TOOL_RESULT_MAX_CHARS] + "…"})
        result.append(m)
    return result


def _write_audit(record: dict):
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


@router.post("/session/new", response_model=NewSessionResponse)
async def new_session():
    session_id = str(uuid.uuid4())
    _sessions[session_id] = []
    return NewSessionResponse(session_id=session_id)


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request):
    graph = request.app.state.graph
    started = time.monotonic()

    if req.session_id not in _sessions:
        _sessions[req.session_id] = []

    _sessions[req.session_id].append(HumanMessage(content=req.message))

    result = None
    reply = None
    for attempt in range(MAX_RETRIES):
        try:
            result = await asyncio.wait_for(
                graph.ainvoke(
                    {"messages": _trim(_sessions[req.session_id])},
                    config={"recursion_limit": 12},
                ),
                timeout=RESPONSE_TIMEOUT,
            )
            break

        except asyncio.TimeoutError:
            reply = "Request timed out. The model may be under load — please try again."
            break

        except Exception as e:
            err = str(e)
            if ("429" in err or "rate_limit" in err.lower()) and attempt < MAX_RETRIES - 1:
                await asyncio.sleep(70)
                continue
            elif "recursion" in err.lower():
                reply = "Agent exceeded maximum steps. Please rephrase your question."
            else:
                reply = f"Something went wrong: {err[:200]}"
            break

    tool_names = []
    tool_args_scrubbed = []
    provider_used = None
    token_usage = None
    fallback_flag = False

    if result is not None:
        _sessions[req.session_id] = result["messages"]
        last = result["messages"][-1]
        reply = last.content if hasattr(last, "content") else str(last)
        reply = scrub_pii(reply)  # belt-and-suspenders on the final reply
        fallback_flag = bool(result.get("fallback") == "data_constrained")

        # Extract tool calls/args from the AI messages in this turn's messages
        for m in result["messages"]:
            calls = getattr(m, "tool_calls", None)
            if calls:
                for tc in calls:
                    tool_names.append(tc["name"])
                    tool_args_scrubbed.append(scrub_pii(json.dumps(tc.get("args", {}), default=str)))

        # Token usage, if the model exposes it
        for m in reversed(result["messages"]):
            usage = getattr(m, "usage_metadata", None)
            if usage:
                token_usage = usage
                break

    elif reply is None:
        reply = "Rate limit reached. Retried but still unavailable — please wait a minute."

    latency_ms = int((time.monotonic() - started) * 1000)
    turn = sum(1 for m in _sessions[req.session_id] if isinstance(m, HumanMessage))

    _write_audit({
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": req.session_id,
        "turn": turn,
        "user_msg_scrubbed": scrub_pii(req.message),
        "tool_names": tool_names,
        "tool_args_scrubbed": tool_args_scrubbed,
        "provider_used": provider_used,
        "token_usage": token_usage,
        "latency_ms": latency_ms,
        "fallback": fallback_flag,
        "reply_scrubbed": scrub_pii(reply),
    })

    return ChatResponse(session_id=req.session_id, response=reply, turn=turn)


@router.delete("/session/{session_id}")
async def clear_session(session_id: str):
    _sessions.pop(session_id, None)
    return {"status": "cleared"}


@router.get("/health")
async def health():
    return {"status": "ok"}
