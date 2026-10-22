import base64
import hashlib
import hmac

import httpx
import pytest

from okx_bot.domain import ExchangeError
from okx_bot.exchange.client import OKXClient


async def test_signed_query_and_exact_json_body(settings):
    settings.okx_api_key = "demo-key"
    settings.okx_api_secret = "demo-secret"
    settings.okx_api_passphrase = "demo-passphrase"
    seen = []

    async def handle(request):
        seen.append(request)
        assert request.headers["x-simulated-trading"] == "1"
        assert request.headers["OK-ACCESS-KEY"] == "demo-key"
        prehash = (
            request.headers["OK-ACCESS-TIMESTAMP"]
            + request.method
            + request.url.raw_path.decode()
            + request.content.decode()
        )
        signature = base64.b64encode(
            hmac.new(b"demo-secret", prehash.encode(), hashlib.sha256).digest()
        ).decode()
        assert request.headers["OK-ACCESS-SIGN"] == signature
        return httpx.Response(200, json={"code": "0", "data": [{"ordId": "123", "sCode": "0"}]})

    client = OKXClient(settings, httpx.MockTransport(handle))
    try:
        await client.request(
            "GET", "/api/v5/trade/order", params={"instId": "BTC-USDT-SWAP", "clOrdId": "e1"}
        )
        await client.request("POST", "/api/v5/trade/order", payload={"sz": "1", "side": "buy"})
        assert seen[1].content == b'{"sz":"1","side":"buy"}'
    finally:
        await client.close()


@pytest.mark.parametrize(
    "payload,ambiguous",
    [
        ({"code": "0", "data": [{"sCode": "51008"}]}, False),
        ({"code": "50004", "data": []}, True),
        ({"code": "0", "data": [{"sCode": "50004"}]}, True),
    ],
)
async def test_exchange_rejection_and_timeout_semantics(settings, payload, ambiguous):
    client = OKXClient(settings, httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    try:
        with pytest.raises(ExchangeError) as caught:
            await client.request("POST", "/api/v5/trade/order", payload={})
        assert caught.value.ambiguous is ambiguous
    finally:
        await client.close()


async def test_transport_does_not_retry_mutations(settings):
    calls = []

    def disconnected(request):
        calls.append(request)
        raise httpx.ReadTimeout("Secret URL must not appear in wrapped error", request=request)

    client = OKXClient(settings, httpx.MockTransport(disconnected))
    try:
        with pytest.raises(ExchangeError) as caught:
            await client.request("POST", "/api/v5/trade/order", payload={})
        assert caught.value.ambiguous
        assert str(caught.value) == "OKX error TRANSPORT_OR_RESPONSE"
        assert len(calls) == 1
    finally:
        await client.close()


async def test_read_retries_transient_errors(settings):
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"code": "0", "data": []})

    client = OKXClient(settings, httpx.MockTransport(handle))
    try:
        assert await client.request("GET", "/api/v5/account/positions") == []
        assert len(calls) == 2
    finally:
        await client.close()


async def test_live_guard_also_applies_to_unvalidated_settings(settings):
    unsafe = settings.model_copy(update={"okx_demo": False})
    client = OKXClient(unsafe, httpx.MockTransport(lambda _: pytest.fail("No request may be sent")))
    try:
        with pytest.raises(ExchangeError, match="LIVE_TRADING_DISABLED"):
            await client.request("POST", "/api/v5/trade/order", payload={})
    finally:
        await client.close()


async def test_errors_keep_safe_endpoint_and_http_status(settings):
    client = OKXClient(
        settings,
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"code": "50101", "msg": "provider text containing a secret", "data": []},
            )
        ),
    )
    try:
        with pytest.raises(ExchangeError) as caught:
            await client.request("GET", "/api/v5/account/config")
        assert caught.value.endpoint == "/api/v5/account/config"
        assert caught.value.http_status == 200
        assert caught.value.method == "GET"
        assert "secret" not in str(caught.value)
    finally:
        await client.close()
