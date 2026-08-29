import os
import logging

from dotenv import load_dotenv
from langchain_core.language_models.chat_models import BaseChatModel

from agent.state import AgentState
from core.prompts import SYSTEM_PROMPT, DATA_CONSTRAINED_FALLBACK_MESSAGE
from core.rca_engine import InsufficientTelemetryError

log = logging.getLogger("aeps.agent")

load_dotenv()

MODELS = {
    "groq": ("langchain_groq", "ChatGroq", "openai/gpt-oss-120b", "GROQ_API_KEY"),
    "openai": ("langchain_openai", "ChatOpenAI", "gpt-4o-mini", "OPENAI_API_KEY"),
    "anthropic": ("langchain_anthropic", "ChatAnthropic", "claude-3-5-haiku-latest", "ANTHROPIC_API_KEY"),
}


def _resolve_order() -> list[str]:
    """Provider + fallback order from env. Resolved once at build_graph() time and
    closed over by agent_node — no runtime config threading needed."""
    provider = os.getenv("LLM_PROVIDER", "groq").strip().lower()
    fallback_env = os.getenv("LLM_FALLBACK_ORDER", "").strip()
    if fallback_env:
        order = [p.strip().lower() for p in fallback_env.split(",") if p.strip()]
        if provider not in order:
            order.insert(0, provider)
    else:
        order = [provider] + [p for p in ("groq", "openai", "anthropic") if p != provider]
    return order


def build_llm(provider: str) -> BaseChatModel:
    module_name, class_name, model, key_env = MODELS[provider]
    module = __import__(module_name, fromlist=[class_name])
    cls = getattr(module, class_name)
    api_key = os.getenv(key_env)
    # A missing key would crash graph build; pass a sentinel so the stack boots
    # keyless. Real invocations surface a clear error from the provider client.
    if not api_key:
        api_key = f"missing-{key_env}-set-it-in-.env"
    return cls(model=model, api_key=api_key, temperature=0)


def make_agent_node(llm, tools, mcp_tools, llm_order):
    llm_with_tools = llm.bind_tools(tools)

    def agent_node(state: AgentState):
        messages = state["messages"]
        if not any(isinstance(m, SystemMessage) for m in messages):
            messages = [SystemMessage(content=SYSTEM_PROMPT)] + messages

        last_err = None
        for provider in llm_order:
            try:
                current = llm if provider == llm_order[0] else _build_for(provider, llm_order[0])
                if provider != llm_order[0]:
                    current = current.bind_tools(tools)
                response = current.invoke(messages)
                if provider != llm_order[0]:
                    log.info("LLM fallback → served by %s", provider)
                return {"messages": [response]}
            except Exception as e:
                err = str(e).lower()
                if any(k in err for k in ("429", "rate_limit", "connection", "timeout", "api_error")):
                    last_err = e
                    log.warning("Provider %s failed (%s); trying next", provider, e)
                    continue
                raise
        raise RuntimeError(f"All LLM providers failed: {last_err}")

    return agent_node


def _build_for(provider: str, primary: str):
    # Build a fresh model for the fallback provider and bind the same tools.
    return build_llm(provider)


def make_tools_node(tools):
    def tools_node(state: AgentState):
        try:
            result = ToolNode(tools).invoke(state)
            return result
        except InsufficientTelemetryError as e:
            log.info("DATA_CONSTRAINED fallback: %s", e)
            return {
                "messages": [ToolMessage(content=DATA_CONSTRAINED_FALLBACK_MESSAGE, tool_call_id="data_constrained")],
                "fallback": "data_constrained",
                "fallback_reason": str(e),
            }
    return tools_node
