"""Standalone production diagnostics. Never import the trading application here."""

import argparse
import asyncio
import base64
import hashlib
import hmac
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode

import httpx
from dotenv.parser import parse_stream
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

LIVE_HOSTS = frozenset(
    {"https://openapi.okx.com", "https://www.okx.com", "https://eea.okx.com", "https://us.okx.com"}
)
CONFIG = "/api/v5/account/config"
BALANCE = "/api/v5/account/balance"
POSITIONS = "/api/v5/account/positions"
ORDERS = "/api/v5/trade/orders-pending"
ALGOS = "/api/v5/trade/orders-algo-pending"
READ_PATHS = frozenset({CONFIG, BALANCE, POSITIONS, ORDERS, ALGOS})
REQUEST_INTERVAL = 0.11  # Algo reads allow 20 requests / 2 seconds; no parallel bursts.
ACCOUNT_MODES = {
    "1": "Spot",
    "2": "Futures (2)",
    "3": "Multi-currency margin",
    "4": "Portfolio margin",
}
POSITION_MODES = {"net_mode": "Net (net_mode)", "long_short_mode": "Long/short (long_short_mode)"}
ERRORS = {
    "ARGUMENTS": "Use --env-file with an explicit dedicated Live configuration file.",
    "ENV_FILE": "The supplied Live configuration file could not be read.",
    "ENV_FORMAT": "Invalid, duplicate or unsupported configuration fields; see the Live guide.",
    "ENVIRONMENT": "Demo/Live configuration mismatch. Only explicit Live credentials are allowed.",
    "LIVE_GATE": "LIVE_TRADING_ENABLED must remain false. No request was sent.",
    "CREDENTIALS": "Live credentials are incomplete, conflicting or invalid.",
    "DOMAIN": "Set an explicit, allowlisted regional OKX_BASE_URL. Domain fallback is disabled.",
    "READ_ONLY": "Blocked by the GET-only endpoint and parameter allowlist.",
    "RESPONSE": "Invalid or incomplete API response; verification stopped.",
    "ACCOUNT_MODE": "Account mode mismatch: the existing bot requires Futures mode (acctLv=2).",
    "POSITION_MODE": "Position mode is unsupported or differs from the expected mode.",
    "PAGINATION": "Incomplete or inconsistent order pagination; counts cannot be verified.",
    "TRANSPORT": "Connection, TLS or timeout failure; no response received. No automatic retry.",
    "HTTP": "The server rejected the read request.",
    "AUTH": "Authentication failed; verify the Live credentials and account region locally.",
    "PERMISSION": "Read access denied; check API permissions and the IP allowlist locally.",
    "REDIRECT": "Redirect refused. Select the correct regional API domain explicitly.",
    "RATE_LIMIT": "API rate limit reached; try the read-only check again later.",
    "PROVIDER": "OKX rejected the read request; no raw provider message is displayed.",
    "50101": "Demo/Live credential environment mismatch. Production credentials are required.",
    "50102": "Request timestamp expired; verify Windows clock synchronization.",
    "50105": "API passphrase rejected; verify it locally.",
    "50110": "IP authorization rejected; check the API IP allowlist locally.",
    "50111": "Invalid API key; verify the Live key and selected regional domain locally.",
    "50113": "API signature rejected; verify the Live secret locally.",
    "50119": "API key not found; verify the account region and regional API domain locally.",
    "50120": "API key lacks permission for this read request.",
    "51010": "The current account mode does not support this read request.",
    "50011": "API rate limit reached; try the read-only check again later.",
    "INTERRUPTED": "Check interrupted; no trading action was performed.",
    "INTERNAL": "Check stopped safely due to an unexpected local error; details suppressed.",
}
CONFIG_REASONS = {
    "demo_flag": "The selected file must set OKX_DEMO to 0 or false when provided.",
    "environment_label": "The selected file must set OKX_ENVIRONMENT to live or production.",
    "demo_credentials": (
        "The selected file contains non-empty OKX_DEMO_API_* fields; remove the mix."
    ),
    "live_declaration": (
        "Use OKX_LIVE_API_* or declare OKX_DEMO=0 / OKX_ENVIRONMENT=live in the selected file."
    ),
    "credentials_missing": (
        "Provide all three fields in every credential name group that is present."
    ),
    "credentials_conflict": "OKX_LIVE_API_* and OKX_API_* in the selected file disagree.",
}


