from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from okx_bot.domain import (
    Message,
    ReviewRequired,
    RiskRejected,
    utcnow,
)
from okx_bot.engine import TradingEngine
from okx_bot.exchange.client import OKXClient, ReadOnlyOKXClient
from okx_bot.exchange.gateway import OKXGateway
from okx_bot.models import BotEvent, Position, TelegramSignal, TradeOrder
from okx_bot.parser import ChannelSignalParser
from okx_bot.risk import fill_levels
from okx_bot.telegram.formatting import format_notification


def mz_text(side="LONG", symbol="CTUSDT", entry="100", spread="5"):
    origin = "destekten" if side == "LONG" else "dirençten"
    return (
        f"{symbol} {origin} dönüş için pozitif durumda, kontrol etmenizi öneririm.\n"
        f"Anlık Fiyat: {entry}$\nOrtalama Aralık: %{spread}\n"
        "Hedef Fiyat: Ortalama aralığın 0.3 - 0.9 katı\n"
        "Stop Fiyat: Ortalama aralığın 0.3 - 0.9 katı"
    )


def item(settings, content=None, age=0, identifier=1):
    return Message(
        settings.telegram_channel_id,
        identifier,
        content or mz_text(),
        utcnow() - timedelta(seconds=age),
        topic_id=settings.telegram_topic_id,
    )


@pytest.fixture
async def mz_engine(settings, database, exchange):
    settings.mztradingbot_channel_id = settings.telegram_channel_id
    exchange.available_instruments.update({"CT-USDT-SWAP", "BAT-USDT-SWAP", "DOGE-USDT-SWAP"})
    exchange.instrument_tick = D("0.01")
    engine = TradingEngine(settings, database, exchange)
    await engine.reconcile(startup=True)
    return engine


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("symbol", ["CTUSDT", "CT/USDT", "CT-USDT", "BATUSDT", "DOGEUSDT"])
def test_channel_policy_handles_both_sides_symbols_and_legal_youtube_footer(settings, side, symbol):
    raw = mz_text(side, symbol) + (
        "\nhttps://www.youtube.com/shorts/BTCUSDT?LONG=1\n"
        "Yasal uyarı: LONG ve SHORT işlemler risklidir; yatırım tavsiyesi değildir.\n"
        "Kanalın haftalık yayın kaydını izleyebilirsiniz."
    )
    parsed = ChannelSignalParser(settings.telegram_channel_id, settings.telegram_topic_id).parse(
        item(settings, raw)
    )
    assert parsed.direction == side.lower()
    assert parsed.symbol == symbol.replace("/", "").replace("-", "")
    assert parsed.signal_kind == "channel_interpreted_candidate"
    assert parsed.automation_policy == "mztradingbot_verified_topic_patterns_v2"
    assert parsed.notes == raw
    assert (parsed.take_profit, parsed.stop_loss) == (
        (D(103), D(97)) if side == "LONG" else (D(97), D(103))
    )


@pytest.mark.parametrize(
    "content",
    [
        mz_text() + "\nSHORT",
        mz_text("SHORT") + "\nLONG",
        mz_text().replace("pozitif durumda", "pozitif durumda değil"),
        mz_text().replace("destekten", "diren?ten") + "\nSHORT",
        mz_text().replace("pozitif durumda", "pozitif durumda olabilir"),
        mz_text().replace("destekten dönüş", "destekten dönüş yapmayabilir") + "\nLONG",
        mz_text() + "\nLONG açmayın",
        mz_text() + "\nCTUSDT dirençten dönüş için pozitif durumda.",
    ],
)
def test_channel_policy_rejects_conflicting_negated_or_uncertain_words(settings, content):
    with pytest.raises(ReviewRequired):
        ChannelSignalParser(settings.telegram_channel_id, settings.telegram_topic_id).parse(
            item(settings, content)
        )


