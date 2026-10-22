import json
import logging
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from okx_bot.diagnostics import failure_details
from okx_bot.domain import ExchangeError, RiskRejected, utcnow
from okx_bot.engine import TradingEngine
from okx_bot.logging import JSONFormatter
from okx_bot.models import BotEvent, Position, RuntimeState
from tests.test_engine import message


async def test_account_mode_failure_is_recorded_and_recovery_requires_approval(engine, exchange):
    original = exchange.account_config

    async def wrong_mode():
        raise RiskRejected(
            "Account must use Futures mode",
            code="UNSUPPORTED_ACCOUNT_MODE",
            context={"account_level": "1", "expected_account_level": "2"},
        )

    exchange.account_config = wrong_mode
    with pytest.raises(RiskRejected):
        await engine.reconcile()
    state = await engine.state()
    original_reason = state.reason
    assert state.emergency and state.paused and not state.last_reconciliation_ok
    assert state.last_reconciliation_error["stage"] == "account_config"
    assert state.last_reconciliation_error["code"] == "UNSUPPORTED_ACCOUNT_MODE"
    assert state.last_reconciliation_error["account_level"] == "1"
    exchange.account_config = original
    await engine.reconcile()
    state = await engine.state()
    assert state.reason == original_reason
    assert state.last_reconciliation_ok and state.reconciliation_recovered_at
    assert state.emergency and state.paused and engine.healthy
    status = await engine.status()
    assert status["requires_admin_approval"] and not status["trading_enabled"]
    assert status["reconciliation"]["last_error"]["code"] == "UNSUPPORTED_ACCOUNT_MODE"
    assert not exchange.submissions


async def test_legacy_database_emergency_survives_success_and_restart(engine, exchange):
    async with engine.db.sessions.begin() as session:
        state = await session.get(RuntimeState, 1)
        state.paused = state.emergency = True
        state.reason = "Reconciliation failed; new orders disabled"
    restarted = TradingEngine(engine.settings, engine.db, exchange)
    await restarted.reconcile(startup=True)
    state = await restarted.state()
    assert state.emergency and state.paused
    assert state.reason == "Reconciliation failed; new orders disabled"
    assert state.last_reconciliation_ok and restarted.healthy


async def test_repeated_failure_does_not_flood_events_or_hide_first_incident(engine, exchange):
    exchange.fail_preflight = True
    for _ in range(2):
        with pytest.raises(ExchangeError):
            await engine.reconcile()
    original_reason = (await engine.state()).reason
    await engine.trip("Required background task stopped: example")
    assert (await engine.state()).reason == original_reason
    async with engine.db.sessions() as session:
        events = (
            await session.scalars(select(BotEvent).where(BotEvent.kind == "reconciliation_failed"))
        ).all()
    assert len(events) == 1


async def test_database_failure_latches_after_database_recovers(engine, monkeypatch):
    original_sessions = engine.db.sessions

    class Unavailable:
        def __call__(self):
            raise OperationalError("SECRET SQL", {}, Exception("SECRET PASSWORD"))

        def begin(self):
            return self()

    monkeypatch.setattr(engine.db, "sessions", Unavailable())
    with pytest.raises(OperationalError):
        await engine.reconcile()
    assert not engine.healthy and engine._unpersisted_failure
    assert engine.last_error["stage"] == "initialize_limits"
    assert engine.last_error["code"] == "DATABASE_ERROR"
    monkeypatch.setattr(engine.db, "sessions", original_sessions)
    await engine.reconcile()
    state = await engine.state()
    assert engine.healthy and state.emergency and state.paused
    assert state.last_reconciliation_error["code"] == "DATABASE_ERROR"
    assert state.last_reconciliation_ok


async def test_malformed_position_failure_has_stage_and_missing_field(engine, exchange):
    exchange.exposure = [{"pos": "1"}]
    with pytest.raises(KeyError):
        await engine.reconcile()
    error = (await engine.state()).last_reconciliation_error
    assert error["stage"] == "inventory_validation"
    assert error["missing_field"] == "instId"
    assert not engine.healthy