class LiveCheckError(Exception):
    """Only fixed descriptions and HTTP status are allowed across the output boundary."""

    def __init__(self, code: str, http_status: int | None = None, *, reason: str | None = None):
        self.code = code if code in ERRORS else "INTERNAL"
        self.http_status = http_status
        self.reason = reason if reason in CONFIG_REASONS else None
        message = ERRORS[self.code]
        if self.reason:
            message += " " + CONFIG_REASONS[self.reason]
        super().__init__(message)


class LiveReadOnlyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    base_url: str
    api_key: SecretStr = Field(repr=False)
    api_secret: SecretStr = Field(repr=False)
    api_passphrase: SecretStr = Field(repr=False)
    # These are checked again by the HTTP client, including model_construct bypasses.
    demo: bool = False
    live_trading_enabled: bool = False
    expected_position_mode: str | None = None


def _one(values: dict[str, str], *names: str) -> str:
    present = [values[name] for name in names if name in values]
    if len(set(present)) > 1:
        raise LiveCheckError("ENV_FORMAT")
    return present[0] if present else ""


def _file_credentials(values: dict[str, str]) -> tuple[SecretStr, SecretStr, SecretStr]:
    """Select a complete group from the explicit file; never merge partial groups."""
    groups = []
    for prefix in ("OKX_LIVE_API_", "OKX_API_"):
        names = [prefix + suffix for suffix in ("KEY", "SECRET", "PASSPHRASE")]
        if any(name in values for name in names):
            if not all(values.get(name) for name in names):
                raise LiveCheckError("CREDENTIALS", reason="credentials_missing")
            groups.append(tuple(values[name] for name in names))
    if not groups:
        raise LiveCheckError("CREDENTIALS", reason="credentials_missing")
    if len(groups) == 2 and groups[0] != groups[1]:
        raise LiveCheckError("CREDENTIALS", reason="credentials_conflict")
    key, secret, passphrase = groups[0]
    return SecretStr(key), SecretStr(secret), SecretStr(passphrase)


def validate_live_config(config: LiveReadOnlyConfig) -> None:
    if config.live_trading_enabled is not False:
        raise LiveCheckError("LIVE_GATE")
    if config.demo is not False:
        raise LiveCheckError("ENVIRONMENT")
    if config.base_url not in LIVE_HOSTS:
        raise LiveCheckError("DOMAIN")
    if config.expected_position_mode not in {None, *POSITION_MODES}:
        raise LiveCheckError("POSITION_MODE")
    for secret in (config.api_key, config.api_secret, config.api_passphrase):
        raw = secret.get_secret_value()
        if (
            not raw
            or len(raw) > 512
            or raw != raw.strip()
            or not raw.isascii()
            or any(ord(char) < 32 or ord(char) == 127 for char in raw)
            or "${" in raw
        ):
            raise LiveCheckError("CREDENTIALS")