def test_channel_inference_requires_configured_and_matching_topic(settings):
    for parser, message in [
        (ChannelSignalParser(settings.telegram_channel_id), item(settings)),
        (
            ChannelSignalParser(settings.telegram_channel_id, 23576),
            replace(item(settings), topic_id=None),
        ),
        (
            ChannelSignalParser(settings.telegram_channel_id, 23576),
            replace(item(settings), topic_id=6),
        ),
    ]:
        with pytest.raises(ReviewRequired):
            parser.parse(message)


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("slip", ["0", "0.2", "0.5"])
async def test_bounded_ioc_accepts_zero_point_two_and_boundary_then_rebases_protection(
    settings, mz_engine, exchange, side, slip
):
    exchange.market = D(100) + D(slip) * (1 if side == "LONG" else -1)
    signal = item(settings, mz_text(side, "BATUSDT"))
    assert await mz_engine.ingest(signal) == "open"
    payload = exchange.submissions[0]
    assert payload["ordType"] == "ioc" and payload["tdMode"] == "isolated"
    assert D(payload["px"]) == (D("100.5") if side == "LONG" else D("99.5"))
    assert exchange.leverage_calls == [("BAT-USDT-SWAP", "net", "10", "isolated")]
    async with mz_engine.db.sessions() as session:
        order = await session.scalar(select(TradeOrder))
        stored = await session.scalar(select(TelegramSignal))
        position = await session.scalar(select(Position))
        assert order.reference_price == 100 and position.avg_price == exchange.market
        assert D(order.execution_context["fill_price"]) == exchange.market
        assert order.execution_context["coefficient"] == "0.6"
        assert stored.parsed["automation_policy"] == "mztradingbot_verified_topic_patterns_v2"
        assert D(stored.parsed["take_profit"]) == (103 if side == "LONG" else 97)
        assert order.take_profit == D(exchange.protection["tpTriggerPx"])
        assert order.stop_loss == D(exchange.protection["slTriggerPx"])
        if D(slip):
            assert len(exchange.amendments) == 1
        else:
            assert not exchange.amendments
        opened = await session.scalar(select(BotEvent).where(BotEvent.kind == "order_opened"))
        rendered = format_notification(opened.kind, opened.details)
        for expected in (
            "Referans fiyat: 100 USDT",
            "Fiyat sapması:",
            "İşlem zamanı:",
            "Koruyucu emirler doğrulandı",
        ):
            assert expected in rendered
    restarted = TradingEngine(settings, mz_engine.db, exchange)
    await restarted.reconcile(startup=True)
    assert restarted.healthy and not (await restarted.state()).emergency
    assert await restarted.ingest(signal) == "duplicate"
    assert len(exchange.submissions) == 1


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
async def test_twelve_tenths_price_deviation_rejects_with_turkish_price_details(
    settings, mz_engine, exchange, side
):
    exchange.market = D("101.2") if side == "LONG" else D("98.8")
    assert await mz_engine.ingest(item(settings, mz_text(side))) == "rejected"
    assert not exchange.submissions and not exchange.leverage_calls
    async with mz_engine.db.sessions() as session:
        event = await session.scalar(select(BotEvent).where(BotEvent.kind == "rejected"))
        assert event.details["reason_code"] == "ENTRY_DEVIATION"
        assert D(event.details["price_deviation_percent"]) == D("1.2")
        assert "İzin verilen maksimum sapma: %0,5" in format_notification(event.kind, event.details)


async def test_spread_is_separate_from_directional_price_deviation(settings, mz_engine, exchange):
    exchange.bid, exchange.ask = D("99.6"), D("100.4")
    assert await mz_engine.ingest(item(settings)) == "rejected"
    async with mz_engine.db.sessions() as session:
        event = await session.scalar(select(BotEvent).where(BotEvent.kind == "rejected"))
        assert event.details["reason_code"] == "SPREAD_LIMIT"
    assert not exchange.submissions


async def test_missing_demo_coin_manual_notification_persists_without_orders(
    settings, mz_engine, exchange
):
    message = item(settings, mz_text(symbol="XYZUSDT"))
    assert await mz_engine.ingest(message) == "manual_required"
    assert await mz_engine.ingest(message) == "duplicate"
    restarted = TradingEngine(settings, mz_engine.db, exchange)
    await restarted.reconcile(startup=True)
    assert await restarted.ingest(message) == "duplicate"
    async with mz_engine.db.sessions() as session:
        events = (
            await session.scalars(select(BotEvent).where(BotEvent.kind == "manual_required"))
        ).all()
        assert len(events) == 1
        reply = format_notification(events[0].kind, events[0].details)
        for expected in (
            "MANUEL İŞLEM GEREKLİ",
            "XYZ/USDT",
            "LONG adayı",
            "Hesaplanan TP:",
            "Hesaplanan SL:",
            "Sinyal zamanı:",
            "risk uygunluğu doğrulanmadı",
        ):
            assert expected in reply
        assert "canlı piyasada" not in reply
        assert await session.scalar(select(func.count(TradeOrder.id))) == 0
    assert not exchange.submissions and not exchange.leverage_calls


