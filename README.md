# Eko AePS Transaction-Failure RCA Worker

An AI worker that performs Root Cause Analysis on **AePS cash-out transaction failures** across Eko's micro-entrepreneur network. A merchant's terminal starts failing; the agent queries real transaction data, runs the diagnostic playbook, and returns a structured root cause analysis — in seconds, without SQL.

## The Problem

When a rural micro-entrepreneur's AePS terminal starts failing, an analyst has to:

1. Open a SQL editor and run multiple queries across merchant × hour tables
2. Cross-reference results against the diagnostic playbook — is it a **gateway timeout**? **Frozen pending funds**? **A dead fingerprint sensor**?
3. Write a summary report — for each merchant, for each problem hour

That's **20–40 minutes per merchant**, requires SQL skills, and happens *after* the damage is done. On a bad day there are dozens of merchants with problem hours across 8 districts.

**This worker replaces that entire flow with a conversation.**

## What the Worker Does

The worker receives a plain-English question, decides which data to fetch, runs the RCA checks, and returns a structured answer. It supports multi-turn conversations — drill-downs, follow-ups, city/store context switches — all within one session.

```
User: "Which districts had the most failures today?"
Agent: [calls get_problem_hours] → district-level summary of merchants affected

User: "Summarize BHIWANI"
Agent: [calls get_district_summary] → weighted failure rate, avg response, problem hours

User: "Which merchants there are worst?"
Agent: [calls list_merchants_in_district] → merchant list ranked by problem hours

User: "Run RCA for MERCHANT_GATEWAY_002"
Agent: [calls run_rca_for_merchant] → gateway timeout, lockup, and biometric checks per hour

User: "Run RCA for MERCHANT_ERROR_404"
Agent: [calls run_rca_for_merchant] → DATA_CONSTRAINED — missing telemetry, asks for terminal logs
```

No query rewriting. No SQL. No re-explaining context on each turn.

## System Design

### Full stack

```
┌─────────────────────────────────────────────────────────┐
│ Browser                                                │
│ Streamlit UI (frontend/app.py) · port 8501             │
└─────────────────────┬───────────────────────────────────┘
                      │ POST /chat {session_id, message}
                      ▼
┌─────────────────────────────────────────────────────────┐
│ FastAPI (api/) · port 8000                             │
│ • Session store (_sessions: dict[uuid → messages])     │
│ • History trim (_trim: last 6 msgs, 300 char cap)      │
│ • Retry + timeout (75s per attempt, 2 retries on 429)  │
│ • PII-scrubbed audit log (logs/audit.jsonl)            │
└─────────────────────┬───────────────────────────────────┘
                      │ graph.ainvoke({messages})
                      ▼
┌─────────────────────────────────────────────────────────┐
│ LangGraph ReAct Loop (agent/)                          │
│                                                         │
│  ┌──────────┐  tool_calls?  ┌──────────────────────┐   │
│  │  agent   │ ─────────────►│ ToolNode             │   │
│  │  node    │ ◄─────────────│ (MCP or direct)      │   │
│  └────┬─────┘  ToolMessage  └──────────────────────┘   │
│       │ no tool_calls                                  │
│       ▼                                                │
│      END                                               │
└───────────────┬──────────────────────┬─────────────────┘
                │                      │
                ▼                      ▼
┌──────────────────┐   ┌─────────────────────────────────┐
│ Direct tools     │   │ MCP server (mcp_server/)        │
│ (agent/graph.py) │   │ FastMCP · port 8001             │
│ 5 @tool fns      │   │ HTTP transport                  │
└────────┬─────────┘   └──────────────┬──────────────────┘
         │                            │
         └──────────┬─────────────────┘
                    │ both call
                    ▼
┌─────────────────────────────────────────────────────────┐
│ RCA Service (services/rca_service.py)                  │
│ Composes repository queries + engine checks + PII scrub │
└──────────────┬──────────────────────┬──────────────────┘
               ▼                      ▼
┌──────────────┐   ┌─────────────────────────────────────┐
│ Repository   │   │ RCA Engine (core/rca_engine.py)     │
│ (db/)        │   │ Pure functions, zero I/O            │
│ DuckDB SQL   │   │ gateway + lockup + biometric checks │
└──────────────┘   └─────────────────────────────────────┘
```