def load_live_config(env_file: str | Path) -> LiveReadOnlyConfig:
    """Read ONLY the explicitly supplied file at operator runtime; never load .env/os.environ.

    The low-level dotenv parser performs no interpolation or environment writes and
    emits no parser warnings containing source lines. Reject ambiguity instead of
    dotenv's usual silent duplicate/invalid-line handling.
    """
    values: dict[str, str] = {}
    try:
        with Path(env_file).open(encoding="utf-8-sig") as stream:
            for binding in parse_stream(stream):
                if binding.error:
                    raise LiveCheckError("ENV_FORMAT")
                if binding.key is None:
                    continue
                key = binding.key.upper()
                if key in values or binding.value is None:
                    raise LiveCheckError("ENV_FORMAT")
                values[key] = binding.value
    except (OSError, UnicodeError, ValueError):
        raise LiveCheckError("ENV_FILE") from None

    credential_fields = {
        f"OKX_{prefix}API_{suffix}"
        for prefix in ("", "LIVE_", "DEMO_")
        for suffix in ("KEY", "SECRET", "PASSPHRASE")
    }
    allowed = credential_fields | {
        "OKX_DEMO",
        "OKX_ENVIRONMENT",
        "LIVE_TRADING_ENABLED",
        "OKX_BASE_URL",
        "OKX_LIVE_BASE_URL",
        "OKX_EXPECTED_ACCOUNT_MODE",
        "OKX_EXPECTED_POSITION_MODE",
    }
    if values.keys() - allowed:
        raise LiveCheckError("ENV_FORMAT")

    # Normalize only non-secret switches. Credentials are never trimmed or expanded.
    def switch(name: str, default: str = "") -> str:
        return values.get(name, default).strip().lower()

    if switch("LIVE_TRADING_ENABLED", "false") not in {"false", "0"}:
        raise LiveCheckError("LIVE_GATE")
    if "OKX_DEMO" in values and switch("OKX_DEMO") not in {"false", "0"}:
        raise LiveCheckError("ENVIRONMENT", reason="demo_flag")
    if "OKX_ENVIRONMENT" in values and switch("OKX_ENVIRONMENT") not in {"live", "production"}:
        raise LiveCheckError("ENVIRONMENT", reason="environment_label")
    if any(value for key, value in values.items() if key.startswith("OKX_DEMO_API_")):
        raise LiveCheckError("ENVIRONMENT", reason="demo_credentials")
    live_names = [f"OKX_LIVE_API_{suffix}" for suffix in ("KEY", "SECRET", "PASSPHRASE")]
    has_live = any(name in values for name in live_names)
    if not has_live and "OKX_DEMO" not in values and "OKX_ENVIRONMENT" not in values:
        raise LiveCheckError("ENVIRONMENT", reason="live_declaration")
    api_key, api_secret, api_passphrase = _file_credentials(values)
    if values.get("OKX_EXPECTED_ACCOUNT_MODE", "2") != "2":
        raise LiveCheckError("ACCOUNT_MODE")
    try:
        config = LiveReadOnlyConfig(
            base_url=_one(values, "OKX_LIVE_BASE_URL", "OKX_BASE_URL"),
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
            expected_position_mode=values.get("OKX_EXPECTED_POSITION_MODE"),
        )
        validate_live_config(config)
        return config
    except ValidationError:
        raise LiveCheckError("ENV_FORMAT") from None


def _validate_read(method: str, path: str, params: dict[str, str], body: bytes = b""):
    if method != "GET" or path not in READ_PATHS or body:
        raise LiveCheckError("READ_ONLY")
    allowed = {"limit", "after"} if path in {ORDERS, ALGOS} else set()
    if path == ALGOS:
        allowed.add("ordType")
        if params.get("ordType") not in {"conditional", "oco"}:
            raise LiveCheckError("READ_ONLY")
    if params.keys() - allowed:
        raise LiveCheckError("READ_ONLY")
    if "limit" in params and params["limit"] != "100":
        raise LiveCheckError("READ_ONLY")
    if "after" in params and not re.fullmatch(r"[0-9]{1,40}", params["after"]):
        raise LiveCheckError("READ_ONLY")


