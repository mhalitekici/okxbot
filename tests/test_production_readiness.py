import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import httpx
import pytest
from sqlalchemy import func, select

from okx_bot.domain import ExchangeError, RiskRejected, utcnow
from okx_bot.engine import TradingEngine
from okx_bot.models import BotEvent, CapitalSlot, DailyReport, Execution, TradeOrder
from okx_bot.reporting import day_bounds
from okx_bot.telegram.admin import AdminBot
from tests.fakes import text, update
from tests.test_engine import message
from tests.test_risk import build


async def equity_cycle(engine, exchange, equity):
    exchange.equity = exchange.available = D(equity)
    return await engine.allocator.cycle(await exchange.balance(), verified_flat=True, force=True)


def signal(engine, symbol, number):
    return message(engine, number, text().replace("BTC", symbol).replace("%10", "%5.78"))


@pytest.mark.parametrize("equity,expected", [(1500, 500), (900, 300), (3000, 1000)])
async def test_fixed_cycle_budget_survives_available_margin_changes(
    engine, exchange, equity, expected
):
    cycle = await equity_cycle(engine, exchange, equity)
    assert cycle.slot_target_margin == expected
    exchange.available = D(equity) / 2
    assert (
        await engine.allocator.cycle(await exchange.balance(), verified_flat=False)
    ).slot_target_margin == expected
    restarted = TradingEngine(engine.settings, engine.db, exchange)
    assert D((await restarted.allocator.summary())["slot_target_margin"]) == expected


async def test_three_slots_keep_target_reuse_and_refresh_only_when_all_flat(engine, exchange):
    await equity_cycle(engine, exchange, 1500)
    exchange.available_instruments.update({"ETH-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP"})
    for number, symbol in enumerate(("BTC", "ETH", "SOL"), 1):
        assert await engine.ingest(signal(engine, symbol, number)) == "open"
        allocation = await engine.allocator.summary()
        assert D(allocation["slot_target_margin"]) == 500 and allocation["occupied"] == number
    assert await engine.ingest(signal(engine, "XRP", 4)) == "rejected"
    exchange.finish("tp", "BTC-USDT-SWAP")
    await engine.reconcile()
    assert (await engine.allocator.summary())["occupied"] == 2
    assert await engine.ingest(signal(engine, "XRP", 5)) == "open"
    assert D((await engine.allocator.summary())["slot_target_margin"]) == 500
    for inst in ("ETH-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP"):
        exchange.finish("tp", inst)
    exchange.equity = exchange.available = D(1650)
    await engine.reconcile()
    allocation = await engine.allocator.summary()
    assert D(allocation["slot_target_margin"]) == 550 and allocation["occupied"] == 0
    async with engine.db.sessions() as session:
        assert {o.target_margin for o in (await session.scalars(select(TradeOrder))).all()} == {
            D(500)
        }


async def test_simultaneous_signals_cannot_take_the_same_slot(engine, exchange):
    exchange.available_instruments.add("ETH-USDT-SWAP")
    outcomes = await asyncio.gather(
        engine.ingest(signal(engine, "BTC", 1)), engine.ingest(signal(engine, "ETH", 2))
    )
    assert outcomes == ["open", "open"]
    async with engine.db.sessions() as session:
        slots = list(
            (
                await session.scalars(select(CapitalSlot).where(CapitalSlot.order_id.is_not(None)))
            ).all()
        )
        assert len(slots) == len({s.number for s in slots}) == 2


async def test_free_slot_does_not_bypass_margin_reserves(engine, exchange):
    await equity_cycle(engine, exchange, 1500)
    exchange.available = D(500)
    assert await engine.ingest(signal(engine, "BTC", 1)) == "rejected"
    assert not exchange.submissions
    assert D((await engine.allocator.summary())["slot_target_margin"]) == 500


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_channel_stop_is_not_narrowed_by_old_per_trade_cap(settings, side):
    settings.max_account_risk_percent = D("0.01")
    plan = build(settings, side)
    assert plan.stop_loss == (94 if side == "LONG" else 106)
    assert plan.risk_amount > 100
    assert plan.position_margin <= D(1000) / 3


def test_portfolio_cap_rejects_instead_of_shrinking_slots(settings):
    settings.max_portfolio_risk_percent = D(1)
    with pytest.raises(RiskRejected) as exc:
        build(settings)
    assert exc.value.code == "PORTFOLIO_RISK_LIMIT"


def test_lot_rounding_never_exceeds_target_margin(settings):
    plan = build(settings, target_margin=D("123.4567"))
    assert plan.position_margin <= D("123.4567")
    assert plan.contracts % 1 == 0


