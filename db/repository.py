from core.models import TransactionRow, DistrictSummary, MerchantSummary
from db.database import get_connection


class TransactionRepository:

    def get_merchant_rows(self, merchant: str, date: str) -> list[TransactionRow]:
        with get_connection() as con:
            rows = con.execute(
                "SELECT * FROM aeps_transactions WHERE LOWER(merchant_id) LIKE LOWER(?) AND charge_date = ? ORDER BY hour",
                [f"%{merchant}%", date]
            ).fetchdf().to_dict(orient="records")
        return [TransactionRow(**r) for r in rows]

    def get_problem_hours(self, date: str, district: str = "", merchant: str = "") -> list[TransactionRow]:
        conditions = ["is_high_failure_hour = 1", "charge_date = ?"]
        params: list = [date]
        if district:
            conditions.append("LOWER(district) LIKE LOWER(?)")
            params.append(f"%{district}%")
        if merchant:
            conditions.append("LOWER(merchant_id) LIKE LOWER(?)")
            params.append(f"%{merchant}%")

        query = f"SELECT * FROM aeps_transactions WHERE {' AND '.join(conditions)} ORDER BY merchant_id, hour"
        with get_connection() as con:
            rows = con.execute(query, params).fetchdf().to_dict(orient="records")
        return [TransactionRow(**r) for r in rows]

    def get_district_summary(self, district: str, date: str) -> DistrictSummary | None:
        with get_connection() as con:
            rows = con.execute("""
                SELECT
                    district,
                    SUM(total_attempts)                                                            AS total_attempts,
                    SUM(failed_count)                                                              AS total_failed,
                    ROUND(SUM(failed_count) * 100.0 / NULLIF(SUM(total_attempts), 0), 1)          AS failure_rate,
                    ROUND(SUM(avg_response_time * total_attempts) / NULLIF(SUM(total_attempts), 0), 1) AS avg_response_time,
                    SUM(CASE WHEN is_high_failure_hour = 1 THEN 1 ELSE 0 END)                     AS problem_hours,
                    COUNT(*)                                                                       AS total_merchant_hours
                FROM aeps_transactions
                WHERE LOWER(district) LIKE LOWER(?) AND charge_date = ?
                GROUP BY district
            """, [f"%{district}%", date]).fetchdf().to_dict(orient="records")

        if not rows:
            return None
        r = rows[0]
        return DistrictSummary(
            district=r["district"],
            date=date,
            total_attempts=int(r["total_attempts"] or 0),
            weighted_failure_rate_pct=float(r["failure_rate"] or 0),
            weighted_avg_response_time=float(r["avg_response_time"] or 0),
            problem_hours=int(r["problem_hours"]),
            total_merchant_hours=int(r["total_merchant_hours"]),
        )

    def list_districts(self, date: str) -> list[str]:
        with get_connection() as con:
            rows = con.execute(
                "SELECT DISTINCT district FROM aeps_transactions WHERE charge_date = ? ORDER BY district", [date]
            ).fetchall()
        return [r[0] for r in rows]

    def list_merchants(self, district: str, date: str) -> list[MerchantSummary]:
        with get_connection() as con:
            rows = con.execute("""
                SELECT merchant_id, district,
                       SUM(CASE WHEN is_high_failure_hour = 1 THEN 1 ELSE 0 END) AS problem_hours,
                       SUM(total_attempts) AS total_attempts
                FROM aeps_transactions
                WHERE LOWER(district) LIKE LOWER(?) AND charge_date = ?
                GROUP BY merchant_id, district
                ORDER BY problem_hours DESC
            """, [f"%{district}%", date]).fetchdf().to_dict(orient="records")
        return [
            MerchantSummary(
                merchant_id=r["merchant_id"],
                district=r["district"],
                problem_hours=int(r["problem_hours"]),
                total_attempts=int(r["total_attempts"]),
            )
            for r in rows
        ]


repository = TransactionRepository()