### Startup sequence (`run.py`)

1. `setup()` — loads the CSV from `data/` into `db/aeps.db` (DuckDB)
2. Spawns `mcp_server/server.py` as a subprocess on port 8001
3. 3-second sleep, then starts FastAPI on port 8000
4. FastAPI lifespan: `MultiServerMCPClient` connects to MCP, loads the 5 tools, wires them into the LangGraph graph

## The RCA Logic

### Data model

The source table is **merchant × hour** grain. One row = one merchant, one hour of one day (~744 rows, 8 districts).

| Column | Meaning |
|---|---|
| `merchant_id`, `district`, `charge_date`, `hour` | Identity — micro-entrepreneur, rural zone, time |
| `total_attempts`, `failed_count`, `failure_rate` | Cash-out volume and error rate that hour |
| `is_high_failure_hour` | 1 if flagged for RCA |
| `reversal_pending_flag`, `pending_count`, `pending_amount` | Txns stuck PENDING — money mid-air (₹ impact) |
| `avg_response_time` | Seconds, fingerprint → NPCI gate response |
| `total_scans`, `successful_auths` | Biometric scan volume / match quality |
| `bank_code`, `device_id` | Owner attribution (SBI/PNB/…, terminal ID) |
| `biometric_telemetry_missing` | Derived flag for data-constrained detection |

### Three independent checks (per problem hour)

**1. Gateway Timeout** — Is NPCI responding slowly?

```
LATENCY_THRESHOLD_SEC = 15.0
if row.avg_response_time >= 15.0 and row.failure_rate > 0.30:
    → GatewayResult(triggered=True)  # "NPCI gateway timeout pattern"
```

**2. Pending Ledger Lockup** — Are funds frozen mid-air? If so, is it sustained?

```
SUSTAINED_LOCKUP_HOURS = 2
if row.reversal_pending_flag and longest-consecutive-pending-run >= 2:
    → LockupResult(sustained=True)  # "[CRITICAL: N hours of frozen funds]"
```

The sustained check looks at *all* pending hours for the merchant that day (the full `merchant_rows`), not just the current row — that's why the whole day's data is passed alongside the single row.

**3. Biometric Device L1** — Is the terminal's fingerprint sensor failing?

```
BIOMETRIC_MATCH_MIN = 0.70
if total_scans > 0 and successful_auths / total_scans < 0.70:
    → BiometricResult(level="L1_DEVICE_DEFECT")
```

**All three checks are independent** — a single problem hour can trigger gateway + lockup + biometric simultaneously. `run_rca` aggregates all triggered reasons into `root_causes`.

### Ownership map (deterministic, per triggered cause)

| Cause | Owner | Next action | Escalation |
|---|---|---|---|
| Gateway | Bank Gateway / NPCI Integration | Raise bank ticket with evidence | L2 after 2h |
| Lockup | Reversal / Ledger Ops | Trigger reversal workflow | **CRITICAL — immediate, on-call lead** |
| Biometric | Field Support / Device Vendor | Clean lens, update SDK, re-test | L2 field dispatch |

### The intentional failure: `DATA_CONSTRAINED`

This is the demo beat the assignment asks for. **Ask: *"Run RCA for MERCHANT_ERROR_404"*.**

This merchant's terminal sent **no biometric telemetry at all** — blank `successful_auths` / `total_scans`. Instead of a division-by-zero crash (or worse, a hallucinated biometric result), the engine raises `InsufficientTelemetryError` *before* any division, and the graph routes to a `DATA_CONSTRAINED` fallback node:

> "Insufficient terminal logs collected from this merchant device to calculate local biometric error paths… treat as DATA_CONSTRAINED. Recommended next step: request the terminal's local error log and re-collect telemetry before running RCA."

