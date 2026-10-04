import pytest

from agent.invoice_graph import active_model_name
from core.invoice_operator import (
    SCENARIOS,
    agent_apply,
    agent_authorize,
    agent_inspect,
    agent_reconcile,
    agent_verify,
    approve_case,
    create_case,
    get_case,
    get_events,
    get_snapshot,
    grant_approval,
    initialize_database,
    run_operator,
)


@pytest.fixture
def demo_db(monkeypatch, tmp_path):
    monkeypatch.setenv("INVOICE_DB_PATH", str(tmp_path / "demo.duckdb"))
    initialize_database()


def test_groq_model_defaults_to_tool_reasoning_model(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("GROQ_MODEL", raising=False)

    assert active_model_name() == "Groq · openai/gpt-oss-120b"


def test_autonomous_resolution_mutates_and_verifies(demo_db):
    case_id = create_case("autonomous", "Please resolve the issue with invoice INV-1042")

    case = run_operator(case_id)

    assert case["status"] == "COMPLETED"
    snapshot = get_snapshot(SCENARIOS["autonomous"]["invoice_id"])
    assert snapshot["invoice"]["amount"] == 10000
    assert snapshot["invoice"]["status"] == "READY"
    assert any(event["event_type"] == "completed" for event in get_events(case_id))


def test_failed_readback_recovers_with_bounded_retry(demo_db):
    case_id = create_case("recovery", "Resolve this invoice")

    case = run_operator(case_id)

    events = get_events(case_id)
    assert case["status"] == "COMPLETED"
    assert case["attempts"] == 2
    assert any(event["event_type"] == "recovery" for event in events)
    assert any(event["details"].get("injected_demo_failure") for event in events)


def test_high_value_correction_waits_for_exact_human_approval(demo_db):
    case_id = create_case("approval", "Resolve this high-value invoice")
    invoice_id = SCENARIOS["approval"]["invoice_id"]

    pending = run_operator(case_id)

    assert pending["status"] == "HUMAN_REQUIRED"
    assert get_snapshot(invoice_id)["invoice"]["amount"] == 48000
    assert not pending["approval_granted"]

    completed = approve_case(case_id)

    assert completed["status"] == "COMPLETED"
    assert get_snapshot(invoice_id)["invoice"]["amount"] == 50000
    assert any(event["event_type"] == "human_approved" for event in get_events(case_id))


def test_tool_contract_recovers_after_false_success(demo_db):
    case_id = create_case("recovery", "Resolve invoice exception")
    agent_inspect(case_id)
    target = agent_reconcile(case_id)["authoritative_amount"]
    assert agent_authorize(case_id, target)["authorized"]

    first_action = agent_apply(case_id)
    first_verification = agent_verify(case_id)

    assert first_action["applied"] is True
    assert first_verification["passed"] is False
    agent_inspect(case_id)
    agent_reconcile(case_id)
    agent_authorize(case_id, target)
    agent_apply(case_id)
    assert agent_verify(case_id)["passed"] is True


def test_tool_contract_blocks_high_value_write_until_approval(demo_db):
    case_id = create_case("approval", "Resolve high-value invoice")
    agent_inspect(case_id)
    target = agent_reconcile(case_id)["authoritative_amount"]

    assert agent_authorize(case_id, target)["authorized"] is False
    assert agent_apply(case_id)["approval_required"] is True
    assert get_snapshot(SCENARIOS["approval"]["invoice_id"])["invoice"]["amount"] == 48000

    grant_approval(case_id)
    assert agent_authorize(case_id, target)["authorized"] is True
    agent_apply(case_id)
    assert agent_verify(case_id)["passed"] is True