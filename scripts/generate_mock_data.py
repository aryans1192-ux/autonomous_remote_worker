"""Deterministic mock-data generator for the Eko AePS RCA worker.

Creates data/eko_aeps_transactions_2026.csv: ~32 merchants x 24 hours across 8 districts
on 2026-08-25, with planted failure profiles:

- ~8 gateway-timeout merchants   (slow avg_response_time + high failure_rate)
- ~6 lockup merchants            (2-4 consecutive reversal_pending hours)
- ~6 low-biometric-match merchants (successful_auths / total_scans < 0.70)
- a few combined-cause merchants
- clean merchants
- MERCHANT_ERROR_404             (blank successful_auths / total_scans for ALL hours,
                                  high failure, ZERO pending hours so the lockup check
                                  can't mask the intended DATA_CONSTRAINED demo beat)
- one PII-demo device_id         (contains a mock 12-digit Aadhaar-like run)

is_high_failure_hour is computed so every check-triggering hour is flagged.
Run: python scripts/generate_mock_data.py  (deterministic; seed 42)
"""
import csv
import os
import random
from pathlib import Path

random.seed(42)

DATE = "2026-08-25"
DISTRICTS = ["BHIWANI", "REWARI", "JHUNJHUNU", "SITAPUR", "BAREILLY", "NUH", "DARBHANGA", "BHAGALPUR"]
N_HOURS = 24

# Gateway-timeout merchants: slow NPCI responses + high failure
GATEWAY_MERCHANTS = [f"MERCHANT_GATEWAY_{i:03d}" for i in range(1, 9)]
# Lockup merchants: funds stuck PENDING across consecutive hours
LOCKUP_MERCHANTS = [f"MERCHANT_LOCKUP_{i:03d}" for i in range(1, 7)]
# Biometric merchants: low successful_auths / total_scans
BIOMETRIC_MERCHANTS = [f"MERCHANT_BIO_{i:03d}" for i in range(1, 7)]
# Combined-cause merchants
COMBINED_MERCHANTS = [f"MERCHANT_COMBINED_{i:03d}" for i in range(1, 4)]
# Clean merchants
CLEAN_MERCHANTS = [f"MERCHANT_CLEAN_{i:03d}" for i in range(1, 8)]
# The intentional-failure demo merchant
ERROR_MERCHANT = "MERCHANT_ERROR_404"

BANK_CODES = ["SBI", "PNB", "BOB", "HDFC", "ICICI", "AXIS", "CANARA", "UCO"]


def profile_for(merchant: str) -> str:
    if merchant in GATEWAY_MERCHANTS:
        return "gateway"
    if merchant in LOCKUP_MERCHANTS:
        return "lockup"
    if merchant in BIOMETRIC_MERCHANTS:
        return "biometric"
    if merchant in COMBINED_MERCHANTS:
        return "combined"
    if merchant == ERROR_MERCHANT:
        return "error404"
    return "clean"


def assign_districts(merchants: list[str], districts: list[str], seed_offset: int = 0) -> dict[str, str]:
    rng = random.Random(seed_offset)
    out = {}
    for i, m in enumerate(merchants):
        out[m] = districts[(i + seed_offset) % len(districts)]
    return out


