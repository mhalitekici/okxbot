"""File-only Live observer configuration and manually attested account pinning."""

import asyncio
import hashlib
import hmac
import re
import tomllib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

from dotenv.parser import parse_stream
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy.engine import URL

from okx_bot.limits import Limits
from okx_bot.live_connection import CONFIG, LIVE_HOSTS, LiveReadOnlyClient


class DeploymentError(ValueError):
    """Use only our fixed codes; never propagate raw provider/config/DB exceptions."""

    def __init__(self, code="LIVE_DEPLOYMENT_BLOCKED"):
        allowed = {
            "LIVE_DEPLOYMENT_BLOCKED",
            "IDENTITY_REQUIRED",
            "IDENTITY_MISMATCH",
            "MANUAL_SUBACCOUNT_CONFIRMATION_REQUIRED",
            "SUBACCOUNT_REQUIRED",
            "DATABASE_GUARD",
            "SCHEMA_MIGRATION_REQUIRED",
            "EXECUTION_DISABLED",
            "EXTERNAL_EXPOSURE",
            "CONNECTION_LOST",
            "TELEGRAM_DISCONNECTED",
        }
        self.code = code if code in allowed else "LIVE_DEPLOYMENT_BLOCKED"
        super().__init__(self.code)


class LiveDeployment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    mode: Literal["live-observer"] = "live-observer"
    paused: Literal[True] = True
    live_trading_enabled: Literal[False] = False
    subaccount: Literal["TelegramBot1"] = "TelegramBot1"
    telegram_channel_id: Literal[-1003769687656] = -1003769687656
    telegram_topic_id: Literal[23576] = 23576
    mztradingbot_channel_id: Literal[-1003769687656] = -1003769687656
    allocation_slots: Literal[3] = 3
    leverage: Literal[10] = 10
    margin_mode: Literal["isolated"] = "isolated"
    max_portfolio_risk_percent: Decimal = Field(default=Decimal(3), gt=0, le=3)
    max_daily_loss_percent: Decimal = Field(default=Decimal(1), gt=0, le=1)
    minimum_free_margin: Decimal = Field(default=Decimal(50), ge=50)
    minimum_free_margin_percent: Decimal = Field(default=Decimal(10), ge=10, le=50)
    poll_seconds: int = Field(default=15, ge=10, le=60)

    @classmethod
    def from_file(cls, path):
        try:
            with Path(path).open("rb") as stream:
                return cls.model_validate(tomllib.load(stream))
        except Exception:
            raise DeploymentError() from None

    def limits(self, equity: Decimal) -> Limits:
        return Limits(
            allocation_slots=3,
            max_open_positions=3,
            max_portfolio_risk_percent=self.max_portfolio_risk_percent,
            max_daily_loss_percent=self.max_daily_loss_percent,
            minimum_free_margin=max(
                self.minimum_free_margin, equity * self.minimum_free_margin_percent / 100
            ),
        )


class TelegramSecrets(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    telegram_api_id: int = Field(gt=0)
    telegram_api_hash: SecretStr = Field(repr=False)
    telegram_bot_token: SecretStr = Field(repr=False)
    telegram_admin_id: int = Field(gt=0)
    admin_api_token: SecretStr = Field(repr=False)

    @classmethod
    def from_file(cls, path):
        try:
            values = {}
            with Path(path).open(encoding="utf-8-sig") as stream:
                for item in parse_stream(stream):
                    if item.error or (item.key and item.value is None):
                        raise DeploymentError()
                    if not item.key:
                        continue
                    key = item.key.lower()
                    if key in values:
                        raise DeploymentError()
                    values[key] = item.value
            result = cls.model_validate(values)
            for name in ("telegram_api_hash", "telegram_bot_token", "admin_api_token"):
                raw = getattr(result, name).get_secret_value()
                if not raw or "${" in raw or any(ord(c) < 32 for c in raw):
                    raise DeploymentError()
            if len(result.admin_api_token.get_secret_value()) < 32:
                raise DeploymentError()
            return result
        except Exception:
            raise DeploymentError() from None


class AccountIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    version: Literal[1] = 1
    environment: Literal["live"] = "live"
    subaccount: Literal["TelegramBot1"] = "TelegramBot1"
    manually_verified: Literal[True]
    base_url: str
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$", repr=False)
    verified_at: datetime
    expires_at: datetime

    @classmethod
    def from_file(cls, path):
        try:
            return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))
        except Exception:
            raise DeploymentError("IDENTITY_REQUIRED") from None

    def verify(self, config, row: dict):
        now = datetime.now(UTC)
        if (
            self.base_url not in LIVE_HOSTS
            or self.base_url != config.base_url
            or self.verified_at.tzinfo is None
            or self.expires_at.tzinfo is None
            or not self.verified_at <= now < self.expires_at
            or self.expires_at - self.verified_at > timedelta(days=30)
            or not hmac.compare_digest(self.fingerprint, account_fingerprint(row))
        ):
            raise DeploymentError("IDENTITY_MISMATCH")


def account_fingerprint(row: dict) -> str:
    uid, main = row.get("uid"), row.get("mainUid")
    if (
        not isinstance(uid, str)
        or not re.fullmatch(r"[0-9]{1,40}", uid)
        or not isinstance(main, str)
        or not re.fullmatch(r"[0-9]{1,40}", main)
        or uid == main
    ):
        raise DeploymentError("SUBACCOUNT_REQUIRED")
    return hashlib.sha256(("live:" + uid).encode()).hexdigest()


async def enroll_identity(config, output: Path, manual_subaccount: str, *, transport=None):
    # This attests the operator's OKX UI check. API label is NOT a subaccount name.
    if manual_subaccount != "TelegramBot1":
        raise DeploymentError("MANUAL_SUBACCOUNT_CONFIRMATION_REQUIRED")
    client = LiveReadOnlyClient(config, transport)
    try:
        rows = await client.request("GET", CONFIG)
        if len(rows) != 1 or rows[0].get("acctLv") != "2" or rows[0].get("posMode") != "net_mode":
            raise DeploymentError("IDENTITY_MISMATCH")
        now = datetime.now(UTC)
        identity = AccountIdentity(
            manually_verified=True,
            base_url=config.base_url,
            fingerprint=account_fingerprint(rows[0]),
            verified_at=now,
            expires_at=now + timedelta(days=30),
        )

        # Refuse accidental overwrite; renewal needs the operator to archive the old pin.
        def save():
            with output.open("x", encoding="utf-8") as stream:
                stream.write(identity.model_dump_json(indent=2))
            output.chmod(0o600)

        await asyncio.to_thread(save)
        return identity
    finally:
        await client.close()


def live_database_url(password_file=Path("/run/secrets/postgres_password")) -> str:
    try:
        password = Path(password_file).read_text(encoding="utf-8").strip()
        if len(password) < 24 or any(ord(c) < 32 for c in password):
            raise DeploymentError()
        return URL.create(
            "postgresql+asyncpg",
            username="okx_live",
            password=password,
            host="postgres-live",
            database="okx_live",
            port=5432,
        ).render_as_string(hide_password=False)
    except Exception:
        raise DeploymentError() from None
