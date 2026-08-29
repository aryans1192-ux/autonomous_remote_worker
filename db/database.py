import os
import duckdb

DB_PATH = os.path.join(os.path.dirname(__file__), "aeps.db")
CSV_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "data", "eko_aeps_transactions_2026.csv")
)


def get_connection() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(DB_PATH, read_only=True)


def setup():
    if not os.path.exists(CSV_PATH):
        raise FileNotFoundError(f"CSV not found: {CSV_PATH}")

    con = duckdb.connect(DB_PATH)
    con.execute("DROP TABLE IF EXISTS aeps_transactions")
    con.execute(
        f"CREATE TABLE aeps_transactions AS SELECT * FROM read_csv_auto('{CSV_PATH}', header=True)"
    )
    count = con.execute("SELECT COUNT(*) FROM aeps_transactions").fetchone()[0]
    con.close()
    print(f"[DB] Loaded {count} rows from CSV into aeps_transactions table")
