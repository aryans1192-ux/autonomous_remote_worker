import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import duckdb


DB_PATH = Path(__file__).resolve().parents[1] / "data" / "invoice_operator.duckdb"
SCENARIOS = {
    "autonomous": {"label": "Autonomous resolution", "invoice_id": "INV-1042", "invoice": 9800.0, "truth": 10000.0},
    "recovery": {"label": "Verification failure and recovery", "invoice_id": "INV-2042", "invoice": 9800.0, "truth": 10000.0},
    "approval": {"label": "Human approval boundary", "invoice_id": "INV-3042", "invoice": 48000.0, "truth": 50000.0},
}


def _connect():
    path = Path(os.getenv("INVOICE_DB_PATH", str(DB_PATH)))
    path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path))


def _now():
    return datetime.now(timezone.utc).isoformat()


def _one(connection, query, parameters=()):
    cursor = connection.execute(query, parameters)
    row = cursor.fetchone()
    return dict(zip((column[0] for column in cursor.description), row)) if row else None


def initialize_database():
    with _connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS invoices (invoice_id VARCHAR PRIMARY KEY, vendor VARCHAR, amount DOUBLE, due_date VARCHAR, po_id VARCHAR, status VARCHAR, revision INTEGER)")
        db.execute("CREATE TABLE IF NOT EXISTS purchase_orders (po_id VARCHAR PRIMARY KEY, vendor VARCHAR, amount DOUBLE, revision INTEGER)")
        db.execute("CREATE TABLE IF NOT EXISTS goods_receipts (receipt_id VARCHAR PRIMARY KEY, po_id VARCHAR, received_amount DOUBLE, received_at VARCHAR)")
        db.execute("CREATE TABLE IF NOT EXISTS amendments (amendment_id VARCHAR PRIMARY KEY, invoice_id VARCHAR, approved_amount DOUBLE, approved BOOLEAN)")
        db.execute("CREATE TABLE IF NOT EXISTS policies (policy_id VARCHAR PRIMARY KEY, auto_approval_limit DOUBLE, amount_tolerance DOUBLE)")
        db.execute("CREATE TABLE IF NOT EXISTS cases (case_id VARCHAR PRIMARY KEY, invoice_id VARCHAR, scenario VARCHAR, request VARCHAR, status VARCHAR, proposed_amount DOUBLE, approval_granted BOOLEAN, failure_used BOOLEAN, attempts INTEGER, created_at VARCHAR)")
        db.execute("CREATE TABLE IF NOT EXISTS audit_events (event_id VARCHAR PRIMARY KEY, case_id VARCHAR, event_type VARCHAR, summary VARCHAR, details VARCHAR, created_at VARCHAR)")
        db.execute("CREATE TABLE IF NOT EXISTS action_ledger (idempotency_key VARCHAR PRIMARY KEY, case_id VARCHAR, invoice_id VARCHAR, target_amount DOUBLE, result VARCHAR, created_at VARCHAR)")
        for scenario in SCENARIOS.values():
            invoice_id = scenario["invoice_id"]
            po_id = f"PO-{invoice_id[-4:]}"
            db.execute("INSERT INTO purchase_orders VALUES (?, 'Northstar Components', ?, 1) ON CONFLICT DO NOTHING", [po_id, scenario["invoice"] - 300])
            db.execute("INSERT INTO invoices VALUES (?, 'Northstar Components', ?, '2026-10-18', ?, 'EXCEPTION', 1) ON CONFLICT DO NOTHING", [invoice_id, scenario["invoice"], po_id])
            db.execute("INSERT INTO goods_receipts VALUES (?, ?, ?, '2026-09-29') ON CONFLICT DO NOTHING", [f"GR-{invoice_id[-4:]}", po_id, scenario["truth"]])
            db.execute("INSERT INTO amendments VALUES (?, ?, ?, TRUE) ON CONFLICT DO NOTHING", [f"AMD-{invoice_id[-4:]}", invoice_id, scenario["truth"]])
        db.execute("INSERT INTO policies VALUES ('AP-STD-01', 25000, 0.01) ON CONFLICT DO NOTHING")


