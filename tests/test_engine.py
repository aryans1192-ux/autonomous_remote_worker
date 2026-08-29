import math

import pytest

from core.models import TransactionRow
from core.rca_engine import (
    check_gateway_timeout,
    check_pending_ledger_lockup,
    check_biometric_device_failure,
    run_rca,
    format_rca_report,
    InsufficientTelemetryError,
    LATENCY_THRESHOLD_SEC,
    SUSTAINED_LOCKUP_HOURS,
)


def make_row(**overrides) -> TransactionRow:
    base = dict(
        charge_date="2026-08-25",
        merchant_id="MERCHANT_TEST",
        district="BHIWANI",
        hour=12,
        total_attempts=40,
        failed_count=18,
        failure_rate=0.45,
        is_high_failure_hour=1,
        pending_count=0,
        reversal_pending_flag=0,
        avg_response_time=4.0,
        pending_amount=0,
        bank_code="SBI",
        device_id="TERM-000-1234",
        total_scans=30,
        successful_auths=25,
    )
    base.update(overrides)
    return TransactionRow(**base)


def merchant_rows(*rows: TransactionRow) -> list[TransactionRow]:
    return list(rows)


# ---------- Gateway timeout ----------

def test_gateway_triggers():
    row = make_row(avg_response_time=20.0, failure_rate=0.5)
    res = check_gateway_timeout(row)
    assert res.triggered
    assert "gateway" in res.reason.lower()


def test_gateway_latency_below_threshold_no_trigger():
    row = make_row(avg_response_time=LATENCY_THRESHOLD_SEC - 1, failure_rate=0.5)
    assert not check_gateway_timeout(row).triggered


def test_gateway_failure_rate_below_min_no_trigger():
    row = make_row(avg_response_time=25.0, failure_rate=0.1)
    assert not check_gateway_timeout(row).triggered


# ---------- Pending ledger lockup ----------

def test_lockup_sustained():
    row = make_row(reversal_pending_flag=1, pending_count=5, pending_amount=12000, hour=10)
    rows = merchant_rows(
        make_row(hour=9, reversal_pending_flag=1),
        make_row(hour=10, reversal_pending_flag=1),
        make_row(hour=11, reversal_pending_flag=1),
    )
    res = check_pending_ledger_lockup(row, rows)
    assert res.triggered
    assert res.sustained
    assert "CRITICAL" in res.reason
    assert "frozen funds" in res.reason


def test_lockup_not_sustained_single_hour():
    row = make_row(reversal_pending_flag=1, pending_count=3, hour=10)
    rows = merchant_rows(
        make_row(hour=10, reversal_pending_flag=1),
    )
    res = check_pending_ledger_lockup(row, rows)
    assert res.triggered
    assert not res.sustained


def test_lockup_not_triggered_without_flag():
    row = make_row(reversal_pending_flag=0)
    assert not check_pending_ledger_lockup(row, merchant_rows(row)).triggered


def test_lockup_sustained_requires_min_consecutive():
    # Non-consecutive pending hours (gap) should NOT be sustained
    row = make_row(reversal_pending_flag=1, hour=10)
    rows = merchant_rows(
        make_row(hour=8, reversal_pending_flag=1),
        make_row(hour=10, reversal_pending_flag=1),
    )
    res = check_pending_ledger_lockup(row, rows)
    assert res.triggered
    assert not res.sustained


# ---------- Biometric device ----------

def test_biometric_low_match_triggers():
    row = make_row(total_scans=40, successful_auths=20)  # 50%
    res = check_biometric_device_failure(row)
    assert res.triggered
    assert res.level == "L1_DEVICE_DEFECT"


def test_biometric_ok_match_no_trigger():
    row = make_row(total_scans=40, successful_auths=36)  # 90%
    assert not check_biometric_device_failure(row).triggered


def test_biometric_zero_scans_no_trigger():
    row = make_row(total_scans=0, successful_auths=0)
    assert not check_biometric_device_failure(row).triggered


def test_biometric_missing_telemetry_raises():
    row = TransactionRow(
        charge_date="2026-08-25",
        merchant_id="MERCHANT_ERROR_404",
        district="NUH",
        hour=12,
        total_attempts=40,
        failed_count=20,
        failure_rate=0.5,
        is_high_failure_hour=1,
        total_scans=None,
        successful_auths=None,
    )
    assert row.biometric_telemetry_missing is True
    with pytest.raises(InsufficientTelemetryError) as exc:
        check_biometric_device_failure(row)
    assert "MERCHANT_ERROR_404" in str(exc.value)


# ---------- run_rca / report ----------

def test_run_rca_aggregates_multiple_causes():
    row = make_row(
        avg_response_time=22.0,
        failure_rate=0.5,
        reversal_pending_flag=1,
        pending_count=4,
        pending_amount=9000,
        total_scans=30,
        successful_auths=10,
    )
    rows = merchant_rows(
        make_row(hour=11, reversal_pending_flag=1),
        make_row(hour=12, reversal_pending_flag=1),
    )
    out = run_rca(row, rows)
    assert len(out.root_causes) == 3
    assert out.owner and "Gateway" in out.owner
    assert "reversal" in out.next_action.lower()
    assert "CRITICAL" in out.escalation


def test_run_rca_no_cause_placeholder():
    row = make_row(failure_rate=0.05, avg_response_time=3.0)
    out = run_rca(row, merchant_rows(row))
    assert out.root_causes == ["No clear root cause identified from available signals"]


def test_run_rca_raises_on_missing_telemetry():
    row = make_row(total_scans=None, successful_auths=None)
    with pytest.raises(InsufficientTelemetryError):
        run_rca(row, merchant_rows(row))


def test_format_rca_report():
    row = make_row(avg_response_time=22.0, failure_rate=0.5, total_scans=30, successful_auths=10)
    report = format_rca_report([run_rca(row, merchant_rows(row))])
    assert "RCA for MERCHANT_TEST" in report
    assert "Gateway Timeout: YES" in report
    assert "Biometric Device: YES" in report
    assert "Owner" in report
    assert "Next Action" in report
    assert "Escalation" in report


def test_format_rca_report_empty():
    assert format_rca_report([]) == "No problem hours found."
