import os

import duckdb
import pytest

from core.models import TransactionRow
from db import database
from db.repository import TransactionRepository


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """Build a temp DuckDB from a small fixture CSV, patching database paths."""
    csv_path = tmp_path / "fixture.csv"
    db_path = tmp_path / "fixture.db"

    csv_path.write_text(
        "charge_date,merchant_id,district,hour,total_attempts,failed_count,failure_rate,"
        "is_high_failure_hour,pending_count,reversal_pending_flag,avg_response_time,"
        "pending_amount,bank_code,device_id,total_scans,successful_auths\n"
        "2026-08-25,M1,BHIWANI,9,100,50,0.5,1,0,0,4.0,0,SBI,TERM-1,30,25\n"
        "2026-08-25,M1,BHIWANI,10,100,10,0.1,0,2,1,4.0,500,PNB,TERM-1,30,28\n"
        "2026-08-25,M2,BHIWANI,11,80,40,0.5,1,0,0,3.0,0,HDFC,TERM-2,20,5\n"
        "2026-08-25,M3,REWARI,12,50,5,0.1,0,0,0,2.5,0,BOB,TERM-3,15,14\n",
        encoding="utf-8",
    )

    con = duckdb.connect(str(db_path))
    con.execute(f"CREATE TABLE aeps_transactions AS SELECT * FROM read_csv_auto('{csv_path}', header=True)")
    con.close()

    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    monkeypatch.setattr(database, "CSV_PATH", str(csv_path))
    return TransactionRepository()


def test_get_merchant_rows(repo):
    rows = repo.get_merchant_rows("M1", "2026-08-25")
    assert len(rows) == 2
    assert all(isinstance(r, TransactionRow) for r in rows)
    assert [r.hour for r in rows] == [9, 10]


def test_get_problem_hours_district_filter(repo):
    rows = repo.get_problem_hours("2026-08-25", district="BHIWANI")
    assert len(rows) == 2
    assert all(r.district == "BHIWANI" for r in rows)


def test_get_problem_hours_merchant_filter(repo):
    rows = repo.get_problem_hours("2026-08-25", merchant="M2")
    assert len(rows) == 1
    assert rows[0].merchant_id == "M2"


def test_get_district_summary_weighted(repo):
    s = repo.get_district_summary("BHIWANI", "2026-08-25")
    assert s.district == "BHIWANI"
    # attempts: 100+100+80 = 280; failed: 50+10+40 = 100 → 35.7%
    assert s.total_attempts == 280
    assert s.weighted_failure_rate_pct == 35.7
    # weighted avg response: (4*100 + 4*100 + 3*80)/280 = 3.71
    assert s.weighted_avg_response_time == 3.7
    assert s.problem_hours == 2
    assert s.total_merchant_hours == 3


def test_get_district_summary_none(repo):
    assert repo.get_district_summary("NOPE", "2026-08-25") is None


def test_list_districts(repo):
    assert repo.list_districts("2026-08-25") == ["BHIWANI", "REWARI"]


def test_list_merchants_ranked(repo):
    merchants = repo.list_merchants("BHIWANI", "2026-08-25")
    # M1 and M2 both have 1 problem hour; M1 has more attempts
    assert [m.merchant_id for m in merchants] == ["M1", "M2"]
    assert merchants[0].problem_hours == 1
