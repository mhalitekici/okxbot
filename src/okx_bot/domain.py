from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"


class Signal(BaseModel):
    symbol: str
    direction: Direction
    entry: Decimal = Field(gt=0)
    range_percent: Decimal | None = Field(default=None, gt=0, lt=100)
    take_profit: Decimal = Field(gt=0)
    stop_loss: Decimal = Field(gt=0)
    timestamp: datetime
    notes: str
    parser_name: str = "regex_v1"
    interpretation_rule: str | None = None
    level_coefficient: Decimal | None = None
    take_profit_source: Literal["range", "explicit"] = "range"
    stop_loss_source: Literal["range", "explicit"] = "range"
    signal_kind: str = "explicit_signal"
    automation_policy: str = "explicit_direction_v1"

    @property
    def inst_id(self) -> str:
        return f"{self.symbol[:-4]}-USDT-SWAP"


@dataclass(frozen=True)
class Message:
    channel_id: int
    message_id: int
    text: str
    date: datetime
    edited: bool = False
    topic_id: int | None = None


class ReviewRequired(ValueError):
    def __init__(self, message: str, *, code: str = "REVIEW_REQUIRED"):
        self.code = code
        super().__init__(message)


class RiskRejected(ValueError):
    def __init__(self, message: str, *, code: str = "RISK_REJECTED", context: dict | None = None):
        self.code, self.context = code, context or {}
        super().__init__(message)


class DailyLossLimitReached(RiskRejected):
    pass


class DemoInstrumentMissing(RiskRejected):
    def __init__(self):
        super().__init__(
            "Instrument absent from eligible demo list", code="DEMO_INSTRUMENT_MISSING"
        )


class ExchangeError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        ambiguous: bool = False,
        endpoint: str | None = None,
        method: str | None = None,
        http_status: int | None = None,
    ):
        # Never include raw responses, request headers, or secret-bearing URLs.
        self.code = code
        self.ambiguous = ambiguous
        self.endpoint, self.method, self.http_status = endpoint, method, http_status
        super().__init__(f"OKX error {code}")