async def test_invalid_daily_exchange_data_is_not_swallowed_as_daily_limit(engine, exchange):
    async def invalid(begin, end):
        raise RiskRejected("Missing or invalid exchange numeric data")

    engine.accounting.daily_realized_loss = invalid
    with pytest.raises(RiskRejected):
        await engine.reconcile()
    state = await engine.state()
    assert state.last_reconciliation_error["stage"] == "daily_risk"
    assert not state.last_reconciliation_ok and state.emergency


async def test_real_daily_limit_is_a_safety_blocker_not_a_sync_failure(engine, exchange):
    engine.accounting.daily_realized_loss = AsyncMock(return_value=30)
    await engine.reconcile()
    state = await engine.state()
    assert engine.healthy and state.last_reconciliation_ok
    assert "daily_loss_limit" in state.last_reconciliation_summary["blockers"]
    assert not (await engine.status())["trading_enabled"]
    with pytest.raises(RiskRejected, match="Daily loss"):
        await engine.request_resume(actor="admin")


async def test_resume_request_does_not_unlock_or_mutate_exchange(engine, exchange):
    await engine.trip("Earlier failure")
    result = await engine.request_resume(actor="admin")
    assert result["status"] == "confirmation_required"
    assert (await engine.state()).emergency and (await engine.state()).paused
    assert not exchange.submissions and not exchange.closed and not exchange.canceled
    async with engine.db.sessions() as session:
        events = (await session.scalars(select(BotEvent))).all()
    assert result["confirmation"] not in json.dumps([e.details for e in events])
    await engine.resume(confirmation=result["confirmation"], actor="admin")
    state = await engine.state()
    assert not state.emergency and not state.paused
    assert (await engine.status())["trading_enabled"]
    async with engine.db.sessions() as session:
        event = await session.scalar(select(BotEvent).where(BotEvent.kind == "resumed"))
        assert event.details["actor"] == "admin"
        assert event.details["explicit_confirmation"] is True
        assert event.details["previous_reason"] == "Earlier failure"
    assert not exchange.submissions
    with pytest.raises(RiskRejected, match="confirmation"):
        await engine.resume(confirmation=result["confirmation"], actor="admin")


@pytest.mark.parametrize(
    "scenario", ["wrong_actor", "wrong_token", "expired", "restart", "new_pause", "new_failure"]
)
async def test_resume_confirmation_is_bound_single_use_and_short_lived(engine, exchange, scenario):
    await engine.trip("Earlier failure")
    result = await engine.request_resume(actor="admin")
    token, actor = result["confirmation"], "admin"
    target = engine
    if scenario == "wrong_actor":
        actor = "someone_else"
    elif scenario == "wrong_token":
        token = "not-the-confirmation-token"
    elif scenario == "expired":
        engine._resume_approvals[actor]["expires"] = utcnow() - timedelta(seconds=1)
    elif scenario == "restart":
        target = TradingEngine(engine.settings, engine.db, exchange)
        await target.reconcile(startup=True)
    elif scenario == "new_pause":
        await engine.pause()
    else:
        exchange.fail_preflight = True
        with pytest.raises(ExchangeError):
            await engine.reconcile()
        exchange.fail_preflight = False
        await engine.reconcile()
    with pytest.raises(RiskRejected):
        await target.resume(confirmation=token, actor=actor)
    assert (await engine.state()).paused and (await engine.state()).emergency


@pytest.mark.parametrize(
    "change",
    [
        "unknown_position",
        "pending_order",
        "unknown_algo",
        "daily_loss",
        "account_mode",
        "db_position",
        "protection",
    ],
)
async def test_confirmation_rechecks_safety_and_never_repairs_exposure(engine, exchange, change):
    if change in {"db_position", "protection"}:
        assert await engine.ingest(message(engine)) == "open"
    await engine.pause()
    result = await engine.request_resume(actor="admin")
    if change == "unknown_position":
        exchange.exposure.append({"instId": "ETH-USDT-SWAP", "posSide": "net", "pos": "1"})
    elif change == "pending_order":
        exchange.pending_orders.append({"instId": "BTC-USDT-SWAP", "clOrdId": "manual"})
    elif change == "unknown_algo":
        exchange.extra_algos.append({"instId": "BTC-USDT-SWAP", "algoId": "manual"})
    elif change == "daily_loss":
        engine.accounting.daily_realized_loss = AsyncMock(return_value=30)
    elif change == "account_mode":

        async def invalid():
            raise RiskRejected("Unsupported account mode", code="UNSUPPORTED_ACCOUNT_MODE")

        exchange.account_config = invalid
    elif change == "db_position":
        async with engine.db.sessions.begin() as session:
            position = await session.scalar(select(Position))
            position.contracts = 99
    else:
        exchange.protection["reduceOnly"] = "false"
    sent_before = len(exchange.submissions)
    with pytest.raises(RiskRejected):
        await engine.resume(confirmation=result["confirmation"], actor="admin")
    assert (await engine.state()).paused
    assert len(exchange.submissions) == sent_before
    assert not exchange.closed and not exchange.canceled


