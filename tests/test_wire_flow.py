import httpx
import pytest
from sqlalchemy import select

from okx_bot.domain import Message, utcnow
from okx_bot.engine import TradingEngine
from okx_bot.exchange.client import OKXClient
from okx_bot.exchange.gateway import OKXGateway
from okx_bot.models import BotEvent, Position
from tests.fakes import text


@pytest.mark.parametrize("side,cause", [("LONG", "tp"), ("SHORT", "sl")])
@pytest.mark.parametrize("slip", ["0", "0.2", "0.5"])
async def test_message_to_signed_okx_order_and_protection_over_mock_http(
    settings, database, exchange, side, cause, slip
):
    import json
    from decimal import Decimal

    exchange.market = Decimal("100") + Decimal(slip) * (1 if side == "LONG" else -1)

    async def wire(request):
        # This suite checks the signed protocol, not real-time request pacing.
        # The transport/retry suite exercises the production pacing implementation.
        client._last_request = 0
        assert request.headers["x-simulated-trading"] == "1"
        path = request.url.path
        stamp = str(int(utcnow().timestamp() * 1000))
        match (request.method, path):
            case "GET", "/api/v5/account/config":
                data = [await exchange.account_config()]
            case "GET", "/api/v5/account/balance":
                data = [
                    {"details": [{"ccy": "USDT", "eq": "1000", "availBal": "1000", "upl": "0"}]}
                ]
            case "GET", "/api/v5/account/positions":
                data = await exchange.positions()
            case (
                "GET",
                "/api/v5/trade/orders-pending"
                | "/api/v5/account/bills"
                | "/api/v5/account/bills-archive"
                | "/api/v5/trade/fills-history",
            ):
                data = await exchange.fills(utcnow()) if path.endswith("fills-history") else []
            case "GET", "/api/v5/trade/orders-algo-pending":
                data = await exchange.open_algo_orders()
            case "GET", "/api/v5/account/instruments":
                assert request.url.params["instType"] == "SWAP"
                data = [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "instType": "SWAP",
                        "instFamily": "BTC-USDT",
                        "groupId": "4",
                        "settleCcy": "USDT",
                        "ctValCcy": "BTC",
                        "ctType": "linear",
                        "state": "live",
                        "ruleType": "normal",
                        "ctVal": "1",
                        "tickSz": "0.1",
                        "lotSz": "1",
                        "minSz": "1",
                        "maxLmtSz": "10000",
                    }
                ]
            case "GET", "/api/v5/market/ticker":
                data = [{"bidPx": str(exchange.market), "askPx": str(exchange.market), "ts": stamp}]
            case "GET", "/api/v5/public/mark-price":
                data = [{"markPx": str(exchange.market), "ts": stamp}]
            case "GET", "/api/v5/public/position-tiers":
                assert request.url.params["tdMode"] == "isolated"
                data = [{"minSz": "0", "maxSz": "10000", "mmr": "0.005", "maxLever": "100"}]
            case "GET", "/api/v5/account/trade-fee":
                data = [{"feeGroup": [{"groupId": "4", "taker": "-0.0005"}]}]
            case "POST", "/api/v5/account/set-leverage":
                payload = json.loads(request.content)
                assert payload == {"instId": "BTC-USDT-SWAP", "lever": "10", "mgnMode": "isolated"}
                data = [{"lever": "10"}]
            case "GET", "/api/v5/account/leverage-info":
                data = [{"lever": "10", "mgnMode": "isolated", "posSide": "net"}]
            case "POST", "/api/v5/trade/order":
                data = [await exchange.place_entry(json.loads(request.content))]
            case "GET", "/api/v5/trade/order":
                data = (
                    [await exchange.order_by_id("BTC-USDT-SWAP", request.url.params["ordId"])]
                    if "ordId" in request.url.params
                    else [await exchange.order("BTC-USDT-SWAP", request.url.params["clOrdId"])]
                )
            case "GET", "/api/v5/trade/order-algo":
                data = [await exchange.algo("")]
            case "POST", "/api/v5/trade/amend-algos":
                payload = json.loads(request.content)
                assert isinstance(payload, dict) and payload["cxlOnFail"] is False
                await exchange.amend_protection(
                    payload["instId"],
                    payload["algoId"],
                    Decimal(payload["newTpTriggerPx"]),
                    Decimal(payload["newSlTriggerPx"]),
                )
                data = [{"algoId": payload["algoId"], "sCode": "0"}]
            case _:
                raise AssertionError(f"Unexpected endpoint: {request.method} {path}")
        return httpx.Response(200, json={"code": "0", "data": data})

    client = OKXClient(settings, httpx.MockTransport(wire))
    try:
        engine = TradingEngine(settings, database, OKXGateway(client))
        await engine.reconcile(startup=True)
        assert (
            await engine.ingest(
                Message(
                    settings.telegram_channel_id,
                    1,
                    text(side),
                    utcnow(),
                    topic_id=settings.telegram_topic_id,
                )
            )
            == "open"
        )
        assert len(exchange.submissions) == 1
        restarted = TradingEngine(settings, database, OKXGateway(client))
        await restarted.reconcile(startup=True)
        assert not (await restarted.state()).emergency
        async with database.sessions() as session:
            position = await session.scalar(select(Position))
            assert position.status == "open"
            assert position.avg_price == exchange.market
        exchange.finish(cause)
        await restarted.reconcile()
        async with database.sessions() as session:
            assert (await session.scalar(select(Position))).status == "closed"
            event = await session.scalar(select(BotEvent).where(BotEvent.kind == "position_closed"))
            assert event.details["cause"] == cause
        assert len(exchange.submissions) == 1
    finally:
        await client.close()