class LiveReadOnlyClient:
    """Separate from OKXClient/OKXGateway: no trading methods or application lifecycle."""

    def __init__(
        self, config: LiveReadOnlyConfig, transport: httpx.AsyncBaseTransport | None = None
    ):
        validate_live_config(config)
        self._config = config
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._http = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=httpx.Timeout(10.0),
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            event_hooks={"request": [self._guard]},
        )

    async def _guard(self, request: httpx.Request):
        # Last boundary before transport: also protects accidental direct client use.
        validate_live_config(self._config)
        origin = f"{request.url.scheme}://{request.url.netloc.decode()}"
        if origin != self._config.base_url or "x-simulated-trading" in request.headers:
            raise LiveCheckError("READ_ONLY")
        if len(request.url.params.multi_items()) != len(request.url.params):
            raise LiveCheckError("READ_ONLY")
        _validate_read(request.method, request.url.path, dict(request.url.params), request.content)

    async def close(self):
        await self._http.aclose()

    async def request(self, method: str, path: str, *, params=None, payload=None) -> list[dict]:
        params = dict(params or {})
        if payload is not None:
            raise LiveCheckError("READ_ONLY")
        _validate_read(method, path, params)
        validate_live_config(self._config)
        query = urlencode(sorted(params.items()))
        target = path + (f"?{query}" if query else "")
        stamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        signature = base64.b64encode(
            hmac.new(
                self._config.api_secret.get_secret_value().encode(),
                f"{stamp}GET{target}".encode(),
                hashlib.sha256,
            ).digest()
        ).decode()
        headers = {
            "OK-ACCESS-KEY": self._config.api_key.get_secret_value(),
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": stamp,
            "OK-ACCESS-PASSPHRASE": self._config.api_passphrase.get_secret_value(),
        }
        try:
            async with self._lock:
                await asyncio.sleep(
                    max(0, REQUEST_INTERVAL - (time.monotonic() - self._last_request))
                )
                self._last_request = time.monotonic()
                response = await self._http.get(target, headers=headers)
        except httpx.TransportError:
            raise LiveCheckError("TRANSPORT") from None
        status = response.status_code
        if 300 <= status < 400:
            raise LiveCheckError("REDIRECT", status)
        try:
            envelope = response.json()
        except ValueError:
            envelope = None
        code = envelope.get("code") if isinstance(envelope, dict) else None
        # Never reflect arbitrary provider messages/codes, which may contain keys or UIDs.
        provider_code = (
            code if isinstance(code, str) and code in ERRORS and code.isdigit() else None
        )
        if status != 200:
            fallback = {401: "AUTH", 403: "PERMISSION", 429: "RATE_LIMIT"}.get(status, "HTTP")
            raise LiveCheckError(provider_code or fallback, status)
        if code != "0":
            raise LiveCheckError(provider_code or "PROVIDER", status)
        if not isinstance(envelope.get("data"), list) or not all(
            isinstance(row, dict) and row.get("sCode", "0") == "0" for row in envelope["data"]
        ):
            raise LiveCheckError("RESPONSE", status)
        return envelope["data"]


def _number(value) -> Decimal:
    if not isinstance(value, str) or len(value) > 64:
        raise LiveCheckError("RESPONSE", 200)
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise LiveCheckError("RESPONSE", 200) from None
    if (
        not number.is_finite()
        or number.copy_abs() > Decimal("1e24")
        or number.as_tuple().exponent < -24
    ):
        raise LiveCheckError("RESPONSE", 200)
    return number


async def _pending_ids(
    client: LiveReadOnlyClient, path: str, ord_type: str | None = None
) -> set[str]:
    params = {"limit": "100"}
    if ord_type:
        params["ordType"] = ord_type
    id_field = "algoId" if path == ALGOS else "ordId"
    seen: set[str] = set()
    for _ in range(100):
        rows = await client.request("GET", path, params=params)
        if len(rows) > 100:
            raise LiveCheckError("PAGINATION", 200)
        for row in rows:
            order_id = row.get(id_field)
            if (
                not isinstance(order_id, str)
                or not re.fullmatch(r"[0-9]{1,40}", order_id)
                or order_id in seen
                or (ord_type and row.get("ordType") != ord_type)
            ):
                raise LiveCheckError("PAGINATION", 200)
            seen.add(order_id)
        if len(rows) < 100:
            return seen
        params["after"] = rows[-1][id_field]
    raise LiveCheckError("PAGINATION", 200)


@dataclass
class LiveCheckResult:
    connected: bool = False
    account_mode: str = "N/A"
    position_mode: str = "N/A"
    equity: Decimal | None = None
    available: Decimal | None = None
    positions: int | None = None
    orders: int | None = None
    tpsl: int | None = None
    error: LiveCheckError | None = None

    def render(self) -> str:
        def amount(value):
            if value is None:
                return "N/A"
            return format(value, "f") if isinstance(value, Decimal) else str(value)

        lines = [
            f"LIVE API: {'CONNECTED' if self.connected else 'FAILED'}",
            f"Hesap modu: {self.account_mode}",
            f"Pozisyon modu: {self.position_mode}",
            f"Toplam özkaynak (USD): {amount(self.equity)}",
            f"Kullanılabilir bakiye (USDT): {amount(self.available)}",
            f"Açık pozisyon sayısı: {amount(self.positions)}",
            f"Bekleyen emir sayısı: {amount(self.orders)}",
            f"Bekleyen TP/SL sayısı: {amount(self.tpsl)}",
            "Gerçek emir gönderme: DISABLED",
        ]
        if self.error:
            status = self.error.http_status if self.error.http_status is not None else "N/A"
            lines.append(f"HTTP: {status}; Hata: {self.error.code} - {self.error}")
        return "\n".join(lines)


