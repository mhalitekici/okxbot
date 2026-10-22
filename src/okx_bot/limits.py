"""Validated PostgreSQL settings; no runtime command can disable a hard safety gate."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allocation_slots: int = Field(default=3, ge=1, le=10)
    auto_rebalance: bool = True
    max_open_positions: int = Field(default=3, ge=1, le=10)
    max_portfolio_risk_percent: Decimal = Field(default=Decimal("40"), gt=0, le=75)
    max_daily_loss_percent: Decimal = Field(default=Decimal("3"), gt=0, le=25)
    minimum_free_margin: Decimal = Field(default=Decimal("10"), gt=0)
    max_bid_ask_spread_percent: Decimal = Field(default=Decimal("0.5"), gt=0, le=5)
    max_entry_price_deviation_percent: Decimal = Field(default=Decimal("0.5"), gt=0, le=5)
    max_order_slippage_percent: Decimal = Field(default=Decimal("0.5"), gt=0, le=5)
    unusual_stop_distance_percent: Decimal = Field(default=Decimal("5"), gt=0, le=25)
    liquidation_buffer_percent: Decimal = Field(default=Decimal("0.25"), gt=0, le=5)

    @classmethod
    def from_settings(cls, settings):
        return cls(**{name: getattr(settings, name) for name in cls.model_fields})
