from core.models import (
    TransactionRow, RCAOutcome, GatewayResult, LockupResult, BiometricResult,
)


class InsufficientTelemetryError(Exception):
    """Raised when a merchant row lacks biometric telemetry (successful_auths / total_scans)
    so the biometric check cannot run. Signals a DATA_CONSTRAINED fallback, not a root cause."""

    def __init__(self, merchant_id: str):
        super().__init__(
            f"Biometric telemetry missing for merchant {merchant_id} — "
            "cannot compute local biometric error paths."
        )
        self.merchant_id = merchant_id


LATENCY_THRESHOLD_SEC = 15.0
LATENCY_FAILURE_RATE_MIN = 0.30
BIOMETRIC_MATCH_MIN = 0.70
SUSTAINED_LOCKUP_HOURS = 2


def check_gateway_timeout(row: TransactionRow) -> GatewayResult:
    if row.avg_response_time >= LATENCY_THRESHOLD_SEC and row.failure_rate > LATENCY_FAILURE_RATE_MIN:
        return GatewayResult(
            triggered=True,
            reason=(
                f"Avg gateway response {row.avg_response_time:.1f}s (≥ {LATENCY_THRESHOLD_SEC}s) "
                f"with {row.failure_rate * 100:.0f}% failure rate — NPCI gateway timeout pattern"
            ),
        )
    return GatewayResult(triggered=False)


def check_pending_ledger_lockup(row: TransactionRow, merchant_rows: list[TransactionRow]) -> LockupResult:
    if not row.reversal_pending_flag:
        return LockupResult(triggered=False)

    pending_hours = sorted(r.hour for r in merchant_rows if r.reversal_pending_flag)

    max_consecutive = 1
    current_run = 1
    for i in range(1, len(pending_hours)):
        if pending_hours[i] == pending_hours[i - 1] + 1:
            current_run += 1
            max_consecutive = max(max_consecutive, current_run)
        else:
            current_run = 1

    pending_count = int(row.pending_count)
    amount = row.pending_amount
    if max_consecutive >= SUSTAINED_LOCKUP_HOURS:
        return LockupResult(
            triggered=True,
            sustained=True,
            reason=(
                f"[CRITICAL: {max_consecutive} hours of frozen funds] "
                f"{pending_count} txns PENDING (₹{amount:,.0f} mid-air)"
            ),
        )
    return LockupResult(
        triggered=True,
        sustained=False,
        reason=f"{pending_count} txns PENDING (₹{amount:,.0f} mid-air)",
    )


def check_biometric_device_failure(row: TransactionRow) -> BiometricResult:
    if row.biometric_telemetry_missing:
        raise InsufficientTelemetryError(row.merchant_id)

    if row.total_scans > 0:
        match_ratio = row.successful_auths / row.total_scans
        if match_ratio < BIOMETRIC_MATCH_MIN:
            return BiometricResult(
                triggered=True,
                level="L1_DEVICE_DEFECT",
                reason=(
                    f"Biometric match ratio {match_ratio * 100:.1f}% "
                    f"({int(row.successful_auths)} of {int(row.total_scans)} scans) below {BIOMETRIC_MATCH_MIN * 100:.0f}%"
                ),
            )
    return BiometricResult(triggered=False)


# Deterministic guidance map: owner, next action, escalation per triggered cause
OWNER_GUIDANCE = {
    "gateway": {
        "owner": "Bank Gateway / NPCI Integration",
        "next_action": "Raise bank ticket with gateway timeout evidence; check NPCI outage advisories",
        "escalation": "L2: Escalate to bank TAM if unresolved after 2 hours",
    },
    "lockup": {
        "owner": "Reversal / Ledger Ops",
        "next_action": "Trigger reversal workflow to release pending funds",
        "escalation": "CRITICAL: Immediate escalation to on-call lead — customer funds mid-air",
    },
    "biometric": {
        "owner": "Field Support / Device Vendor",
        "next_action": "Clean lens, update SDK, re-test biometric device",
        "escalation": "L2: Field dispatch if repeated across sessions",
    },
}

