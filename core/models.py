import math
from typing import Optional
from pydantic import BaseModel, ConfigDict, field_validator, model_validator


def _is_missing(v) -> bool:
    """True for None or NaN (DuckDB fetchdf yields NaN for empty cells, not None)."""
    return v is None or (isinstance(v, float) and math.isnan(v))


class TransactionRow(BaseModel):
    charge_date: str

    @field_validator("charge_date", mode="before")
    @classmethod
    def coerce_date(cls, v):
        return v.isoformat() if hasattr(v, "isoformat") else str(v)

    @model_validator(mode="before")
    @classmethod
    def replace_missing_with_zero(cls, values):
        if not isinstance(values, dict):
            return values

        # Before coercion, flag missing biometric telemetry (successful_auths / total_scans)
        # so the engine can raise InsufficientTelemetryError instead of dividing by zero.
        sa, ts = values.get("successful_auths"), values.get("total_scans")
        if _is_missing(sa) or _is_missing(ts):
            values["biometric_telemetry_missing"] = True

        return {k: (0 if _is_missing(v) else v) for k, v in values.items()}

    merchant_id: str
    district: str
    hour: int
    total_attempts: float = 0
    failed_count: float = 0
    failure_rate: float = 0
    is_high_failure_hour: int = 0
    pending_count: float = 0
    reversal_pending_flag: int = 0
    avg_response_time: float = 0
    pending_amount: float = 0
    bank_code: str = ""
    device_id: str = ""
    total_scans: float = 0
    successful_auths: float = 0
    biometric_telemetry_missing: bool = False

    model_config = ConfigDict(extra="allow")


class GatewayResult(BaseModel):
    triggered: bool
    reason: Optional[str] = None


class LockupResult(BaseModel):
    triggered: bool
    sustained: bool = False
    reason: Optional[str] = None


class BiometricResult(BaseModel):
    triggered: bool
    level: Optional[str] = None
    reason: Optional[str] = None


class RCAOutcome(BaseModel):
    merchant_id: str
    district: str
    hour: int
    avg_response_time: float
    failure_rate_pct: float
    total_attempts: int
    pending_count: int
    pending_amount: float
    total_scans: int
    successful_auths: int
    bank_code: str
    device_id: str
    root_causes: list[str]
    gateway: GatewayResult
    lockup: LockupResult
    biometric: BiometricResult
    owner: str
    next_action: str
    escalation: str


class MerchantSummary(BaseModel):
    merchant_id: str
    district: str
    problem_hours: int
    total_attempts: int


class DistrictSummary(BaseModel):
    district: str
    date: str
    total_attempts: int
    weighted_failure_rate_pct: float
    weighted_avg_response_time: float
    problem_hours: int
    total_merchant_hours: int
