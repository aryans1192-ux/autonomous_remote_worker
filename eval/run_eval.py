"""Evaluation harness for the aeps-rca-worker graph.

Runs each golden case through the graph, records the tool chain, fallback flag,
pass/fail, latency, and token usage. Writes logs/eval_report.json + a summary table.

Usage:
    python eval/run_eval.py --offline   # deterministic paths only (no LLM API needed)
    python eval/run_eval.py --live      # needs GROQ_API_KEY (or configured provider)

Offline mode replays the golden tool path per case via a scripted fake LLM, so the
deterministic data-constrained and RCA-report assertions still exercise the real graph,
service, engine, and repository — only the LLM itself is faked.
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from langchain_core.messages import AIMessage, HumanMessage

EVAL_DIR = Path(__file__).resolve().parent
GOLDEN = EVAL_DIR / "golden_cases.json"
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

# Update rates when models/pricing change. Costs are per 1M tokens (USD, approximate).
PRICING = {
    "openai/gpt-oss-120b": {"input": 0.15, "output": 0.60},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "claude-3-5-haiku-latest": {"input": 0.80, "output": 4.00},
}


def load_golden() -> list[dict]:
    with open(GOLDEN, encoding="utf-8") as f:
        return json.load(f)


def run_live(case: dict) -> dict:
    from agent.graph import get_graph

    started = time.monotonic()
    try:
        result = asyncio.run(get_graph().ainvoke(
            {"messages": [HumanMessage(content=case["prompt"])], "fallback": "", "fallback_reason": ""},
            config={"recursion_limit": 12},
        ))
        error = None
    except Exception as e:  # noqa: BLE001 - eval harness reports all failures
        error = str(e)
        result = None

    latency_ms = int((time.monotonic() - started) * 1000)
    reply = ""
    tool_names = []
    fallback = None
    token_usage = None

    if result:
        messages = result["messages"]
        last = messages[-1]
        reply = last.content if hasattr(last, "content") else str(last)
        fallback = result.get("fallback") or None
        for m in messages:
            for tc in getattr(m, "tool_calls", None) or []:
                tool_names.append(tc["name"])
        for m in reversed(messages):
            usage = getattr(m, "usage_metadata", None)
            if usage:
                token_usage = usage
                break

    return {
        "reply": reply, "tool_names": tool_names, "fallback": fallback,
        "latency_ms": latency_ms, "token_usage": token_usage, "error": error,
    }


def run_offline(case: dict) -> dict:
    """Replay the expected tool path deterministically through the real graph.

    Each expected tool is called once with representative args; the final synthetic
    answer echoes the last tool result so substring checks still exercise the real
    service/engine/repository output.
    """
    from unittest.mock import patch
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from agent.graph import build_graph
    from tests.fake_llm import BindableFakeChatModel

    args_for = {
        "get_problem_hours": {"district": "", "merchant": "", "date": "2026-08-25"},
        "get_district_summary": {"district": "SITAPUR", "date": "2026-08-25"},
        "list_districts": {"date": "2026-08-25"},
        "list_merchants_in_district": {"district": "BHIWANI", "date": "2026-08-25"},
    }
    # run_rca_for_merchant args are per-case (merchant determines the expected output)
    rca_merchants = {
        "merchant_rca_gateway": "MERCHANT_GATEWAY_001",
        "merchant_rca_lockup_critical": "MERCHANT_LOCKUP_001",
        "merchant_error_404_data_constrained": "MERCHANT_ERROR_404",
        "pii_scrub": "MERCHANT_LOCKUP_001",
        "no_data_case": "MERCHANT_DOES_NOT_EXIST",
        "multi_turn_drill_down_2": "MERCHANT_GATEWAY_001",
    }
    expect_tool = case.get("expect_tool") or []
    script = []
    for i, tool_name in enumerate(expect_tool):
        if tool_name == "run_rca_for_merchant":
            args = {"merchant": rca_merchants.get(case["id"], "MERCHANT_GATEWAY_001"), "date": "2026-08-25"}
        else:
            args = args_for[tool_name]
        script.append(AIMessage(content="", tool_calls=[{
            "name": tool_name, "args": args, "id": f"call_{i}", "type": "tool_call",
        }]))
    # Final answer: reuse the last tool result so reply-content checks are meaningful
    script.append(AIMessage(content="[offline] final answer follows the tool result."))

    fake = BindableFakeChatModel(responses=script)
    with patch("agent.graph.build_llm", return_value=fake):
        g = build_graph(mcp_tools=None)

    started = time.monotonic()
    try:
        result = asyncio.run(g.ainvoke(
            {"messages": [HumanMessage(content=case["prompt"])], "fallback": "", "fallback_reason": ""},
            config={"recursion_limit": 12},
        ))
        error = None
    except Exception as e:  # noqa: BLE001
        error = str(e)
        result = None

    latency_ms = int((time.monotonic() - started) * 1000)
    reply = ""
    tool_names = []
    fallback = None
    if result:
        messages = result["messages"]
        last = messages[-1]
        reply = last.content if hasattr(last, "content") else str(last)
        fallback = result.get("fallback") or None
        for m in messages:
            for tc in getattr(m, "tool_calls", None) or []:
                tool_names.append(tc["name"])
        # Append real tool outputs so substring checks hit actual service output
        tool_outputs = [
            m.content for m in messages
            if isinstance(m, ToolMessage) and isinstance(m.content, str)
        ]
        if tool_outputs:
            reply = reply + "\n" + "\n".join(tool_outputs)

    return {
        "reply": reply, "tool_names": tool_names, "fallback": fallback,
        "latency_ms": latency_ms, "token_usage": None, "error": error,
    }


def cost_usd(token_usage: dict | None) -> float:
    if not token_usage:
        return 0.0
    model = token_usage.get("model_name", "")
    pricing = PRICING.get(model)
    if not pricing:
        return 0.0
    return (
        token_usage.get("input_tokens", 0) / 1e6 * pricing["input"]
        + token_usage.get("output_tokens", 0) / 1e6 * pricing["output"]
    )


def evaluate(case: dict, mode: str) -> dict:
    outcome = run_live(case) if mode == "live" else run_offline(case)

    checks = {
        "expected_tool_used": bool(outcome["tool_names"]) and all(
            t in outcome["tool_names"] for t in case.get("expect_tool") or []
        ),
        "expected_substrings": all(
            s.lower() in outcome["reply"].lower()
            for s in (case.get("expect_substrings") or [])
        ),
        "no_forbidden_substrings": not any(
            s.lower() in outcome["reply"].lower()
            for s in (case.get("must_not_contain") or [])
        ),
        "fallback_matches": (
            (case.get("expect_fallback") or None) == outcome["fallback"]
        ),
    }
    passed = all(checks.values()) and outcome["error"] is None

    return {
        "id": case["id"],
        "prompt": case["prompt"],
        "passed": passed,
        "checks": checks,
        "error": outcome["error"],
        "tool_names": outcome["tool_names"],
        "fallback": outcome["fallback"],
        "latency_ms": outcome["latency_ms"],
        "token_usage": outcome["token_usage"],
        "cost_usd": round(cost_usd(outcome["token_usage"]), 6),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["live", "offline"], default="offline",
                        help="Alias for --offline/--live; kept for compatibility")
    parser.add_argument("--offline", action="store_true", help="Run deterministic paths only (no LLM API)")
    parser.add_argument("--live", action="store_true", help="Run against the real LLM (needs API key)")
    parser.add_argument("--case", default=None, help="Run only one case id")
    args = parser.parse_args()

    if args.live:
        mode = "live"
    elif args.offline:
        mode = "offline"
    else:
        mode = args.mode

    cases = load_golden()
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]

    results = [evaluate(c, mode) for c in cases]

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        "mode": args.mode,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in results if r["passed"]),
            "total_latency_ms": sum(r["latency_ms"] for r in results),
            "total_cost_usd": round(sum(r["cost_usd"] for r in results), 6),
        },
        "results": results,
    }
    report_path = LOG_DIR / "eval_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n{'CASE':<36} {'PASS':<6} {'TOOLS':<24} {'FALLBACK':<18} {'LATENCY(ms)':<12} COST($)")
    print("-" * 100)
    for r in results:
        print(
            f"{r['id']:<36} {'PASS' if r['passed'] else 'FAIL':<6} "
            f"{','.join(r['tool_names'])[:24]:<24} {str(r['fallback']):<18} "
            f"{r['latency_ms']:<12} {r['cost_usd']:.6f}"
        )
        if not r["passed"] and r["error"]:
            print(f"    error: {r['error'][:200]}")
    s = report["summary"]
    print("-" * 100)
    print(f"TOTAL: {s['passed']}/{s['total']} passed | total latency {s['total_latency_ms']}ms | "
          f"total cost ${s['total_cost_usd']}")
    print(f"Report written to {report_path}")


if __name__ == "__main__":
    main()
