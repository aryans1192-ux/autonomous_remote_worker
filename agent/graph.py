import os
import logging
from typing import Optional

from dotenv import load_dotenv
from functools import lru_cache
from langchain_core.messages import SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

from agent.state import AgentState
from agent.llm import build_llm, _resolve_order
from services.rca_service import rca_service
from core.prompts import SYSTEM_PROMPT, DATA_CONSTRAINED_FALLBACK_MESSAGE
from core.rca_engine import InsufficientTelemetryError

DEBUG = os.getenv("RCA_DEBUG", "0") == "1"
log = logging.getLogger("aeps.agent")

load_dotenv()


@tool
def get_problem_hours(district: str = "", merchant: str = "", date: str = "2026-08-25") -> str:
    """List all high-failure hours for a district, merchant, or date. Leave district/merchant blank for all."""
    return rca_service.get_problem_hours_summary(date=date, district=district, merchant=merchant)


@tool
def run_rca_for_merchant(merchant: str, date: str = "2026-08-25", hour: Optional[int] = None) -> str:
    """Run full RCA for a merchant. Omit hour for all problem hours, or pass specific hour (0-23). Never pass text like 'morning' — omit hour instead."""
    return rca_service.run_merchant_rca(merchant=merchant, date=date, hour=hour if hour is not None else -1)


@tool
def get_district_summary(district: str, date: str = "2026-08-25") -> str:
    """Get weighted day-level summary for a district: attempts, failure rate, avg response time, problem hours."""
    return rca_service.get_district_summary(district=district, date=date)


@tool
def list_districts(date: str = "2026-08-25") -> str:
    """List all districts available in the dataset."""
    return rca_service.list_districts(date=date)


@tool
def list_merchants_in_district(district: str, date: str = "2026-08-25") -> str:
    """List all merchants in a district with high-failure hour flags."""
    return rca_service.list_merchants(district=district, date=date)


ALL_TOOLS = [get_problem_hours, run_rca_for_merchant, get_district_summary, list_districts, list_merchants_in_district]


def build_graph(mcp_tools: list = None):
    llm_order = _resolve_order()
    llm = build_llm(llm_order[0])
    tools = mcp_tools if mcp_tools else ALL_TOOLS
    llm_with_tools = llm.bind_tools(tools)

    async def agent_node(state: AgentState):
        messages = state["messages"]
        if not any(isinstance(m, SystemMessage) for m in messages):
            messages = [SystemMessage(content=SYSTEM_PROMPT)] + messages

        if DEBUG:
            for m in messages:
                if isinstance(m, ToolMessage):
                    log.debug("TOOL RESULT [%s]: %s", m.tool_call_id, m.content[:200])

        last_err = None
        for provider in llm_order:
            try:
                current = llm_with_tools if provider == llm_order[0] else build_llm(provider).bind_tools(tools)
                response = await current.ainvoke(messages)
                if provider != llm_order[0]:
                    log.info("LLM fallback → served by %s", provider)
                break
            except Exception as e:
                err = str(e).lower()
                if any(k in err for k in ("429", "rate_limit", "connection", "timeout", "api_error")):
                    last_err = e
                    log.warning("Provider %s failed (%s); trying next", provider, e)
                    continue
                raise
        else:
            raise RuntimeError(f"All LLM providers failed: {last_err}")

        if DEBUG:
            if hasattr(response, "tool_calls") and response.tool_calls:
                for tc in response.tool_calls:
                    log.debug("TOOL CALL: %s(%s)", tc["name"], tc["args"])
            else:
                log.debug("FINAL ANSWER (%d chars)", len(response.content))

        return {"messages": [response]}

    def should_continue(state: AgentState) -> str:
        last = state["messages"][-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "tools"
        return END

    def after_tools(state: AgentState) -> str:
        if state.get("fallback") == "data_constrained":
            return "fallback"
        return "agent"

    async def fallback_node(state: AgentState):
        return {
            "messages": [
                ToolMessage(
                    content=DATA_CONSTRAINED_FALLBACK_MESSAGE,
                    tool_call_id="data_constrained",
                )
            ],
        }

    async def tools_node(state: AgentState):
        """ToolNode with handle_tool_errors=False wrapped in try/except so
        InsufficientTelemetryError becomes the DATA_CONSTRAINED fallback state
        instead of a crash."""
        try:
            return await ToolNode(tools, handle_tool_errors=False).ainvoke(state)
        except InsufficientTelemetryError as e:
            log.info("DATA_CONSTRAINED fallback: %s", e)
            return {
                "messages": [
                    ToolMessage(content=DATA_CONSTRAINED_FALLBACK_MESSAGE, tool_call_id="data_constrained")
                ],
                "fallback": "data_constrained",
                "fallback_reason": str(e),
            }

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_node("fallback", fallback_node)
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_conditional_edges("tools", after_tools, {"fallback": "fallback", "agent": "agent"})
    graph.add_edge("fallback", END)

    return graph.compile()


@lru_cache(maxsize=1)
def get_graph():
    """Lazily build the compiled graph. Cached so /chat and eval reuse one instance.
    build_graph() must be callable without a provider API key for tests."""
    return build_graph()
