# Business Resolution Operator

A compact AI employee prototype for autonomous invoice exception handling. The system takes a short business request, gathers the relevant evidence, decides whether a correction is allowed, executes the bounded action, verifies the result, and asks for approval when policy requires it.

This project is intentionally focused on a narrow, working workflow rather than a broad enterprise platform. It demonstrates the core autonomous operating loop expected in the Founding Engineer brief: goal → understand → plan → act → observe → adapt → verify → complete.

## Why this project exists

Many business tasks require more than an answer; they require actual completion of work across multiple systems, records, and policies. Typical AI assistants stop at generating text. This prototype goes one step further by:

- understanding a business objective
- reading authoritative records and evidence
- reconciling conflicting signals
- checking policy and approval boundaries
- performing a controlled write
- re-reading state before claiming completion
- recovering from a false-success failure when needed

## What it demonstrates

The app models a realistic invoice-resolution workflow for synthetic data:

1. A user submits a business request for an invoice issue.
2. The operator gathers invoice, PO, receipt, amendment, and policy evidence.
3. It determines the correct target amount from approved business records.
4. It checks whether the change is allowed under the autonomous policy threshold.
5. It performs the write only when the action is authorized and idempotent.
6. It verifies the result by re-reading the business state.
7. If verification fails, it recovers within a bounded retry window.
8. If the correction exceeds the autonomous limit, it stops and waits for human approval.

## Demo scenarios

The application includes three synthetic but meaningful scenarios:

- Autonomous resolution: a valid invoice mismatch is corrected using approved evidence.
- Recovery: a false-success action is injected, and the system must detect the failure and recover.
- Approval boundary: a value above the autonomous threshold requires explicit human approval before the write is allowed.

## Architecture

```text
User request
   ↓
Streamlit app
   ↓
LangGraph agent loop
   ↓
Tools / evidence retrieval
   ↓
DuckDB business state + audit ledger
   ↓
Policy checks + approval gating + verification
```

Key design choices:

- bounded, deterministic workflow instead of free-form agent action
- authoritative evidence as the source of truth
- write approvals tied to business policy
- revision checks and idempotency keys for safe writes
- verification by independent readback instead of trusting tool success alone

## Tech stack

- Python 3.10+
- Streamlit for the work console
- LangGraph for the agent loop
- DuckDB for local business-state storage
- Groq as the default LLM provider
- Pytest for verification and regression checks

## Repository structure

```text
.
├── agent/
│   ├── graph.py
│   ├── invoice_graph.py
│   ├── llm.py
│   └── state.py
├── api/
│   ├── app.py
│   ├── routes.py
│   └── schemas.py
├── core/
│   ├── invoice_operator.py
│   ├── models.py
│   ├── pii.py
│   ├── prompts.py
│   └── rca_engine.py
├── data/
│   └── invoice_operator.duckdb
├── db/
│   ├── database.py
│   └── repository.py
├── eval/
│   ├── golden_cases.json
│   └── run_eval.py
├── frontend/
│   ├── app.py
│   └── operator_app.py
├── mcp_server/
│   └── server.py
├── services/
│   └── rca_service.py
├── tests/
│   ├── test_api.py
│   ├── test_engine.py
│   ├── test_graph.py
│   ├── test_invoice_operator.py
│   ├── test_pii.py
│   └── test_repository.py
├── .env.example
├── .gitignore
├── langgraph.json
├── Procfile
├── README.md
├── requirements.txt
├── run.py
└── streamlit_app.py
```

## Local setup

Requirements: Python 3.10+.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Set the environment variables in `.env`:

```env
LLM_PROVIDER=groq
GROQ_MODEL=openai/gpt-oss-120b
GROQ_API_KEY=your_groq_api_key_here
```

Then launch the UI:

```powershell
streamlit run frontend/operator_app.py
```

Open http://localhost:8501.

If no API key is configured, the app still runs the offline seeded scenarios and demonstrates the same deterministic business workflow without external model calls.

## Security and credentials

This project intentionally does not commit secrets. Follow these rules:

- keep `.env` local and untracked
- never commit real API keys or credentials
- use `.env.example` as the template for required variables
- add any live secrets to your local environment only

The repository already includes a `.gitignore` rule to exclude `.env` and similar secret files.

## Verification

Run the test suite:

```powershell
python -m pytest -q
```

This project is validated through automated tests covering:

- offline business scenarios
- policy-gated action handling
- agent/tool loop behavior
- invoice operator correctness

## Project assumptions and limits

This is a prototype, not a production ERP integration.

Current assumptions:

- all records are synthetic and local
- the workflow is single-user and local-first
- no real ERP, banking, or external system integration is used
- human approval boundary is intentionally simplified
- verification and auditability are demonstrated in a bounded demo environment

## Future direction

This prototype can evolve into a broader AI employee runtime by adding:

- persistent company memory
- multi-step workflow orchestration
- real browser and desktop automation
- stronger audit and provenance tracking
- more realistic enterprise connectors
- background task execution and retry policies

## Summary

This project demonstrates a practical AI employee pattern for a real business problem: autonomous completion of work with verification and human oversight, instead of simple answer generation. It is intentionally narrow, but its architecture and operating pattern are the right foundation for a more general company AI operator.
