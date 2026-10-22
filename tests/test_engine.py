import asyncio
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from okx_bot.domain import Message, RiskRejected, utcnow
from okx_bot.engine import TradingEngine
from okx_bot.models import BotEvent, Execution, Position, RuntimeState, TradeOrder
from tests.fakes import text


def message(engine, message_id=1, content=None, age=0, edited=False):
    return Message(
        engine.settings.telegram_channel_id,
        message_id,
        content or text(),
        utcnow() - timedelta(seconds=age),
        edited,
        topic_id=engine.settings.telegram_topic_id,
    )


@pytest.mark.parametrize(
    "direction,pos_mode",
    [
        ("LONG", "net_mode"),
        ("SHORT", "net_mode"),
        ("LONG", "long_short_mode"),
        ("SHORT", "long_short_mode"),
    ],
)
async def test_demo_entry_and_restart_reconciliation(engine, exchange, direction, pos_mode):
    exchange.mode = pos_mode
    assert await engine.ingest(message(engine, content=text(direction))) == "open"
    payload = exchange.submissions[0]
    assert payload["tdMode"] == "isolated"
    assert payload["ordType"] == "ioc"
    assert payload["posSide"] == ("net" if pos_mode == "net_mode" else direction.lower())
    assert exchange.leverage_calls == [("BTC-USDT-SWAP", payload["posSide"], "10", "isolated")]
    assert payload["attachAlgoOrds"][0]["slTriggerPxType"] == "mark"
    restarted = TradingEngine(engine.settings, engine.db, exchange)
    await restarted.reconcile(startup=True)
    assert restarted.healthy
    assert len(exchange.submissions) == 1
    async with engine.db.sessions() as session:
        position = await session.scalar(select(Position))
        assert position.status == "open"


async def test_duplicate_concurrent_delivery_and_edits_never_reopen(engine, exchange):
    item = message(engine)
    results = await asyncio.gather(engine.ingest(item), engine.ingest(item))
    assert set(results) == {"open", "duplicate"}
    assert (
        await engine.ingest(message(engine, content=text() + "\nTP: 107", edited=True))
        == "duplicate"
    )
    assert len(exchange.submissions) == 1


async def test_edit_can_complete_unsubmitted_review(engine, exchange):
    assert (
        await engine.ingest(message(engine, content=text().replace("LONG", ""))) == "needs_review"
    )
    assert await engine.ingest(message(engine, edited=True)) == "open"
    assert len(exchange.submissions) == 1


async def test_edit_does_not_refresh_old_signal(engine, exchange):
    assert (
        await engine.ingest(message(engine, content=text().replace("LONG", ""), age=120))
        == "needs_review"
    )
    assert await engine.ingest(message(engine, age=120, edited=True)) == "rejected"
    assert not exchange.submissions


async def test_wrong_coin_and_stale_signal(engine, exchange):
    assert (
        await engine.ingest(message(engine, content=text().replace("BTC", "NOTLISTED")))
        == "manual_required"
    )
    assert await engine.ingest(message(engine, message_id=2, age=61)) == "rejected"
    assert await engine.ingest(message(engine, message_id=3, age=-30)) == "rejected"
    assert not exchange.submissions


async def test_cold_start_ignores_old_channel_history(engine, exchange):
    async with engine.db.sessions.begin() as session:
        state = await session.get(RuntimeState, 1)
        state.channel_started_at = utcnow()
    assert await engine.ingest(message(engine, age=5)) == "ignored_history"
    assert not exchange.submissions


async def test_order_rejection_is_recorded(engine, exchange):
    exchange.rejection = True
    assert await engine.ingest(message(engine)) == "rejected"
    assert not exchange.closed
    async with engine.db.sessions() as session:
        assert (await session.scalar(select(TradeOrder))).status == "rejected"


async def test_edit_cannot_retry_an_exchange_rejected_order(engine, exchange):
    exchange.rejection = True
    assert await engine.ingest(message(engine)) == "rejected"
    exchange.rejection = False
    edited = message(engine, content=text() + "\nTP: 107", edited=True)
    assert await engine.ingest(edited) == "duplicate"
    assert len(exchange.submissions) == 1


async def test_post_fill_deviation_is_checked_again_during_reconciliation(engine, exchange):
    assert await engine.ingest(message(engine)) == "open"
    exchange.exposure[0]["avgPx"] = "101"
    await engine.reconcile()
    assert not exchange.exposure
    assert (await engine.state()).emergency


async def test_exchange_api_error_blocks_further_orders(engine, exchange):
    exchange.fail_preflight = True
    assert await engine.ingest(message(engine)) == "failed"
    assert (await engine.state()).emergency
    assert not exchange.submissions


@pytest.mark.parametrize(
    "failure", ["no_protection", "partial", "bad_liquidation", "disconnect_after_accept"]
)
async def test_unsafe_or_uncertain_execution_flattens_and_pauses(engine, exchange, failure):
    setattr(exchange, failure, True)
    assert await engine.ingest(message(engine)) == "emergency"
    assert not exchange.exposure
    assert exchange.closed == [("BTC-USDT-SWAP", "net")]
    assert (await engine.state()).emergency
    assert await engine.ingest(message(engine)) == "duplicate"
    assert len(exchange.submissions) == 1


