import os
from collections import defaultdict

from core.models import RCAOutcome, DistrictSummary, MerchantSummary
from core.pii import scrub_pii
from core.rca_engine import run_rca, format_rca_report, InsufficientTelemetryError
from db.repository import repository

DEFAULT_DATE = os.getenv("RCA_DATE", "2026-08-25")


class RCAService:

    def _scrub(self, text: str) -> str:
        """PII choke point: every public entry point returns scrubbed text."""
        return scrub_pii(text)

    def get_problem_hours_summary(self, date: str = DEFAULT_DATE, district: str = "", merchant: str = "") -> str:
        rows = repository.get_problem_hours(date=date, district=district, merchant=merchant)
        if not rows:
            return "No problem hours found for the given filters."

        # Broad query (no district/merchant filter) → district-level summary to avoid token bloat
        if not district and not merchant:
            by_district: dict = defaultdict(lambda: {"total": 0, "merchants": set(), "lockup": 0})
            for r in rows:
                by_district[r.district]["total"] += 1
                by_district[r.district]["merchants"].add(r.merchant_id)
                if r.reversal_pending_flag:
                    by_district[r.district]["lockup"] += 1

            total_merchants = sum(len(m["merchants"]) for m in by_district.values())
            lines = [
                f"Found **{total_merchants} merchants** with high-failure hours across **{len(by_district)} districts** on {date}:\n"
            ]
            for district_name, stats in sorted(by_district.items()):
                lines.append(
                    f"- **{district_name}**: {len(stats['merchants'])} merchants affected, "
                    f"{stats['total']} problem hours, "
                    f"{stats['lockup']} with pending-lockup flag"
                )
            lines.append("\nUse get_problem_hours with a specific district to see merchant-level detail.")
            return self._scrub("\n".join(lines))

        # Filtered query → individual rows (capped at 50)
        total = len(rows)
        shown = rows[:50]
        lines = [f"Found **{total}** problem hour(s) (showing first {len(shown)}):\n"]
        for r in shown:
            lines.append(
                f"- **{r.merchant_id}** ({r.district}) | Hour {r.hour}:00 "
                f"| Avg response: {round(r.avg_response_time, 1)}s "
                f"| Failure Rate: {round(r.failure_rate * 100, 1)}%"
                f"| Pending: {'YES' if r.reversal_pending_flag else 'NO'}"
            )
        return self._scrub("\n".join(lines))

    def run_merchant_rca(self, merchant: str, date: str = DEFAULT_DATE, hour: int = -1) -> str:
        merchant_rows = repository.get_merchant_rows(merchant=merchant, date=date)
        if not merchant_rows:
            return f"No data found for merchant '{merchant}' on {date}."

        problem_rows = [r for r in merchant_rows if r.is_high_failure_hour]
        if hour >= 0:
            problem_rows = [r for r in problem_rows if r.hour == hour]

        if not problem_rows:
            return f"No problem hours found for merchant '{merchant}'."

        # Cap at 5 worst hours (highest failure rate) to keep response concise
        problem_rows = sorted(problem_rows, key=lambda r: r.failure_rate, reverse=True)[:5]
        results: list[RCAOutcome] = [run_rca(row, merchant_rows) for row in problem_rows]
        return self._scrub(format_rca_report(results))

    def get_district_summary(self, district: str, date: str = DEFAULT_DATE) -> str:
        summary: DistrictSummary | None = repository.get_district_summary(district=district, date=date)
        if not summary:
            return f"No data found for district '{district}' on {date}."

        return self._scrub(
            f"**{summary.district}** — {date}\n"
            f"- Total Attempts: {summary.total_attempts}\n"
            f"- Weighted Failure Rate: {summary.weighted_failure_rate_pct}%\n"
            f"- Weighted Avg Response: {summary.weighted_avg_response_time}s\n"
            f"- Problem Hours: {summary.problem_hours} of {summary.total_merchant_hours} merchant-hours\n"
        )

    def list_districts(self, date: str = DEFAULT_DATE) -> str:
        districts = repository.list_districts(date=date)
        if not districts:
            return "No districts found."
        return self._scrub("Available districts: " + ", ".join(districts))

    def list_merchants(self, district: str, date: str = DEFAULT_DATE) -> str:
        merchants: list[MerchantSummary] = repository.list_merchants(district=district, date=date)
        if not merchants:
            return f"No merchants found in '{district}'."

        lines = [f"Merchants in **{district}**:\n"]
        for m in merchants:
            flag = "PROBLEM" if m.problem_hours > 0 else "OK"
            lines.append(f"  [{flag}] {m.merchant_id} — {m.problem_hours} problem hour(s), {m.total_attempts} attempts")
        return self._scrub("\n".join(lines))


rca_service = RCAService()