def _record(case_id, event_type, summary, details=None):
    with _connect() as db:
        db.execute("INSERT INTO audit_events VALUES (?, ?, ?, ?, ?, ?)", [
            str(uuid.uuid4()), case_id, event_type, summary, json.dumps(details or {}), _now(),
        ])


def _tool(case_id, name, summary, details=None):
    _record(case_id, "tool", summary, {"tool": name, **(details or {})})


def create_case(scenario, request):
    initialize_database()
    if scenario not in SCENARIOS:
        raise ValueError("Unknown demo scenario")
    case_id = str(uuid.uuid4())
    invoice_id = SCENARIOS[scenario]["invoice_id"]
    with _connect() as db:
        db.execute("INSERT INTO cases VALUES (?, ?, ?, ?, 'RECEIVED', NULL, FALSE, FALSE, 0, ?)", [
            case_id, invoice_id, scenario, request.strip(), _now(),
        ])
    _record(case_id, "state", "Request accepted; operator is resolving the invoice outcome.", {"request": request, "invoice_id": invoice_id})
    return case_id


def get_case(case_id):
    with _connect() as db:
        return _one(db, "SELECT * FROM cases WHERE case_id = ?", [case_id])


def list_cases(limit=10):
    with _connect() as db:
        cursor = db.execute("SELECT * FROM cases ORDER BY created_at DESC LIMIT ?", [limit])
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def get_events(case_id):
    with _connect() as db:
        cursor = db.execute("SELECT event_type, summary, details, created_at FROM audit_events WHERE case_id = ? ORDER BY created_at, event_id", [case_id])
        columns = [column[0] for column in cursor.description]
        events = [dict(zip(columns, row)) for row in cursor.fetchall()]
    for event in events:
        event["details"] = json.loads(event["details"])
    return events


def get_snapshot(invoice_id):
    with _connect() as db:
        invoice = _one(db, "SELECT * FROM invoices WHERE invoice_id = ?", [invoice_id])
        if not invoice:
            return None
        po = _one(db, "SELECT * FROM purchase_orders WHERE po_id = ?", [invoice["po_id"]])
        receipt = _one(db, "SELECT * FROM goods_receipts WHERE po_id = ?", [invoice["po_id"]])
        amendment = _one(db, "SELECT * FROM amendments WHERE invoice_id = ?", [invoice_id])
        policy = _one(db, "SELECT * FROM policies LIMIT 1")
    return {"invoice": invoice, "purchase_order": po, "receipt": receipt, "amendment": amendment, "policy": policy}


def _gather(case):
    snapshot = get_snapshot(case["invoice_id"])
    if not snapshot or not snapshot["purchase_order"] or not snapshot["receipt"]:
        raise ValueError("Required invoice, purchase order, or receipt evidence is missing")
    for tool_name, label in [
        ("get_invoice", "invoice"), ("get_purchase_order", "purchase order"),
        ("get_goods_receipt", "goods receipt"), ("find_approved_amendment", "approved amendment"),
        ("search_policy", "company policy"),
    ]:
        _tool(case["case_id"], tool_name, f"Read {label} evidence.")
    return snapshot


def _reconcile(case, snapshot):
    amendment = snapshot["amendment"]
    target = amendment["approved_amount"] if amendment and amendment["approved"] else snapshot["purchase_order"]["amount"]
    tolerance = snapshot["policy"]["amount_tolerance"]
    if abs(snapshot["receipt"]["received_amount"] - target) > tolerance:
        with _connect() as db:
            db.execute("UPDATE cases SET status = 'ESCALATED' WHERE case_id = ?", [case["case_id"]])
        _record(case["case_id"], "escalated", "Conflicting records; no correction was attempted.", {
            "authoritative_amount": target, "received_amount": snapshot["receipt"]["received_amount"],
        })
        return None
    _tool(case["case_id"], "reconcile_invoice", "Compared invoice, amendment, PO, and receipt.", {
        "invoice_amount": snapshot["invoice"]["amount"], "authoritative_amount": target,
        "authority": "approved amendment" if amendment and amendment["approved"] else "purchase order",
    })
    return target