async def test_unconfirmed_dedicated_account_never_automatically_closes(engine, exchange):
    engine.settings.dedicated_trading_account_confirmed = False
    exchange.no_protection = True
    assert await engine.ingest(message(engine)) == "emergency"
    assert exchange.exposure and not exchange.closed
    assert (await engine.state()).emergency


async def test_partial_exit_unrealized_report_stays_unknown_in_emergency(engine, exchange):
    assert await engine.ingest(message(engine)) == "open"
    exchange.finish("tp", fraction=D("0.5"))
    await engine.reconcile()
    assert (await engine.reporter.account_snapshot())["bot_unrealized"] is None


@pytest.mark.parametrize("extra", ["size", "order", "algo"])
async def test_manual_net_exposure_is_never_closed(engine, exchange, extra):
    assert await engine.ingest(message(engine)) == "open"
    if extra == "size":
        exchange.exposure[0]["pos"] = str(D(exchange.exposure[0]["pos"]) + 1)
    elif extra == "order":
        exchange.pending_orders.append({"instId": "BTC-USDT-SWAP", "clOrdId": "manual"})
    else:
        exchange.extra_algos.append(
            {"instId": "BTC-USDT-SWAP", "algoId": "manual", "algoClOrdId": "other"}
        )
    with pytest.raises(RiskRejected):
        await engine.close("BTC-USDT-SWAP")
    assert exchange.exposure and not exchange.closed


async def test_price_touch_and_disappearance_do_not_prove_closure(engine, exchange):
    assert await engine.ingest(message(engine)) == "open"
    exchange.market = D(106)
    await engine.reconcile()
    async with engine.db.sessions() as session:
        assert not await session.scalar(
            select(BotEvent.id).where(BotEvent.kind == "position_closed")
        )
    exchange.exposure = []
    exchange.protection.update(state="effective", actualSide="tp")
    await engine.reconcile()
    assert (await engine.allocator.summary())["occupied"] == 1
    async with engine.db.sessions() as session:
        assert (await session.scalar(select(TradeOrder))).status == "settling"
        assert not await session.scalar(
            select(BotEvent.id).where(BotEvent.kind == "position_closed")
        )


async def test_partial_exit_never_generates_a_full_close_or_touches_remainder(engine, exchange):
    assert await engine.ingest(message(engine)) == "open"
    exchange.finish("tp", fraction=D("0.5"))
    await engine.reconcile()
    assert exchange.exposure and not exchange.closed
    async with engine.db.sessions() as session:
        assert not await session.scalar(
            select(BotEvent.id).where(BotEvent.kind == "position_closed")
        )
    assert (await engine.state()).emergency


async def test_owned_fill_fees_funding_and_external_flows_are_separate(engine, exchange):
    assert await engine.ingest(message(engine)) == "open"
    exchange.bills_data = [
        {
            "billId": "fund1",
            "type": "8",
            "instId": "BTC-USDT-SWAP",
            "ccy": "USDT",
            "balChg": "-2",
            "ts": str(int(utcnow().timestamp() * 1000)),
        },
        {
            "billId": "deposit",
            "type": "1",
            "instId": "",
            "ccy": "USDT",
            "balChg": "5000",
            "ts": str(int(utcnow().timestamp() * 1000)),
        },
    ]
    exchange.finish("tp")
    exchange.fills_data.append(
        {
            **exchange.fills_data[0],
            "tradeId": "foreign",
            "ordId": "manual-eth",
            "instId": "ETH-USDT-SWAP",
            "fillPnl": "9999",
        }
    )
    await engine.reconcile()
    async with engine.db.sessions() as session:
        order = await session.scalar(select(TradeOrder))
        totals = await engine.accounting.totals(order.id)
        assert totals["net_pnl"] == totals["gross_pnl"] + totals["fees"] - 2
        assert totals["funding"] == -2
        assert order.close_cause == "tp"
        assert not await session.scalar(
            select(Execution.order_id).where(Execution.trade_id == "foreign")
        )
    pnl = (await engine.reporter.pnl())["today"]
    assert D(pnl["net_pnl"]) == totals["net_pnl"]
    assert pnl["wins"] == 1 and pnl["closed"] == 1 and D(pnl["win_rate"]) == 100


async def test_restart_recovers_missing_closure_once(engine, exchange):
    await engine.ingest(message(engine))
    exchange.finish("sl")
    restarted = TradingEngine(engine.settings, engine.db, exchange)
    await restarted.reconcile(startup=True)
    await restarted.reconcile()
    async with engine.db.sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(BotEvent).where(BotEvent.kind == "position_closed")
            )
            == 1
        )
        assert (await session.scalar(select(TradeOrder))).close_cause == "sl"


