import sys
import os
from typing import Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from mcp.server.fastmcp import FastMCP
from services.rca_service import rca_service
from core.rca_engine import InsufficientTelemetryError
from core.prompts import DATA_CONSTRAINED_FALLBACK_MESSAGE

mcp = FastMCP("aeps-rca", port=8001)

DEFAULT_DATE = "2026-08-25"


@mcp.tool()
def get_problem_hours(district: str = "", merchant: str = "", date: str = DEFAULT_DATE) -> str:
    """
    List all high-failure hours for a given district, merchant, or date.
    Leave district or merchant blank to get a district-level summary across all locations.
    """
    return rca_service.get_problem_hours_summary(date=date, district=district, merchant=merchant)


@mcp.tool()
def run_rca_for_merchant(merchant: str, date: str = DEFAULT_DATE) -> str:
    """
    Run full RCA (gateway timeout, pending lockup, biometric device) for a specific merchant — returns all problem hours.
    To discuss a specific time range like morning or evening, call this tool then filter in your response.
    """
    try:
        return rca_service.run_merchant_rca(merchant=merchant, date=date, hour=-1)
    except InsufficientTelemetryError:
        # Serialized as a NORMAL tool result (not an MCP error) so the agent's
        # DATA_CONSTRAINED handling works identically to the direct-tool path.
        return DATA_CONSTRAINED_FALLBACK_MESSAGE


@mcp.tool()
def get_district_summary(district: str, date: str = DEFAULT_DATE) -> str:
    """
    Weighted day-level summary for a district: total attempts, failure rate, avg response time, problem hours.
    Failure rate and response time are weighted by attempt volume as per the RCA playbook.
    """
    return rca_service.get_district_summary(district=district, date=date)


@mcp.tool()
def list_districts(date: str = DEFAULT_DATE) -> str:
    """List all districts available in the dataset for a given date."""
    return rca_service.list_districts(date=date)


@mcp.tool()
def list_merchants_in_district(district: str, date: str = DEFAULT_DATE) -> str:
    """List all merchants in a district with high-failure hour counts."""
    return rca_service.list_merchants(district=district, date=date)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", default="stdio", choices=["stdio", "streamable-http"])
    args = parser.parse_args()
    mcp.run(transport=args.transport)