def build_rows() -> list[dict]:
    rows: list[dict] = []

    # Deterministic district assignment across all merchant lists
    all_merchants = (
        GATEWAY_MERCHANTS + LOCKUP_MERCHANTS + BIOMETRIC_MERCHANTS
        + COMBINED_MERCHANTS + CLEAN_MERCHANTS + [ERROR_MERCHANT]
    )
    districts = assign_districts(all_merchants, DISTRICTS, seed_offset=7)

    for merchant in all_merchants:
        profile = profile_for(merchant)
        district = districts[merchant]

        # Per-merchant random state so hour-to-hour noise is deterministic per merchant
        rng = random.Random(hash(merchant) % (2**32))

        # Lockup merchants: pick a contiguous block of pending hours
        lockup_block: list[int] = []
        if profile in ("lockup", "combined"):
            start = rng.choice([8, 10, 12, 14, 16])
            length = rng.choice([2, 3, 4])
            lockup_block = list(range(start, min(start + length, N_HOURS)))

        for hour in range(N_HOURS):
            row = {
                "charge_date": DATE,
                "merchant_id": merchant,
                "district": district,
                "hour": hour,
                "total_attempts": rng.randint(15, 60),
                "failed_count": 0,
                "failure_rate": 0.0,
                "is_high_failure_hour": 0,
                "pending_count": 0,
                "reversal_pending_flag": 0,
                "avg_response_time": round(rng.uniform(2.0, 6.0), 2),
                "pending_amount": 0,
                "bank_code": rng.choice(BANK_CODES),
                "device_id": f"TERM-{merchant[-3:]}-{rng.randint(1000, 9999)}",
                "total_scans": rng.randint(10, 50),
                "successful_auths": rng.randint(8, 48),
            }

            # ---- profile-specific shaping ----
            if profile == "gateway":
                # Slow hours: high latency + high failure
                if hour in (10, 11, 12, 13, 18, 19):
                    row["avg_response_time"] = round(rng.uniform(16.0, 30.0), 2)
                    row["failed_count"] = int(row["total_attempts"] * rng.uniform(0.35, 0.6))
                else:
                    row["failed_count"] = int(row["total_attempts"] * rng.uniform(0.02, 0.08))

            elif profile == "lockup":
                # Pending block: flag + high failure during block
                if hour in lockup_block:
                    row["reversal_pending_flag"] = 1
                    row["pending_count"] = rng.randint(3, 12)
                    row["pending_amount"] = round(rng.uniform(5000, 60000), 2)
                    row["failed_count"] = int(row["total_attempts"] * rng.uniform(0.3, 0.55))
                else:
                    row["failed_count"] = int(row["total_attempts"] * rng.uniform(0.02, 0.08))

            elif profile == "biometric":
                # Bad match hours: low successful_auths / total_scans
                if hour in (9, 10, 11, 15, 16, 17):
                    row["total_scans"] = rng.randint(20, 40)
                    row["successful_auths"] = int(row["total_scans"] * rng.uniform(0.1, 0.55))
                    row["failed_count"] = int(row["total_attempts"] * rng.uniform(0.25, 0.45))
                else:
                    row["total_scans"] = rng.randint(10, 40)
                    row["successful_auths"] = int(row["total_scans"] * rng.uniform(0.8, 0.98))
                    row["failed_count"] = int(row["total_attempts"] * rng.uniform(0.02, 0.08))

            elif profile == "combined":
                # Gateway + lockup overlap (the realistic combo)
                if hour in (11, 12, 13, 14):
                    row["avg_response_time"] = round(rng.uniform(16.0, 26.0), 2)
                    row["failed_count"] = int(row["total_attempts"] * rng.uniform(0.35, 0.55))
                if hour in lockup_block:
                    row["reversal_pending_flag"] = 1
                    row["pending_count"] = rng.randint(2, 8)
                    row["pending_amount"] = round(rng.uniform(3000, 40000), 2)
                    row["failed_count"] = max(row["failed_count"], int(row["total_attempts"] * rng.uniform(0.3, 0.5)))
                if row["reversal_pending_flag"] == 0 and row["avg_response_time"] < 15.0:
                    row["failed_count"] = int(row["total_attempts"] * rng.uniform(0.02, 0.08))

            elif profile == "error404":
                # NO biometric telemetry at all; high failure; ZERO pending hours
                row["total_scans"] = ""
                row["successful_auths"] = ""
                if hour in (10, 11, 12, 13, 17, 18):
                    row["failed_count"] = int(row["total_attempts"] * rng.uniform(0.4, 0.65))
                else:
                    row["failed_count"] = int(row["total_attempts"] * rng.uniform(0.02, 0.08))

            else:  # clean
                row["failed_count"] = int(row["total_attempts"] * rng.uniform(0.01, 0.06))

            # PII demo: one device_id carries a mock 12-digit Aadhaar-like run
            if merchant == "MERCHANT_LOCKUP_001":
                row["device_id"] = "TERM-404-123456789012"

            row["failure_rate"] = round(row["failed_count"] / row["total_attempts"], 4)
            row["is_high_failure_hour"] = 1 if row["failure_rate"] > 0.30 else 0
            rows.append(row)

    return rows


def main():
    out_dir = Path(__file__).resolve().parents[1] / "data"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "eko_aeps_transactions_2026.csv"

    rows = build_rows()
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows -> {out_path}")

    # Sanity checks
    n_high = sum(1 for r in rows if r["is_high_failure_hour"])
    n_merchants = len({r["merchant_id"] for r in rows})
    n_districts = len({r["district"] for r in rows})
    print(f"Merchants: {n_merchants}, Districts: {n_districts}, High-failure hours: {n_high}")
    err_rows = [r for r in rows if r["merchant_id"] == ERROR_MERCHANT]
    print(f"{ERROR_MERCHANT}: hours={len(err_rows)}, high={sum(1 for r in err_rows if r['is_high_failure_hour'])}, "
          f"pending={sum(1 for r in err_rows if r['reversal_pending_flag'])}")


if __name__ == "__main__":
    main()
