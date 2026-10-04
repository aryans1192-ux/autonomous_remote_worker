import json
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.invoice_operator import (
    SCENARIOS,
    approve_case,
    create_case,
    get_case,
    get_events,
    get_snapshot,
    grant_approval,
    initialize_database,
    list_cases,
    run_operator,
)
from agent.invoice_graph import active_model_name, model_is_configured, run_invoice_agent


st.set_page_config(page_title="Business Resolution Operator", page_icon="BR", layout="wide")
initialize_database()

st.markdown("""
<style>
    .block-container { max-width: 1320px; padding-top: 2rem; }
    [data-testid="stMetric"] { border-left: 3px solid #16806a; padding-left: 12px; }
    .eyebrow { color: #16806a; font-size: 0.75rem; font-weight: 700; text-transform: uppercase; }
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="eyebrow">Accounts payable / local sandbox</div>', unsafe_allow_html=True)
st.title("Business Resolution Operator")
runtime_label = active_model_name() if model_is_configured() else "offline scenario runtime · configure GROQ_API_KEY for model planning"
st.caption(f"Invoice exception desk · synthetic records · {runtime_label}")

requests = {
    "autonomous": "Please resolve the issue with invoice INV-1042.",
    "recovery": "Please resolve the issue with invoice INV-2042.",
    "approval": "Please resolve the issue with invoice INV-3042.",
}

with st.sidebar:
    st.subheader("Start a case")
    scenario = st.selectbox("Demo scenario", options=list(SCENARIOS), format_func=lambda key: SCENARIOS[key]["label"])
    request = st.text_area("Business request", value=requests[scenario], height=90, key=f"request_{scenario}")
    if st.button("Run operator", type="primary", use_container_width=True):
        if not request.strip():
            st.error("Enter a business request to start the case.")
        else:
            try:
                with st.spinner("Gathering evidence and resolving the case..."):
                    case_id = create_case(scenario, request)
                    if model_is_configured():
                        run_invoice_agent(case_id)
                    else:
                        run_operator(case_id)
                st.session_state.active_case_id = case_id
                st.session_state["recent_case_picker"] = case_id
                st.rerun()
            except Exception as error:
                st.error(f"Case could not be started: {error}")

    st.divider()
    st.subheader("Recent cases")
    recent_cases = list_cases()
    if recent_cases:
        selected_case = st.selectbox(
            "Open case",
            options=[case["case_id"] for case in recent_cases],
            format_func=lambda case_id: next(
                f"{case['invoice_id']} · {case['status']}" for case in recent_cases if case["case_id"] == case_id
            ),
            key="recent_case_picker",
        )
        st.session_state.active_case_id = selected_case
    else:
        st.caption("No cases yet.")

case_id = st.session_state.get("active_case_id")
case = get_case(case_id) if case_id else None
if not case:
    st.info("Choose a scenario and run the operator to open a case.")
    st.stop()

snapshot = get_snapshot(case["invoice_id"])
status_labels = {
    "COMPLETED": "Completed",
    "HUMAN_REQUIRED": "Approval required",
    "ESCALATED": "Escalated",
    "RECEIVED": "Received",
    "RESOLUTION_PROPOSED": "Resolution proposed",
    "AUTHORIZED": "Authorized",
}

first, second, third = st.columns(3)
first.metric("Case status", status_labels.get(case["status"], case["status"]))
second.metric("Invoice", case["invoice_id"])
third.metric("Write attempts", case["attempts"])

if case["status"] == "COMPLETED":
    st.success("Outcome verified. The invoice now matches the approved business records.")
elif case["status"] == "HUMAN_REQUIRED":
    st.warning("This correction exceeds the autonomous limit. No business state has changed yet.")
elif case["status"] == "ESCALATED":
    st.error("The operator stopped safely and escalated the case with its evidence.")

left, right = st.columns([1.25, 0.75], gap="large")
with left:
    st.subheader("Business state")
    amendment = snapshot["amendment"]
    records = [
        {"Source": "Invoice", "Reference": snapshot["invoice"]["invoice_id"], "Amount": f"INR {snapshot['invoice']['amount']:,.2f}", "State": snapshot["invoice"]["status"]},
        {"Source": "Purchase order", "Reference": snapshot["purchase_order"]["po_id"], "Amount": f"INR {snapshot['purchase_order']['amount']:,.2f}", "State": "Open"},
        {"Source": "Goods receipt", "Reference": snapshot["receipt"]["receipt_id"], "Amount": f"INR {snapshot['receipt']['received_amount']:,.2f}", "State": "Received"},
        {"Source": "Approved amendment", "Reference": amendment["amendment_id"], "Amount": f"INR {amendment['approved_amount']:,.2f}", "State": "Approved"},
    ]
    st.dataframe(records, hide_index=True, use_container_width=True)
    st.caption(
        f"Policy {snapshot['policy']['policy_id']} · autonomous correction limit "
        f"INR {snapshot['policy']['auto_approval_limit']:,.0f} · tolerance INR {snapshot['policy']['amount_tolerance']:.2f}"
    )

with right:
    st.subheader("Proposed action")
    if case["proposed_amount"] is None:
        st.write("No correction proposed.")
    else:
        st.write(f"Set {case['invoice_id']} amount to **INR {case['proposed_amount']:,.2f}**")
        st.caption("Authority: approved amendment, cross-checked against the PO and goods receipt.")
    if case["status"] == "HUMAN_REQUIRED":
        if st.button("Approve exact action and continue", type="primary", use_container_width=True):
            try:
                if model_is_configured():
                    grant_approval(case_id)
                    run_invoice_agent(case_id, approved=True)
                else:
                    approve_case(case_id)
                st.rerun()
            except Exception as error:
                st.error(f"Approval could not be applied: {error}")
    elif case["status"] == "COMPLETED":
        st.write(f"Verified state: **{snapshot['invoice']['status']}**")
        st.write(f"Current amount: **INR {snapshot['invoice']['amount']:,.2f}**")

st.divider()
st.subheader("Execution timeline")
events = get_events(case_id)
for event in events:
    marker = "-"
    if event["event_type"] in {"completed", "human_approved"}:
        marker = "+"
    elif event["event_type"] in {"recovery", "escalated", "approval_required"}:
        marker = "!"
    details = event["details"]
    label = details.get("tool", event["event_type"].replace("_", " ").title())
    with st.expander(f"{marker}  {label} · {event['summary']}", expanded=event["event_type"] in {"recovery", "approval_required"}):
        st.caption(event["created_at"])
        st.code(json.dumps(details, indent=2), language="json")