CAUSE_KEYS = {
    "gateway": "gateway",
    "lockup": "lockup",
    "biometric": "biometric",
}


def run_rca(row: TransactionRow, merchant_rows: list[TransactionRow]) -> RCAOutcome:
    gateway = check_gateway_timeout(row)
    lockup = check_pending_ledger_lockup(row, merchant_rows)
    biometric = check_biometric_device_failure(row)

    triggered = [
        (CAUSE_KEYS[k], v)
        for k, v in (("gateway", gateway), ("lockup", lockup), ("biometric", biometric))
        if v.triggered and v.reason
    ]

    root_causes = [v.reason for _, v in triggered]
    if not root_causes:
        root_causes = ["No clear root cause identified from available signals"]

    owners, actions, escalations = [], [], []
    for key, _ in triggered:
        g = OWNER_GUIDANCE[key]
        owners.append(g["owner"])
        actions.append(g["next_action"])
        escalations.append(g["escalation"])

    return RCAOutcome(
        merchant_id=row.merchant_id,
        district=row.district,
        hour=row.hour,
        avg_response_time=round(row.avg_response_time, 2),
        failure_rate_pct=round(row.failure_rate * 100, 1),
        total_attempts=int(row.total_attempts),
        pending_count=int(row.pending_count),
        pending_amount=float(row.pending_amount),
        total_scans=int(row.total_scans),
        successful_auths=int(row.successful_auths),
        bank_code=row.bank_code,
        device_id=row.device_id,
        root_causes=root_causes,
        gateway=gateway,
        lockup=lockup,
        biometric=biometric,
        owner="; ".join(dict.fromkeys(owners)) or "Unassigned",
        next_action="; ".join(dict.fromkeys(actions)) or "Review manually",
        escalation="; ".join(dict.fromkeys(escalations)) or "No escalation",
    )


def format_rca_report(results: list[RCAOutcome]) -> str:
    if not results:
        return "No problem hours found."

    blocks = []
    for r in results:
        # Gateway line
        if r.gateway.triggered:
            gateway_line = f"YES — {r.gateway.reason}"
        else:
            gateway_line = (
                f"NO — {r.avg_response_time:.1f}s avg response, "
                f"{r.failure_rate_pct}% failure rate"
            )

        # Lockup line
        if r.lockup.triggered:
            lockup_line = f"YES — {r.lockup.reason}"
        else:
            lockup_line = f"NO — {int(r.pending_count)} pending txns"

        # Biometric line
        if r.biometric.triggered:
            biometric_line = f"YES — {r.biometric.reason}"
        elif r.total_scans > 0:
            ratio = round((r.successful_auths / r.total_scans) * 100, 1)
            biometric_line = f"NO — match ratio {ratio}% ({r.successful_auths} of {r.total_scans} scans)"
        else:
            biometric_line = "NO — no scans recorded"

        summary = "; ".join(r.root_causes)

        block = (
            f"### {r.merchant_id} — Hour {r.hour}:00 — avg response: {r.avg_response_time:.1f}s (device {r.device_id})\n\n"
            f"1. Gateway Timeout: {gateway_line}\n"
            f"2. Pending Ledger Lockup: {lockup_line}\n"
            f"3. Biometric Device: {biometric_line}\n\n"
            f"**Impact**: ₹{r.pending_amount:,.0f} mid-air across {r.pending_count} pending txn(s)\n\n"
            f"**Owner**: {r.owner}\n"
            f"**Next Action**: {r.next_action}\n"
            f"**Escalation**: {r.escalation}\n\n"
            f"**Summary**: Failure rate {r.failure_rate_pct}% — {summary}"
        )
        blocks.append(block)

    header = f"RCA for {results[0].merchant_id} — {len(results)} problem hour(s) shown:\n\n"
    return header + "\n\n---\n\n".join(blocks)