def _execute_and_verify(case_id, target):
    for attempt in range(2):
        case = get_case(case_id)
        snapshot = get_snapshot(case["invoice_id"])
        requires_approval = target > snapshot["policy"]["auto_approval_limit"]
        _tool(case_id, "check_resolution_permission", "Rechecked policy immediately before the write.", {
            "target_amount": target, "approval_limit": snapshot["policy"]["auto_approval_limit"],
            "requires_human_approval": requires_approval,
        })
        if requires_approval and not case["approval_granted"]:
            with _connect() as db:
                db.execute("UPDATE cases SET status = 'HUMAN_REQUIRED' WHERE case_id = ?", [case_id])
            _record(case_id, "approval_required", "No write made; exact proposed action is awaiting approval.", {
                "invoice_id": case["invoice_id"], "target_amount": target,
            })
            return get_case(case_id)

        key = f"{case_id}:{case['invoice_id']}:{target:.2f}"
        with _connect() as db:
            prior = _one(db, "SELECT * FROM action_ledger WHERE idempotency_key = ?", [key])
            current = _one(db, "SELECT * FROM invoices WHERE invoice_id = ?", [case["invoice_id"]])
            false_success = case["scenario"] == "recovery" and not case["failure_used"] and prior is None
            if false_success:
                db.execute("UPDATE cases SET failure_used = TRUE, attempts = attempts + 1 WHERE case_id = ?", [case_id])
            elif prior is None:
                if current["revision"] != snapshot["invoice"]["revision"]:
                    _record(case_id, "escalated", "Stale invoice revision; refused to overwrite newer state.", {
                        "expected_revision": snapshot["invoice"]["revision"], "actual_revision": current["revision"],
                    })
                    db.execute("UPDATE cases SET status = 'ESCALATED' WHERE case_id = ?", [case_id])
                    return get_case(case_id)
                db.execute("UPDATE invoices SET amount = ?, status = 'READY', revision = revision + 1 WHERE invoice_id = ? AND revision = ?", [target, case["invoice_id"], current["revision"]])
                db.execute("INSERT INTO action_ledger VALUES (?, ?, ?, ?, 'applied', ?)", [key, case_id, case["invoice_id"], target, _now()])
                db.execute("UPDATE cases SET attempts = attempts + 1 WHERE case_id = ?", [case_id])

        if false_success:
            _tool(case_id, "apply_authorized_invoice_correction", "Action reported success; business state did not change.", {"injected_demo_failure": True})
        else:
            _tool(case_id, "apply_authorized_invoice_correction", "Applied the authorized correction.", {"target_amount": target, "idempotency_key": key})

        observed = get_snapshot(case["invoice_id"])
        passed = (
            abs(observed["invoice"]["amount"] - target) <= observed["policy"]["amount_tolerance"]
            and observed["invoice"]["status"] == "READY"
            and abs(observed["receipt"]["received_amount"] - target) <= observed["policy"]["amount_tolerance"]
        )
        _tool(case_id, "verify_invoice_resolution", "Reread current invoice and receipt; checked outcome invariants.", {
            "expected_amount": target, "observed_amount": observed["invoice"]["amount"],
            "lifecycle_state": observed["invoice"]["status"], "passed": passed,
        })
        if passed:
            with _connect() as db:
                db.execute("UPDATE cases SET status = 'COMPLETED' WHERE case_id = ?", [case_id])
            _record(case_id, "completed", "Invoice resolution independently verified.", {"verified_amount": target})
            return get_case(case_id)
        if attempt == 0:
            _record(case_id, "recovery", "Verification failed; re-investigating before one bounded retry.")
            _gather(get_case(case_id))

    with _connect() as db:
        db.execute("UPDATE cases SET status = 'ESCALATED' WHERE case_id = ?", [case_id])
    _record(case_id, "escalated", "Verification failed after the bounded retry.")
    return get_case(case_id)