async def test_realized_daily_cap_latches_using_owned_fills(engine, exchange):
    await engine.ingest(message(engine))
    exchange.finish("sl")
    await engine.reconcile()
    assert "daily_loss_limit" in (await engine.state()).last_reconciliation_summary["blockers"]
    assert await engine.ingest(message(engine, 2)) == "rejected"


@pytest.mark.parametrize("dedicated,position_id", [(False, None), (True, "another-position")])
async def test_unknown_funding_is_not_reported_as_zero(engine, exchange, dedicated, position_id):
    engine.settings.dedicated_trading_account_confirmed = dedicated
    await engine.ingest(message(engine))
    exchange.bills_data = [
        {
            "billId": "unknownfund",
            "type": "8",
            "instId": "BTC-USDT-SWAP",
            "ccy": "USDT",
            "balChg": "-2",
            "posId": position_id,
            "ts": str(int(utcnow().timestamp() * 1000)),
        }
    ]
    await engine.accounting.sync()
    assert (await engine.reporter.pnl())["today"]["net_pnl"] is None


def test_istanbul_day_uses_utc_storage_bounds():
    begin, end = day_bounds(date(2026, 10, 21))
    assert begin == datetime(2026, 10, 20, 21, tzinfo=UTC)
    assert end == datetime(2026, 10, 21, 21, tzinfo=UTC)


async def test_daily_0005_schedule_and_restart_deduplication(engine):
    await engine.reporter.queue_daily(datetime(2026, 10, 20, 21, 4, tzinfo=UTC))
    async with engine.db.sessions() as session:
        assert not await session.get(DailyReport, date(2026, 10, 20))
    await engine.reporter.queue_daily(datetime(2026, 10, 20, 21, 5, tzinfo=UTC))
    await engine.reporter.queue_daily(datetime(2026, 10, 20, 21, 6, tzinfo=UTC))
    async with engine.db.sessions() as session:
        assert await session.get(DailyReport, date(2026, 10, 20))
        assert (
            await session.scalar(
                select(func.count())
                .select_from(BotEvent)
                .where(BotEvent.event_key == "daily:demo:2026-10-20")
            )
            == 1
        )


@pytest.mark.parametrize("command", ["/allocation", "/limits", "/pnl", "/daily", "/history"])
async def test_new_read_commands_and_unauthorized_access(engine, settings, command):
    bot = AdminBot(settings, engine)
    try:
        assert await bot.command(update(admin_id=999, command=command)) is None
        assert await bot.command(update(command=command))
    finally:
        await bot.http.aclose()


async def test_critical_settings_are_confirmed_persisted_and_audited(engine, settings, exchange):
    bot = AdminBot(settings, engine)
    try:
        preview = await bot.command(update(command="/setslots 4"))
        confirm = next(line for line in preview.splitlines() if line.startswith("/confirm "))
        assert (await engine.allocator.limits()).allocation_slots == 3
        assert "onaylandı" in await bot.command(update(command=confirm))
        assert (await engine.allocator.limits()).allocation_slots == 4
        assert (await engine.allocator.summary())["slot_count"] == 4
        restarted = TradingEngine(settings, engine.db, exchange)
        assert (await restarted.allocator.limits()).allocation_slots == 4
        assert "uygulanamadı" in await bot.command(update(command=confirm))
        async with engine.db.sessions() as session:
            assert await session.scalar(select(BotEvent).where(BotEvent.kind == "settings_changed"))
    finally:
        await bot.http.aclose()


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_daily_loss_percent", 0),
        ("max_portfolio_risk_percent", 100),
        ("minimum_free_margin", 0),
        ("allocation_slots", 0),
    ],
)
async def test_limits_cannot_be_removed(engine, field, value):
    with pytest.raises(RiskRejected):
        await engine.request_change(field, value, actor="admin")


async def test_rebalance_and_slot_changes_reject_open_inventory(engine):
    await engine.ingest(message(engine))
    for field, value in (("allocation_slots", 4), ("rebalance", None)):
        with pytest.raises(RiskRejected):
            await engine.request_change(field, value, actor="admin")


async def test_offline_okx_cannot_apply_settings_or_auto_resume(engine, exchange):
    preview = await engine.request_change("minimum_free_margin", "20", actor="admin")
    exchange.fail_preflight = True
    with pytest.raises(ExchangeError):
        await engine.reconcile()
    with pytest.raises(RiskRejected):
        await engine.confirm_change(preview["confirmation"], actor="admin")
    exchange.fail_preflight = False
    await engine.reconcile()
    assert (await engine.state()).emergency
    assert not (await engine.status())["trading_enabled"]


async def test_emergency_command_never_closes_or_cancels_positions(engine, settings, exchange):
    await engine.ingest(message(engine))
    bot = AdminBot(settings, engine)
    try:
        assert "kilidi etkin" in await bot.command(update(command="/emergency"))
        assert exchange.exposure and not exchange.closed and not exchange.canceled
    finally:
        await bot.http.aclose()


