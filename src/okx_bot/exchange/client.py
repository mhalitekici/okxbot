import asyncio
import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx

from okx_bot.config import Settings
from okx_bot.domain import ExchangeError


class OKXClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        if settings.okx_base_url not in {
            "https://www.okx.com",
            "https://eea.okx.com",
            "https://us.okx.com",
        }:
            raise ExchangeError("UNSAFE_ENDPOINT")
        self.http = httpx.AsyncClient(
            base_url=settings.okx_base_url,
            timeout=settings.http_timeout_seconds,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def close(self):
        await self.http.aclose()

    def _headers(self, method: str, path: str, body: str, private: bool) -> dict:
        if not self.settings.okx_demo or self.settings.live_trading_enabled:
            raise ExchangeError("LIVE_TRADING_DISABLED")
        headers = {"Content-Type": "application/json", "x-simulated-trading": "1"}
        if private:
            stamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            signature = base64.b64encode(
                hmac.new(
                    self.settings.okx_api_secret.get_secret_value().encode(),
                    f"{stamp}{method}{path}{body}".encode(),
                    hashlib.sha256,
                ).digest()
            ).decode()
            headers.update(
                {
                    "OK-ACCESS-KEY": self.settings.okx_api_key.get_secret_value(),
                    "OK-ACCESS-SIGN": signature,
                    "OK-ACCESS-TIMESTAMP": stamp,
                    "OK-ACCESS-PASSPHRASE": self.settings.okx_api_passphrase.get_secret_value(),
                }
            )
        return headers

    async def request(self, method: str, path: str, *, params=None, payload=None, private=True):
        if (
            not path.startswith("/api/v5/")
            or ":" in path
            or "?" in path
            or "#" in path
            or method not in {"GET", "POST"}
        ):
            raise ExchangeError("UNSAFE_ENDPOINT")
        query = urlencode(sorted((params or {}).items()))
        target = path + (f"?{query}" if query else "")
        body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
        # Conservative per-process pacing. POSTs are never retried: an absent ACK
        # does not prove that an order was not accepted by the matching engine.
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            response = None
            try:
                async with self._lock:
                    await asyncio.sleep(max(0, 0.22 - (time.monotonic() - self._last_request)))
                    self._last_request = time.monotonic()
                    response = await self.http.request(
                        method,
                        target,
                        content=body or None,
                        headers=self._headers(method, target, body, private),
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    raise ExchangeError(f"HTTP_{response.status_code}", ambiguous=method != "GET")
                if response.status_code != 200:
                    raise ExchangeError(f"HTTP_{response.status_code}", ambiguous=method != "GET")
                data = response.json()
                if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                    raise ValueError("Invalid response envelope")
                if data.get("code") != "0":
                    code = str(data.get("code", "INVALID_RESPONSE"))
                    raise ExchangeError(
                        code, ambiguous=method != "GET" and code in {"50004", "50026"}
                    )
                for item in data["data"]:
                    if item.get("sCode", "0") != "0":
                        code = str(item["sCode"])
                        raise ExchangeError(
                            code, ambiguous=method != "GET" and code in {"50004", "50026"}
                        )
                return data["data"]
            except (httpx.TransportError, ValueError, KeyError, TypeError) as exc:
                error = ExchangeError(
                    "TRANSPORT_OR_RESPONSE",
                    ambiguous=method != "GET",
                    endpoint=path,
                    method=method,
                    http_status=response.status_code if response else None,
                )
                if attempt + 1 == attempts:
                    raise error from exc
            except ExchangeError as exc:
                exc.endpoint, exc.method = path, method
                exc.http_status = response.status_code if response is not None else None
                if attempt + 1 == attempts or not (
                    exc.code.startswith("HTTP_5") or exc.code in {"HTTP_429", "50011"}
                ):
                    raise
            await asyncio.sleep(0.5 * (attempt + 1))
        raise ExchangeError("RETRIES_EXHAUSTED")


class ReadOnlyOKXClient(OKXClient):
    """Acceptance probes cannot place, amend, close, cancel or change leverage."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.read_count = 0

    async def request(self, method, path, **kwargs):
        if method != "GET":
            raise RuntimeError("Diagnostic client only permits GET requests")
        self.read_count += 1
        return await super().request(method, path, **kwargs)