@pytest.mark.parametrize("failure", ["fail_amend", "ignore_amend"])
async def test_amendment_failure_never_opens_unconfirmed_position(
    settings, mz_engine, exchange, failure
):
    exchange.market = D("100.2")
    setattr(exchange, failure, True)
    assert await mz_engine.ingest(item(settings)) == "emergency"
    assert not exchange.exposure and exchange.closed
    assert (await mz_engine.state()).emergency
    async with mz_engine.db.sessions() as session:
        assert (
            await session.scalar(
                select(func.count(BotEvent.id)).where(BotEvent.kind == "order_opened")
            )
            == 0
        )


async def test_absolute_generic_levels_are_preserved_after_fill(settings, engine, exchange):
    exchange.market = D("100.2")
    text = "BTCUSDT LONG\nEntry: 100\nTP: 104\nSL: 96"
    assert await engine.ingest(item(settings, text)) == "open"
    assert not exchange.amendments
    async with engine.db.sessions() as session:
        order = await session.scalar(select(TradeOrder))
        assert order.take_profit == 104 and order.stop_loss == 96
        assert order.execution_context["take_profit_source"] == "explicit"
        assert order.execution_context["stop_loss_source"] == "explicit"


async def test_open_signal_edit_warns_once_without_changing_protection(
    settings, mz_engine, exchange
):
    message = item(settings)
    assert await mz_engine.ingest(message) == "open"
    edited = replace(message, text=mz_text(spread="6"), edited=True)
    assert await mz_engine.ingest(edited) == "duplicate"
    assert await mz_engine.ingest(edited) == "duplicate"
    assert len(exchange.submissions) == 1 and not exchange.amendments
    async with mz_engine.db.sessions() as session:
        events = (
            await session.scalars(select(BotEvent).where(BotEvent.kind == "signal_edit_review"))
        ).all()
        assert len(events) == 1
        assert "Yeni emir açılmadı" in format_notification(events[0].kind, events[0].details)
        assert (await session.scalar(select(TelegramSignal))).original_text == message.text


async def test_edit_cannot_refresh_original_message_time(settings, mz_engine, exchange):
    old = item(settings, mz_text().replace("pozitif durumda", "belirsiz durumda"), age=120)
    assert await mz_engine.ingest(old) == "needs_review"
    edited = replace(old, text=mz_text(), date=utcnow(), edited=True)
    assert await mz_engine.ingest(edited) == "rejected"
    assert not exchange.submissions


@pytest.mark.parametrize("failure", ["partial", "no_fill", "rejection"])
async def test_non_full_entry_states_never_claim_open(settings, mz_engine, exchange, failure):
    setattr(exchange, failure, True)
    result = await mz_engine.ingest(item(settings))
    assert (
        result == {"partial": "emergency", "no_fill": "canceled", "rejection": "rejected"}[failure]
    )
    async with mz_engine.db.sessions() as session:
        assert (
            await session.scalar(
                select(func.count(BotEvent.id)).where(BotEvent.kind == "order_opened")
            )
            == 0
        )
        if failure == "partial":
            event = await session.scalar(select(BotEvent).where(BotEvent.kind == "entry_partial"))
            assert "KISMEN GERÇEKLEŞTİ" in format_notification(event.kind, event.details)
            assert not exchange.exposure


def instrument_row(state="live"):
    return {
        "instId": "BTC-USDT-SWAP",
        "instType": "SWAP",
        "instFamily": "BTC-USDT",
        "groupId": "4",
        "settleCcy": "USDT",
        "ctValCcy": "BTC",
        "ctType": "linear",
        "state": state,
        "ruleType": "normal",
        "ctVal": "1",
        "tickSz": "0.01",
        "lotSz": "1",
        "minSz": "1",
        "maxLmtSz": "10000",
    }


