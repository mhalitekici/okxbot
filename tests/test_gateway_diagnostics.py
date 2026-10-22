import httpx
import pytest

from okx_bot.domain import RiskRejected
from okx_bot.exchange.client import OKXClient
from okx_bot.exchange.gateway import OKXGateway
from scripts.diagnose import ReadOnlyClient


async def test_real_account_validation_reports_actual_mode_without_account_identifiers(settings):
    client = OKXClient(
        settings,
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"code": "0", "data": [{"acctLv": "1", "uid": "private-account-id"}]},
            )
        ),
    )
    try:
        with pytest.raises(RiskRejected) as caught:
            await OKXGateway(client).account_config()
        assert caught.value.code == "UNSUPPORTED_ACCOUNT_MODE"
        assert caught.value.context == {"account_level": "1", "expected_account_level": "2"}
        assert "private-account-id" not in str(caught.value.context)
    finally:
        await client.close()


async def test_pending_algos_checks_every_supported_type_in_demo(settings):
    seen = []

    def handle(request):
        assert request.headers["x-simulated-trading"] == "1"
        assert request.url.path == "/api/v5/trade/orders-algo-pending"
        assert "instType" not in request.url.params  # Detect foreign exposure across the account.
        seen.append(request.url.params["ordType"])
        return httpx.Response(200, json={"code": "0", "data": []})

    client = OKXClient(settings, httpx.MockTransport(handle))
    try:
        assert await OKXGateway(client).open_algo_orders() == []
        assert seen == [
            "conditional,oco",
            "trigger",
            "move_order_stop",
            "iceberg",
            "twap",
            "smart_iceberg",
            "chase",
        ]
    finally:
        await client.close()


async def test_diagnostic_transport_cannot_send_any_mutation(settings):
    client = ReadOnlyClient(
        settings,
        httpx.MockTransport(lambda _: pytest.fail("No HTTP mutation may be made by diagnostics")),
    )
    try:
        with pytest.raises(RuntimeError, match="only permits GET"):
            await client.request("POST", "/api/v5/trade/order", payload={})
        with pytest.raises(RuntimeError, match="only permits GET"):
            await client.request("POST", "/api/v5/account/set-leverage", payload={})
    finally:
        await client.close()


async def test_usdt_equity_available_margin_and_upl_are_distinct(settings):
    from decimal import Decimal

    def wire(request):
        assert request.url.path == "/api/v5/account/balance"
        return httpx.Response(
            200,
            json={
                "code": "0",
                "data": [
                    {
                        "details": [
                            {
                                "ccy": "USDT",
                                "eq": "1500",
                                "availBal": "1000",
                                "upl": "30",
                                "isoEq": "510",
                                "isoUpl": "20",
                                "imr": "10",
                                "ordFrozen": "5",
                            }
                        ]
                    }
                ],
            },
        )

    client = OKXClient(settings, httpx.MockTransport(wire))
    try:
        balance = await OKXGateway(client).balance()
        assert balance.equity == 1500 and balance.available == 1000
        assert balance.unrealized == 30  # isoUpl is already included, never added twice.
        assert balance.used_margin == Decimal(500) and balance.order_frozen == 5
    finally:
        await client.close()