The worker stops, says what it **cannot** determine, and gives a human the exact next step. No crash, no fabrication. Works identically through the direct-tool and MCP paths — and it's locked in by a test and an eval golden case.

## The 5 Tools

Both the direct tools (`agent/graph.py`) and MCP tools (`mcp_server/server.py`) expose the same 5 operations. Both call the same `rca_service` — the MCP server is purely a transport wrapper.

| Tool | What it does | When the agent uses it |
|---|---|---|
| `list_districts` | All districts in the dataset | First turn, broad queries |
| `get_district_summary` | Weighted failure rate, avg response, problem hours | District-level "how did X do?" |
| `list_merchants_in_district` | Merchants ranked by problem hours | "Which merchants were worst?" |
| `get_problem_hours` | Problem hours (broad = district aggregate, filtered = row-level, cap 50) | Scoping before running RCA |
| `run_rca_for_merchant` | Full gateway/lockup/biometric RCA (5 worst hours) | "Run RCA for MERCHANT_X" |

### Token-safe query design

Broad queries return a **district aggregate** (not all 744 rows). Filtered queries cap at 50 rows. RCA returns the **5 worst hours** by failure rate. This keeps tool results under ~500 tokens so history doesn't overflow the LLM's context.

## Technology Choices and Tradeoffs

### LLM: Groq + `openai/gpt-oss-120b`

**Chose:** Groq, running `openai/gpt-oss-120b` (~500 t/s, 131K context, built for agentic tool-calling). The original Llama 4 Scout model was deprecated by Groq on July 17, 2026; GPT-OSS-120B is the official recommended replacement.

**Why Groq:** fast enough for real conversation, with a usable free/developer tier. For an ops tool that needs sub-5s response time, latency matters.

**Tradeoff:** rate limits on the free tier. Handled via history trimming + automatic provider fallback (OpenAI/Anthropic) with retry/backoff on 429.

### Orchestration: LangGraph

**Why LangGraph over a plain while loop with manual tool dispatch:** that works for one tool call, but for multi-step plans ("list districts → list merchants → run RCA → summarize") you'd be rebuilding the ReAct loop manually. LangGraph gives it for free: the graph is explicit (`agent → tools → agent → END`), with a `recursion_limit` and a dedicated `fallback` state for data-constrained cases.

### Tool Protocol: MCP + direct tools

**Why both:** direct tools are simpler and equally correct. The MCP path (FastMCP on HTTP, port 8001) exists so the worker plugs into any MCP-compatible client — LangGraph Studio, Claude Desktop — without touching the tool logic. Both call `rca_service`. Zero logic duplication.

**Why HTTP transport over stdio:** stdio spawns a subprocess over pipes, and Windows `asyncio` has a known `ProactorEventLoop` issue there — it raises `ExceptionGroup` on async errors. HTTP sidesteps this entirely.

### Database: DuckDB

**Why DuckDB over SQLite:** DuckDB is column-oriented and built for analytical queries — `SUM`, `GROUP BY`, window functions. The district summary uses weighted averages over hundreds of rows; DuckDB runs these ~10× faster. It also reads CSV natively with `read_csv_auto` — no schema definition needed.

**Why not PostgreSQL:** requires a running server, credentials, and a migration. DuckDB is file-based — `duckdb.connect("orders.db")` is all the setup.

**Thread safety:** every query opens a fresh `read_only=True` connection via context manager. `setup()` writes once at startup and closes the write connection; after that everything is read-only.

### API: FastAPI

**Why FastAPI over Flask:** it's async-native. The LangGraph `graph.ainvoke()` call is async — in Flask you'd need threading hacks to not block the server. FastAPI also generates OpenAPI docs at `/docs` for free.

**Session management:** plain in-memory `dict[session_id → messages]`. No Redis, no database — sessions are short-lived ops investigations, and history is already bounded to 6 messages by `_trim()`. If the server restarts, sessions reset — acceptable for an ops tool.

### History Trimming: `_trim()` in `api/routes.py`