async def test_close_gateway_never_uses_whole_account_cancel(settings):
    from types import SimpleNamespace

    from okx_bot.exchange.client import OKXClient
    from okx_bot.exchange.gateway import OKXGateway

    def wire(request):
        import json

        payload = json.loads(request.content)
        assert request.url.path == "/api/v5/trade/order"
        assert payload["reduceOnly"] is True and payload["sz"] == "2"
        assert "autoCxl" not in payload
        return httpx.Response(200, json={"code": "0", "data": [{"ordId": "exit1"}]})

    client = OKXClient(settings, httpx.MockTransport(wire))
    try:
        await OKXGateway(client).close_owned(
            SimpleNamespace(inst_id="BTC-USDT-SWAP", pos_side="net", direction="long"),
            D(2),
            "ownedexit",
        )
    finally:
        await client.close()


async def test_uncertain_telegram_send_is_not_blindly_retried_after_restart(engine, settings):
    from unittest.mock import AsyncMock

    bot = AdminBot(settings, engine)
    bot.send = AsyncMock(side_effect=httpx.ReadTimeout("lost acknowledgement"))
    await engine.event("paused")
    try:
        with pytest.raises(httpx.ReadTimeout):
            await bot.deliver_pending()
        restarted = AdminBot(settings, engine)
        restarted.send = AsyncMock()
        try:
            await restarted.deliver_pending()
            restarted.send.assert_not_awaited()
            assert (await engine.status())["uncertain_notifications"] == 1
        finally:
            await restarted.http.aclose()
    finally:
        await bot.http.aclose()


async def test_database_bound_to_account_rejects_a_different_account(engine, exchange):
    original = exchange.account_config

    async def other():
        return {**await original(), "uid": "another-account"}

    exchange.account_config = other
    with pytest.raises(RiskRejected) as error:
        await engine.reconcile()
    assert error.value.code == "ACCOUNT_MISMATCH"
    assert (await engine.state()).emergency
    assert not exchange.submissions


async def test_settings_confirmation_is_actor_bound_and_expires(engine):
    preview = await engine.request_change("minimum_free_margin", 20, actor="one")
    with pytest.raises(RiskRejected):
        await engine.confirm_change(preview["confirmation"], actor="two")
    engine._change_approvals["one"]["expires"] = utcnow() - timedelta(seconds=1)
    with pytest.raises(RiskRejected):
        await engine.confirm_change(preview["confirmation"], actor="one")
    assert (await engine.allocator.limits()).minimum_free_margin == 10


async def test_close_does_not_claim_success_without_confirmed_exit_fills(engine, exchange):
    assert await engine.ingest(message(engine)) == "open"
    original = exchange.close_owned

    async def missing_fills(order, quantity, client_id):
        result = await original(order, quantity, client_id)
        exchange.fills_data = [f for f in exchange.fills_data if f["ordId"] != result["ordId"]]
        return result

    exchange.close_owned = missing_fills
    with pytest.raises(RiskRejected) as exc:
        await engine.close("BTC-USDT-SWAP")
    assert exc.value.code == "CLOSURE_UNCONFIRMED"
    assert (await engine.allocator.summary())["occupied"] == 1
    async with engine.db.sessions() as session:
        assert not await session.scalar(
            select(BotEvent.id).where(BotEvent.kind == "position_closed")
        )


async def test_setup_failure_before_entry_releases_the_slot_without_retry(engine, exchange):
    from unittest.mock import AsyncMock

    exchange.set_leverage = AsyncMock(side_effect=ExchangeError("HTTP_401", ambiguous=True))
    assert await engine.ingest(message(engine)) == "failed"
    assert not exchange.submissions
    assert (await engine.allocator.summary())["occupied"] == 0
    assert (await engine.state()).emergency


async def test_repeated_emergency_cause_does_not_spam_the_persistent_outbox(engine):
    await engine.trip("first cause")
    await engine.trip("another cause")
    await engine.trip("another cause")
    async with engine.db.sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(BotEvent).where(BotEvent.kind == "emergency")
            )
            == 2
        )


async def test_legacy_missing_fill_times_do_not_fabricate_performance_counts(engine):
    assert await engine.ingest(message(engine)) == "open"
    async with engine.db.sessions.begin() as session:
        order = await session.scalar(select(TradeOrder))
        order.status, order.closed_at = "closed", None
    report = await engine.reporter.period()
    assert report["opened"] is None and report["closed"] is None
    assert report["wins"] is None and report["losses"] is None
    assert report["win_rate"] is None and report["net_pnl"] is None