async def test_even_safe_snapshot_change_requires_new_confirmation(engine, exchange):
    await engine.pause()
    result = await engine.request_resume(actor="admin")
    exchange.equity -= 1  # A changed account snapshot requires another explicit approval.
    with pytest.raises(RiskRejected, match="snapshot changed"):
        await engine.resume(confirmation=result["confirmation"], actor="admin")
    assert (await engine.state()).paused


async def test_resume_checks_database_revision_again_before_commit(engine, monkeypatch):
    await engine.pause()
    result = await engine.request_resume(actor="admin")
    original = engine._resume_snapshot

    async def changed_during_validation():
        snapshot = await original()
        await engine.trip("Concurrent safety event")
        return snapshot

    monkeypatch.setattr(engine, "_resume_snapshot", changed_during_validation)
    with pytest.raises(RiskRejected, match="Safety state changed"):
        await engine.resume(confirmation=result["confirmation"], actor="admin")
    assert (await engine.state()).emergency


async def test_confirmation_expiry_is_rechecked_after_slow_validation(engine, monkeypatch):
    await engine.pause()
    result = await engine.request_resume(actor="admin")
    approval = engine._resume_approvals["admin"]
    original = engine._resume_snapshot

    async def slow_validation():
        snapshot = await original()
        approval["expires"] = utcnow() - timedelta(seconds=1)
        return snapshot

    monkeypatch.setattr(engine, "_resume_snapshot", slow_validation)
    with pytest.raises(RiskRejected, match="expired"):
        await engine.resume(confirmation=result["confirmation"], actor="admin")
    assert (await engine.state()).paused


async def test_safety_change_during_preview_cannot_issue_confirmation(engine, monkeypatch):
    await engine.pause()
    original = engine._resume_snapshot

    async def concurrent_event():
        snapshot = await original()
        await engine.trip("Concurrent safety incident")
        return snapshot

    monkeypatch.setattr(engine, "_resume_snapshot", concurrent_event)
    with pytest.raises(RiskRejected, match="Safety state changed"):
        await engine.request_resume(actor="admin")
    assert not engine._resume_approvals
    assert (await engine.state()).emergency


def test_diagnostics_and_json_logging_keep_safe_context_without_secrets():
    try:
        raise RuntimeError("postgresql://user:SECRET_PASSWORD@host/db?token=SECRET_TOKEN")
    except RuntimeError as exc:
        details = failure_details(exc, "database_orders")
        record = logging.LogRecord(
            "test",
            logging.ERROR,
            __file__,
            1,
            "reconciliation_failed",
            (),
            (type(exc), exc, exc.__traceback__),
        )
        record.context = details
        output = JSONFormatter().format(record)
    assert "SECRET" not in output
    value = json.loads(output)
    assert value["context"]["stage"] == "database_orders"
    assert (
        value["context"]["frames"][-1]["function"]
        == "test_diagnostics_and_json_logging_keep_safe_context_without_secrets"
    )


def test_exchange_error_diagnostics_include_endpoint_code_and_status_only():
    error = ExchangeError(
        "50011", endpoint="/api/v5/account/positions", method="GET", http_status=429
    )
    details = failure_details(error, "positions")
    assert details["code"] == "50011" and details["http_status"] == 429
    assert details["endpoint"] == "/api/v5/account/positions"
