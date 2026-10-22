from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="forbid",
        case_sensitive=False,
        validate_assignment=True,
        hide_input_in_errors=True,
    )

    database_url: str = "postgresql+asyncpg://okx:change-me@localhost:5432/okx"
    postgres_password: SecretStr = SecretStr("")
    # These values are deliberately literals: this release cannot trade live.
    okx_demo: Literal[True] = True
    live_trading_enabled: Literal[False] = False
    okx_base_url: Literal["https://www.okx.com", "https://eea.okx.com", "https://us.okx.com"] = (
        "https://www.okx.com"
    )
    okx_api_key: SecretStr = SecretStr("")
    okx_api_secret: SecretStr = SecretStr("")
    okx_api_passphrase: SecretStr = SecretStr("")
    # Optional explicitly named demo credentials override the legacy demo-only names.
    okx_demo_api_key: SecretStr = SecretStr("")
    okx_demo_api_secret: SecretStr = SecretStr("")
    okx_demo_api_passphrase: SecretStr = SecretStr("")
    dedicated_trading_account_confirmed: bool = False
    telegram_api_id: int = Field(default=0, ge=0)
    telegram_api_hash: SecretStr = SecretStr("")
    telegram_channel_id: int = 0
    # 0 is fail-closed: no new signals until a forum topic has been verified/configured.
    telegram_topic_id: int = Field(default=0, ge=0, le=2**31 - 1)
    # Explicit numeric identity for the MZTRADINGBOT interpretation rule; 0 disables it.
    mztradingbot_channel_id: int = 0
    telegram_session_string: SecretStr = SecretStr("")
    telegram_session_file: Path = Path("data/telegram.session-string")
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_admin_id: int = Field(default=0, ge=0)
    admin_api_token: SecretStr = SecretStr("")
    max_account_risk_percent: Decimal = Field(default=Decimal("1"), gt=0, le=10)
    max_open_positions: int = Field(default=3, ge=1, le=50)
    max_signal_age_seconds: int = Field(default=60, ge=1, le=3600)
    max_entry_price_deviation_percent: Decimal = Field(default=Decimal("0.5"), gt=0, le=5)
    max_bid_ask_spread_percent: Decimal = Field(default=Decimal("0.5"), gt=0, le=5)
    max_daily_loss_percent: Decimal = Field(default=Decimal("3"), gt=0, le=25)
    allocation_slots: int = Field(default=3, ge=1, le=10)
    auto_rebalance: bool = True
    max_portfolio_risk_percent: Decimal = Field(default=Decimal("40"), gt=0, le=75)
    minimum_free_margin: Decimal = Field(default=Decimal("10"), gt=0)
    max_order_slippage_percent: Decimal = Field(default=Decimal("0.5"), gt=0, le=5)
    unusual_stop_distance_percent: Decimal = Field(default=Decimal("5"), gt=0, le=25)
    liquidation_buffer_percent: Decimal = Field(default=Decimal("0.25"), gt=0, le=5)
    reconciliation_interval_seconds: float = Field(default=5, ge=1, le=60)
    telegram_scan_interval_seconds: float = Field(default=10, ge=1, le=60)
    order_poll_attempts: int = Field(default=8, ge=1, le=30)
    order_poll_interval_seconds: float = Field(default=0.5, ge=0, le=5)
    http_timeout_seconds: float = Field(default=10, gt=0, le=30)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("okx_demo", "live_trading_enabled", mode="before")
    @classmethod
    def parse_switch(cls, value):
        if isinstance(value, str):
            if value.lower() in {"1", "true"}:
                return True
            if value.lower() in {"0", "false"}:
                return False
        return value

    @model_validator(mode="after")
    def database_driver(self):
        demo = (self.okx_demo_api_key, self.okx_demo_api_secret, self.okx_demo_api_passphrase)
        if any(v.get_secret_value() for v in demo):
            if not all(v.get_secret_value() for v in demo):
                raise ValueError("All three explicit demo credentials are required together")
            # No live credential fields are accepted by this release.
            object.__setattr__(self, "okx_api_key", demo[0])
            object.__setattr__(self, "okx_api_secret", demo[1])
            object.__setattr__(self, "okx_api_passphrase", demo[2])
        if not self.database_url.startswith(("postgresql+asyncpg://", "sqlite+aiosqlite://")):
            raise ValueError("Use postgresql+asyncpg (SQLite is for offline tests only)")
        if self.mztradingbot_channel_id and (
            self.mztradingbot_channel_id != self.telegram_channel_id
        ):
            raise ValueError("MZTRADINGBOT_CHANNEL_ID must equal the allowed TELEGRAM_CHANNEL_ID")
        return self

    def validate_runtime(self) -> None:
        required = (
            "okx_api_key",
            "okx_api_secret",
            "okx_api_passphrase",
            "telegram_api_hash",
            "telegram_bot_token",
            "admin_api_token",
        )
        missing = [name for name in required if not getattr(self, name).get_secret_value()]
        if (
            missing
            or not self.telegram_api_id
            or not self.telegram_channel_id
            or not self.telegram_admin_id
        ):
            raise ValueError(f"Missing runtime configuration: {', '.join(missing)} / Telegram IDs")
        if len(self.admin_api_token.get_secret_value()) < 32:
            raise ValueError("ADMIN_API_TOKEN must contain at least 32 characters")
        if (
            not self.telegram_session_string.get_secret_value()
            and not self.telegram_session_file.is_file()
        ):
            raise ValueError("Run telegram-session first, or set TELEGRAM_SESSION_STRING")
        if not self.database_url.startswith("postgresql+asyncpg://"):
            raise ValueError("Service runtime requires PostgreSQL")