def run_operator(case_id):
    case = get_case(case_id)
    if not case:
        raise ValueError("Case not found")
    if case["status"] in {"COMPLETED", "ESCALATED", "HUMAN_REQUIRED"}:
        return case
    snapshot = _gather(case)
    target = _reconcile(case, snapshot)
    if target is None:
        return get_case(case_id)
    tolerance = snapshot["policy"]["amount_tolerance"]
    if abs(snapshot["invoice"]["amount"] - target) <= tolerance:
        with _connect() as db:
            db.execute("UPDATE cases SET status = 'COMPLETED' WHERE case_id = ?", [case_id])
        _record(case_id, "completed", "Invoice already matches authoritative records.")
        return get_case(case_id)
    with _connect() as db:
        db.execute("UPDATE cases SET status = 'RESOLUTION_PROPOSED', proposed_amount = ? WHERE case_id = ?", [target, case_id])
    _record(case_id, "resolution_proposed", "Proposed correcting the invoice to the approved amount.", {
        "invoice_id": case["invoice_id"], "current_amount": snapshot["invoice"]["amount"], "target_amount": target,
    })
    return _execute_and_verify(case_id, target)


def approve_case(case_id):
    case = get_case(case_id)
    if not case or case["status"] != "HUMAN_REQUIRED":
        raise ValueError("This case is not waiting for approval")
    _record(case_id, "human_approved", "Approved the exact proposed invoice correction.", {
        "invoice_id": case["invoice_id"], "target_amount": case["proposed_amount"],
    })
    with _connect() as db:
        db.execute("UPDATE cases SET approval_granted = TRUE, status = 'AUTHORIZED' WHERE case_id = ?", [case_id])
    return _execute_and_verify(case_id, case["proposed_amount"])


def grant_approval(case_id):
    case = get_case(case_id)
    if not case or case["status"] != "HUMAN_REQUIRED":
        raise ValueError("This case is not waiting for approval")
    _record(case_id, "human_approved", "Approved the exact proposed invoice correction.", {
        "invoice_id": case["invoice_id"], "target_amount": case["proposed_amount"],
    })
    with _connect() as db:
        db.execute("UPDATE cases SET approval_granted = TRUE, status = 'AUTHORIZED' WHERE case_id = ?", [case_id])
    return get_case(case_id)


def agent_inspect(case_id):
    case = get_case(case_id)
    if not case:
        raise ValueError("Case not found")
    snapshot = _gather(case)
    return snapshot


def agent_reconcile(case_id):
    case = get_case(case_id)
    snapshot = get_snapshot(case["invoice_id"])
    target = _reconcile(case, snapshot)
    if target is None:
        return {"status": "ESCALATED", "reason": "Conflicting records"}
    if abs(snapshot["invoice"]["amount"] - target) <= snapshot["policy"]["amount_tolerance"]:
        with _connect() as db:
            db.execute("UPDATE cases SET status = 'COMPLETED' WHERE case_id = ?", [case_id])
        _record(case_id, "completed", "Invoice already matches authoritative records.")
        return {"status": "COMPLETED", "reason": "No correction required", "target_amount": target}
    return {
        "invoice_id": case["invoice_id"],
        "current_amount": snapshot["invoice"]["amount"],
        "authoritative_amount": target,
        "mismatch": True,
        "evidence": "approved amendment" if snapshot["amendment"] and snapshot["amendment"]["approved"] else "purchase order",
    }


def agent_authorize(case_id, target_amount):
    case = get_case(case_id)
    snapshot = get_snapshot(case["invoice_id"])
    amendment = snapshot["amendment"]
    authoritative = amendment["approved_amount"] if amendment and amendment["approved"] else snapshot["purchase_order"]["amount"]
    if abs(float(target_amount) - authoritative) > snapshot["policy"]["amount_tolerance"]:
        _record(case_id, "blocked", "Proposed amount did not match authoritative business evidence.", {
            "proposed_amount": target_amount, "authoritative_amount": authoritative,
        })
        return {"authorized": False, "reason": "Target amount is not supported by the approved amendment or PO."}
    requires_approval = authoritative > snapshot["policy"]["auto_approval_limit"] and not case["approval_granted"]
    with _connect() as db:
        db.execute("UPDATE cases SET proposed_amount = ?, status = ? WHERE case_id = ?", [
            authoritative, "HUMAN_REQUIRED" if requires_approval else "AUTHORIZED", case_id,
        ])
    _record(case_id, "resolution_proposed", "Proposed an evidence-backed invoice correction.", {
        "invoice_id": case["invoice_id"], "target_amount": authoritative,
    })
    self_approval_status = "HUMAN_REQUIRED" if requires_approval else "AUTHORIZED"
    _tool(case_id, "check_resolution_permission", "Checked the proposed correction against company policy.", {
        "target_amount": authoritative, "requires_human_approval": requires_approval,
    })
    return {"authorized": not requires_approval, "status": self_approval_status,
            "reason": "Human approval required" if requires_approval else "Policy permits autonomous correction"}