async def test_failed_close_requires_manual_intervention(engine, exchange):
    exchange.no_protection = exchange.close_fails = True
    assert await engine.ingest(message(engine)) == "emergency"
    assert exchange.exposure
    async with engine.db.sessions() as session:
        assert (await session.scalar(select(TradeOrder))).status == "emergency"
        assert await session.scalar(
            select(BotEvent).where(BotEvent.kind == "manual_intervention_required")
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("ordType", "conditional"),
        ("reduceOnly", "false"),
        ("slTriggerPxType", "last"),
        ("slTriggerPx", "1"),
        ("sz", "99"),
    ],
)
async def test_changed_protection_is_never_accepted(engine, exchange, field, value):
    assert await engine.ingest(message(engine)) == "open"
    exchange.protection[field] = value
    await engine.reconcile()
    assert not exchange.exposure
    assert (await engine.state()).emergency


async def test_live_partial_fill_is_canceled_before_close(engine, exchange):
    original = exchange.place_entry
    exchange.partial = True

    async def place(payload):
        ack = await original(payload)
        exchange.row["state"] = "partially_filled"
        return ack

    exchange.place_entry = place
    assert await engine.ingest(message(engine)) == "emergency"
    assert len(exchange.canceled) == 1
    assert not exchange.exposure


async def test_unconfirmed_cancel_latches_emergency(engine, exchange):
    original = exchange.place_entry

    async def place(payload):
        ack = await original(payload)
        exchange.row["state"] = "live"
        return ack

    async def unconfirmed_cancel(inst_id, client_id):
        exchange.canceled.append(client_id)

    exchange.place_entry = place
    exchange.cancel_order = unconfirmed_cancel
    assert await engine.ingest(message(engine)) == "emergency"
    assert (await engine.state()).emergency
    assert exchange.exposure
    assert not exchange.closed


async def test_no_fill_does_not_create_a_position(engine, exchange):
    exchange.no_fill = True
    assert await engine.ingest(message(engine)) == "canceled"
    async with engine.db.sessions() as session:
        assert await session.scalar(select(Position)) is None


async def test_unknown_submission_is_not_retried_after_restart(engine, exchange):
    exchange.disconnect_before_accept = True
    assert await engine.ingest(message(engine)) == "emergency"
    restarted = TradingEngine(engine.settings, engine.db, exchange)
    await restarted.reconcile(startup=True)
    assert (await restarted.state()).emergency
    with pytest.raises(RiskRejected, match="unresolved"):
        await restarted.request_resume(actor="test_admin")
    assert len(exchange.submissions) == 1


async def test_restart_during_submission_closes_exposure(engine, exchange):
    assert await engine.ingest(message(engine)) == "open"
    async with engine.db.sessions.begin() as session:
        order = await session.scalar(select(TradeOrder))
        order.status = "submitting"
    restarted = TradingEngine(engine.settings, engine.db, exchange)
    await restarted.reconcile(startup=True)
    assert not exchange.exposure
    assert len(exchange.submissions) == 1
    assert (await restarted.state()).emergency


async def test_restart_finds_missing_protection_and_closes(engine, exchange):
    assert await engine.ingest(message(engine)) == "open"
    exchange.protection = None
    restarted = TradingEngine(engine.settings, engine.db, exchange)
    await restarted.reconcile(startup=True)
    assert not exchange.exposure
    assert (await restarted.state()).emergency


@pytest.mark.parametrize("cause", ["tp", "sl"])
async def test_protective_exit_syncs_position_and_notifies(engine, exchange, cause):
    assert await engine.ingest(message(engine)) == "open"
    exchange.finish(cause)
    await engine.reconcile()
    async with engine.db.sessions() as session:
        assert (await session.scalar(select(Position))).status == "closed"
        event = await session.scalar(select(BotEvent).where(BotEvent.kind == "position_closed"))
        assert event.details["cause"] == cause


async def test_daily_limit_persists_even_after_balance_recovers(engine, exchange):
    engine.accounting.daily_realized_loss = AsyncMock(return_value=Decimal(30))
    assert await engine.ingest(message(engine)) == "rejected"
    restarted = TradingEngine(engine.settings, engine.db, exchange)
    await restarted.reconcile(startup=True)
    assert await restarted.ingest(message(engine, message_id=2)) == "rejected"
    assert not exchange.submissions


async def test_pause_persists_and_admin_close_works_while_paused(engine, exchange):
    assert await engine.ingest(message(engine)) == "open"
    await engine.pause()
    assert (await engine.state()).paused
    assert await engine.ingest(message(engine, message_id=2)) == "rejected"
    await engine.close("BTC-USDT-SWAP")
    assert not exchange.exposure
    approval = await engine.request_resume(actor="test_admin")
    await engine.resume(confirmation=approval["confirmation"], actor="test_admin")
    assert not (await engine.state()).paused


async def test_unmanaged_positions_pause_account(engine, exchange):
    exchange.exposure = [{"instId": "ETH-USDT-SWAP", "posSide": "net", "pos": "1"}]
    await engine.reconcile(startup=True)
    assert (await engine.state()).emergency
    with pytest.raises(RiskRejected, match="Unmanaged"):
        await engine.request_resume(actor="test_admin")
    assert not exchange.closed


async def test_fill_ingestion_is_idempotent(engine, exchange):
    assert await engine.ingest(message(engine)) == "open"
    exchange.fills_data = [
        {
            "instId": "BTC-USDT-SWAP",
            "tradeId": "5",
            "ordId": "123",
            "fillPx": "100",
            "fillSz": "1",
            "fillPnl": "0",
            "fee": "-0.05",
            "feeCcy": "USDT",
            "ts": str(int(utcnow().timestamp() * 1000)),
        }
    ]
    await engine.reconcile()
    await engine.reconcile()
    async with engine.db.sessions() as session:
        rows = (await session.scalars(select(Execution))).all()
        assert len(rows) == 1
    assert (await engine.stats())["executions"] == 0  # Unknown account fills are excluded.
