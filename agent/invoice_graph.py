import json
import os
from typing import Annotated

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from typing_extensions import TypedDict

from agent.llm import MODELS, build_llm
from core.invoice_operator import (
    agent_apply,
    agent_authorize,
    agent_inspect,
    agent_reconcile,
    agent_verify,
    get_case,
)


load_dotenv()
SYSTEM_PROMPT = """You are an invoice-resolution operator working only on the supplied case ID.
Resolve the requested business outcome using tools, not guesses. Start by inspecting
the invoice, PO, receipt, approved amendment, and policy. Reconcile the evidence,
then propose only the authoritative amount and check the separate authorization
tool. If approval is required, stop without attempting a write. Otherwise apply the
authorized correction and independently verify the resulting state. A successful
tool response is not proof of completion. If verification fails, inspect again and
retry only when evidence still supports it; never exceed the tool's bounded limit.
Report completed, waiting for approval, or escalated with concise evidence. Do not
claim an action or verification that the tools did not confirm."""


class InvoiceAgentState(TypedDict):
    messages: Annotated[list, add_messages]


@tool
def inspect_business_records(case_id: str) -> str:
    """Read invoice, purchase order, goods receipt, approved amendment, and policy evidence for a case."""
    return json.dumps(agent_inspect(case_id))


@tool
def reconcile_invoice_evidence(case_id: str) -> str:
    """Compare the invoice to authoritative PO/amendment and receipt records; do not mutate state."""
    return json.dumps(agent_reconcile(case_id))


@tool
def authorize_invoice_correction(case_id: str, target_amount: float) -> str:
    """Validate a proposed correction against business evidence and check the approval policy."""
    return json.dumps(agent_authorize(case_id, target_amount))


@tool
def apply_invoice_correction(case_id: str) -> str:
    """Apply the persisted, authorized correction with revision and idempotency checks."""
    return json.dumps(agent_apply(case_id))


@tool
def verify_invoice_resolution(case_id: str) -> str:
    """Reread current invoice and receipt records and verify outcome invariants."""
    return json.dumps(agent_verify(case_id))


TOOLS = [
    inspect_business_records,
    reconcile_invoice_evidence,
    authorize_invoice_correction,
    apply_invoice_correction,
    verify_invoice_resolution,
]


def model_is_configured():
    provider = os.getenv("LLM_PROVIDER", "groq").strip().lower()
    key_names = {"groq": "GROQ_API_KEY", "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
    return bool(os.getenv(key_names.get(provider, "GROQ_API_KEY")))


def active_model_name():
    provider = os.getenv("LLM_PROVIDER", "groq").strip().lower()
    model = os.getenv("GROQ_MODEL", MODELS["groq"][2]) if provider == "groq" else MODELS[provider][2]
    return f"{provider.title()} · {model}"


def build_invoice_graph():
    provider = os.getenv("LLM_PROVIDER", "groq").strip().lower()
    llm = build_llm(provider).bind_tools(TOOLS, parallel_tool_calls=False)

    def agent_node(state):
        messages = state["messages"]
        if not any(isinstance(message, SystemMessage) for message in messages):
            messages = [SystemMessage(content=SYSTEM_PROMPT), *messages]
        return {"messages": [llm.invoke(messages)]}

    def route(state):
        last = state["messages"][-1]
        return "tools" if getattr(last, "tool_calls", None) else END

    graph = StateGraph(InvoiceAgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", ToolNode(TOOLS, handle_tool_errors=False))
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", route, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile()


def run_invoice_agent(case_id, approved=False):
    case = get_case(case_id)
    if not case:
        raise ValueError("Case not found")
    instruction = (
        f"Resolve this business request: {case['request']} Case ID: {case_id}. "
        "Use the case's current persisted state and stop at any approval boundary."
    )
    if approved:
        instruction += " A human has approved the exact persisted proposal; re-check policy before execution."
    result = build_invoice_graph().invoke(
        {"messages": [HumanMessage(content=instruction)]},
        config={"recursion_limit": 18},
    )
    final_message = result["messages"][-1]
    content = final_message.content
    if isinstance(content, list):
        content = " ".join(item.get("text", "") for item in content if isinstance(item, dict))
    return str(content)