def agent_apply(case_id):
    case = get_case(case_id)
    if not case or case["proposed_amount"] is None:
        return {"applied": False, "reason": "No proposed action exists."}
    snapshot = get_snapshot(case["invoice_id"])
    target = case["proposed_amount"]
    if target > snapshot["policy"]["auto_approval_limit"] and not case["approval_granted"]:
        return {"applied": False, "approval_required": True, "reason": "No human approval recorded."}
    if case["status"] not in {"AUTHORIZED", "RESOLUTION_PROPOSED"}:
        return {"applied": False, "reason": f"Case is not authorized to write ({case['status']})."}
    if case["attempts"] >= 2:
        return {"applied": False, "reason": "Bounded action attempts exhausted; escalate."}

    key = f"{case_id}:{case['invoice_id']}:{target:.2f}"
    with _connect() as db:
        prior = _one(db, "SELECT * FROM action_ledger WHERE idempotency_key = ?", [key])
        current = _one(db, "SELECT * FROM invoices WHERE invoice_id = ?", [case["invoice_id"]])
        if current["revision"] != snapshot["invoice"]["revision"]:
            return {"applied": False, "reason": "Stale invoice revision; re-read before taking action."}
        false_success = case["scenario"] == "recovery" and not case["failure_used"] and prior is None
        if false_success:
            db.execute("UPDATE cases SET failure_used = TRUE, attempts = attempts + 1 WHERE case_id = ?", [case_id])
        elif prior is None:
            db.execute("UPDATE invoices SET amount = ?, status = 'READY', revision = revision + 1 WHERE invoice_id = ? AND revision = ?", [target, case["invoice_id"], current["revision"]])
            db.execute("INSERT INTO action_ledger VALUES (?, ?, ?, ?, 'applied', ?)", [key, case_id, case["invoice_id"], target, _now()])
            db.execute("UPDATE cases SET attempts = attempts + 1 WHERE case_id = ?", [case_id])
    if false_success:
        _tool(case_id, "apply_authorized_invoice_correction", "Action reported success; no business state changed.", {"injected_demo_failure": True})
    else:
        _tool(case_id, "apply_authorized_invoice_correction", "Applied the authorized invoice correction.", {"target_amount": target, "idempotency_key": key})
    return {"applied": True, "reported_success": True, "target_amount": target,
            "note": "A readback is required; tool response alone is not proof of completion."}


def agent_verify(case_id):
    case = get_case(case_id)
    snapshot = get_snapshot(case["invoice_id"])
    target = case["proposed_amount"]
    passed = bool(target is not None and
        abs(snapshot["invoice"]["amount"] - target) <= snapshot["policy"]["amount_tolerance"] and
        snapshot["invoice"]["status"] == "READY" and
        abs(snapshot["receipt"]["received_amount"] - target) <= snapshot["policy"]["amount_tolerance"])
    _tool(case_id, "verify_invoice_resolution", "Reread current invoice and receipt state.", {
        "expected_amount": target, "observed_amount": snapshot["invoice"]["amount"],
        "lifecycle_state": snapshot["invoice"]["status"], "passed": passed,
    })
    if passed:
        with _connect() as db:
            db.execute("UPDATE cases SET status = 'COMPLETED' WHERE case_id = ?", [case_id])
        _record(case_id, "completed", "Invoice resolution independently verified.", {"verified_amount": target})
    elif case["attempts"] >= 2:
        with _connect() as db:
            db.execute("UPDATE cases SET status = 'ESCALATED' WHERE case_id = ?", [case_id])
        _record(case_id, "escalated", "Verification still failed after bounded recovery.")
    else:
        _record(case_id, "recovery", "Verification failed; re-investigate and retry only if evidence still supports it.")
    return {"passed": passed, "status": get_case(case_id)["status"],
            "observed_amount": snapshot["invoice"]["amount"], "target_amount": target}