```
MAX_HISTORY = 6          # keep last 6 messages
TOOL_RESULT_MAX_CHARS = 300  # truncate tool results to 300 chars
```

On turn 1 the agent calls `list_merchants_in_district("BHIWANI")` and gets a large list back. On turn 4, that full list is still in history. Without trimming, the LLM receives a bloated context, hits TPM limits, and returns 400s.

**The MCP content-format fix:** MCP tool results come back as `list[{'type': 'text', 'text': '...'}]`, not a plain string. The original `len(m.content) > 300` check measured `len(list) = 1`, so truncation never fired. The fix extracts the text first, then measures.

### Frontend: Streamlit

**Why Streamlit over React:** internal ops tool, not a consumer product. Streamlit gives a production-quality chat UI in ~75 lines of Python; React would need a JS build pipeline, npm dependencies, CORS config, and a deploy step. Gradio's `ChatInterface` doesn't compose cleanly with server-side session IDs; Streamlit gives full control over `st.session_state`.

## Setup

### Prerequisites

* Python 3.10+ (3.13 tested)
* A [Groq](https://console.groq.com) API key (free tier)

### Install

```
git clone <your-repo-url>
cd aeps-rca-worker
pip install -r requirements.txt
```

### Configure

```
# .env
GROQ_API_KEY=your_groq_api_key_here
```

`LLM_PROVIDER`, `LLM_FALLBACK_ORDER`, `RCA_DATE` are optional — see `.env.example`.

### Run

**Terminal 1** — backend + MCP server:

```
python run.py
```

**Terminal 2** — frontend:

```
streamlit run frontend/app.py
```

Open **http://localhost:8501**.

### Debug mode

```
python run.py --debug
# or
RCA_DEBUG=1 python run.py
```

Prints every tool call, tool result (first 200 chars), and final answer length to the terminal.

## Sample Test Sequence

Run in a single session to exercise all layers:

```
1. Which districts are in the dataset?
   → calls list_districts
2. What's the overall summary for BHIWANI?
   → calls get_district_summary
3. Which merchants in BHIWANI had problem hours?
   → calls list_merchants_in_district
4. Run RCA for MERCHANT_GATEWAY_002.
   → gateway timeout report
5. Walk me through just the morning hours there.
   → no new tool call; agent filters from turn 4's result
6. Was there a sustained pending ledger lockup at MERCHANT_LOCKUP_002?
   → CRITICAL frozen-funds report
7. Run RCA for MERCHANT_ERROR_404.
   → DATA_CONSTRAINED — the intentional failure beat
8. Which district had the worst failure rate overall?
   → calls get_district_summary for multiple districts
9. Is MERCHANT_DOES_NOT_EXIST having issues today?
   → graceful "no data found" handling
```

Questions 5–6 test multi-turn history (the `_trim()` fix). Question 9 tests graceful no-data handling.

## Tests & Evaluation

```
pytest tests/ -v                    # 41 tests: engine, repo, PII, API, graph
python eval/run_eval.py --offline   # 10 golden cases, deterministic, no API key
python eval/run_eval.py --live      # same cases against the real LLM
```

The eval harness checks expected tool use, expected substrings, **forbidden output** (hallucination check), and fallback routing — recording latency and cost per run to `logs/eval_report.json`.

## Privacy — DPDP

See [docs/DPDP_PRIVACY.md](docs/DPDP_PRIVACY.md). Short version:

- Data stays local (CSV + DuckDB on the analyst's machine)
- **Aadhaar-like** 12-digit runs and **Indian mobiles** are scrubbed at the single service-layer choke point **and** on the final `/chat` reply
- Audit logs store only scrubbed text; only aggregated, non-PII data may ever flow to Eko

## Coverage

- **Date:** 2026-08-25
- **Districts:** BHIWANI, REWARI, JHUNJHUNU, SITAPUR, BAREILLY, NUH, DARBHANGA, BHAGALPUR
- **Merchants:** 31, with planted failure profiles (gateway, lockup, biometric, combined, clean, data-constrained)
- **Records:** 744 merchant × hour rows