@pytest.mark.parametrize(
    "state,expected",
    [(None, "DEMO_INSTRUMENT_MISSING"), ("suspend", "INSTRUMENT_SUSPENDED"), ("live", None)],
)
async def test_demo_list_distinguishes_absence_suspension_and_tradability(
    settings, state, expected
):
    def handle(request):
        assert request.method == "GET" and request.headers["x-simulated-trading"] == "1"
        assert dict(request.url.params) == {"instType": "SWAP"}
        return httpx.Response(
            200, json={"code": "0", "data": [] if state is None else [instrument_row(state)]}
        )

    client = OKXClient(settings, httpx.MockTransport(handle))
    try:
        gateway = OKXGateway(client)
        if expected:
            with pytest.raises(RiskRejected) as caught:
                await gateway.instrument("BTC-USDT-SWAP")
            assert caught.value.code == expected
        else:
            assert (await gateway.instrument("BTC-USDT-SWAP")).tick == D("0.01")
        with pytest.raises(RiskRejected) as invalid:
            await gateway.instrument("btc-spot")
        assert invalid.value.code == "INVALID_SYMBOL"
    finally:
        await client.close()


async def test_http_401_is_not_an_absent_coin_or_retried(settings, mz_engine, exchange):
    calls = []

    def unauthorized(request):
        calls.append(request)
        return httpx.Response(401, text="SECRET provider body")

    client = OKXClient(settings, httpx.MockTransport(unauthorized))
    try:
        exchange.instrument = OKXGateway(client).instrument
        assert await mz_engine.ingest(item(settings)) == "failed"
        assert len(calls) == 1 and not exchange.submissions
        assert (await mz_engine.state()).emergency
        async with mz_engine.db.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count(BotEvent.id)).where(BotEvent.kind == "manual_required")
                )
                == 0
            )
    finally:
        await client.close()


async def test_amend_api_uses_object_payload_and_keeps_existing_protection_on_failure(settings):
    def handle(request):
        import json

        p = json.loads(request.content)
        assert request.url.path == "/api/v5/trade/amend-algos"
        assert request.method == "POST" and request.headers["x-simulated-trading"] == "1"
        assert p == {
            "instId": "BTC-USDT-SWAP",
            "algoId": "123",
            "cxlOnFail": False,
            "newTpTriggerPx": "103.20",
            "newTpOrdPx": "-1",
            "newTpTriggerPxType": "mark",
            "newSlTriggerPx": "97.20",
            "newSlOrdPx": "-1",
            "newSlTriggerPxType": "mark",
        }
        return httpx.Response(200, json={"code": "0", "data": [{"algoId": "123", "sCode": "0"}]})

    client = OKXClient(settings, httpx.MockTransport(handle))
    try:
        await OKXGateway(client).amend_protection("BTC-USDT-SWAP", "123", D("103.20"), D("97.20"))
    finally:
        await client.close()


def test_fill_level_risk_checks_do_not_relax_account_or_liquidation_limits():
    order = SimpleNamespace(
        direction="long",
        contracts=D(2),
        take_profit=D(103),
        stop_loss=D(97),
        execution_context={
            "tick": "0.01",
            "fee": "0.0005",
            "contract_value": "1",
            "range_percent": "5",
            "take_profit_source": "range",
            "stop_loss_source": "range",
            "risk_budget": "0.01",
        },
    )
    with pytest.raises(RiskRejected) as risk:
        fill_levels(order, D("100.2"), D(91))
    assert risk.value.code == "FILL_RISK_EXCEEDED"
    order.execution_context["risk_budget"] = "10"
    with pytest.raises(RiskRejected) as liquidation:
        fill_levels(order, D("99.5"), D("96.9"))
    assert liquidation.value.code == "STOP_BEYOND_LIQUIDATION"


@pytest.mark.parametrize(
    "endpoint", ["/api/v5/trade/order", "/api/v5/trade/amend-algos", "/api/v5/account/set-leverage"]
)
async def test_read_only_acceptance_client_blocks_all_trading_mutations(settings, endpoint):
    client = ReadOnlyOKXClient(
        settings, httpx.MockTransport(lambda _: pytest.fail("No network mutation allowed"))
    )
    try:
        with pytest.raises(RuntimeError, match="only permits GET"):
            await client.request("POST", endpoint, payload={})
        assert client.read_count == 0
    finally:
        await client.close()