async def check_live_connection(
    config: LiveReadOnlyConfig, *, transport: httpx.AsyncBaseTransport | None = None
) -> LiveCheckResult:
    result = LiveCheckResult()
    client = LiveReadOnlyClient(config, transport)
    try:
        rows = await client.request("GET", CONFIG)
        if len(rows) != 1:
            raise LiveCheckError("RESPONSE", 200)
        account, position = rows[0].get("acctLv"), rows[0].get("posMode")
        # Display only fixed enum labels, never arbitrary response fields or user IDs.
        result.account_mode = (
            ACCOUNT_MODES.get(account, "N/A") if isinstance(account, str) else "N/A"
        )
        result.position_mode = (
            POSITION_MODES.get(position, "N/A") if isinstance(position, str) else "N/A"
        )
        if account != "2":
            raise LiveCheckError("ACCOUNT_MODE", 200)
        if (
            not isinstance(position, str)
            or position not in POSITION_MODES
            or (config.expected_position_mode and config.expected_position_mode != position)
        ):
            raise LiveCheckError("POSITION_MODE", 200)
        balances = await client.request("GET", BALANCE)
        if len(balances) != 1 or not isinstance(balances[0].get("details"), list):
            raise LiveCheckError("RESPONSE", 200)
        result.equity = _number(balances[0].get("totalEq"))
        details = balances[0]["details"]
        if not all(
            isinstance(detail, dict) and isinstance(detail.get("ccy"), str) for detail in details
        ):
            raise LiveCheckError("RESPONSE", 200)
        usdt = [detail for detail in details if detail["ccy"] == "USDT"]
        if len(usdt) > 1:
            raise LiveCheckError("RESPONSE", 200)
        # OKX omits currencies with zero assets; an empty details list is normal.
        result.available = _number(usdt[0].get("availBal")) if usdt else Decimal(0)
        positions = await client.request("GET", POSITIONS)
        result.positions = sum(_number(row.get("pos")) != 0 for row in positions)
        result.orders = len(await _pending_ids(client, ORDERS))
        conditional = await _pending_ids(client, ALGOS, "conditional")
        oco = await _pending_ids(client, ALGOS, "oco")
        if conditional & oco:
            raise LiveCheckError("PAGINATION", 200)
        result.tpsl = len(conditional | oco)
        result.connected = True
    except LiveCheckError as error:
        result.error = error
    finally:
        await client.close()
    return result


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise LiveCheckError("ARGUMENTS")


def main(argv: list[str] | None = None, *, transport=None) -> int:
    # Disable descendants too: disabling only 'httpcore' does not silence its children.
    # Restore the caller's logging threshold even on --help/SystemExit.
    previous_logging_threshold = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    parser = _SafeArgumentParser(
        description="GET-only OKX Live connection check. Trading stays disabled."
    )
    parser.add_argument(
        "--env-file", required=True, help="Explicit path to a dedicated Live env file"
    )
    try:
        args = parser.parse_args(argv)
        config = load_live_config(args.env_file)
        result = asyncio.run(check_live_connection(config, transport=transport))
    except LiveCheckError as error:
        result = LiveCheckResult(error=error)
    except KeyboardInterrupt:
        result = LiveCheckResult(error=LiveCheckError("INTERRUPTED"))
    except Exception:
        # Never render a traceback, exception repr, config object or raw provider text.
        result = LiveCheckResult(error=LiveCheckError("INTERNAL"))
    finally:
        logging.disable(previous_logging_threshold)
    print(result.render())
    return 0 if result.connected else 1


if __name__ == "__main__":
    sys.exit(main())
