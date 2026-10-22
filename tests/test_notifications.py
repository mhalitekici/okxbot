from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from okx_bot.domain import Message, utcnow
from okx_bot.models import BotEvent, TelegramSignal, TradeOrder
from okx_bot.parser import ChannelSignalParser
from okx_bot.telegram.admin import AdminBot
from okx_bot.telegram.formatting import format_notification, reason_tr
from tests.fakes import text, update
from tests.test_mz_parser import EXAMPLE


@pytest.fixture
async def bot(settings, engine, monkeypatch):
    bot = AdminBot(settings, engine, SimpleNamespace(status=lambda: {"ready": True}))
    bot.send = AsyncMock()

    async def no_delay(_):
        pass

    monkeypatch.setattr("okx_bot.telegram.admin.asyncio.sleep", no_delay)
    yield bot
    await bot.http.aclose()


def message(engine, content=None, identifier=1, edited=False):
    return Message(
        engine.settings.telegram_channel_id,
        identifier,
        content or text(),
        utcnow(),
        edited,
        topic_id=engine.settings.telegram_topic_id,
    )


async def events(engine, kind):
    async with engine.db.sessions() as session:
        return (await session.scalars(select(BotEvent).where(BotEvent.kind == kind))).all()


def test_parsed_mz_notification_has_turkish_text_and_exact_calculation():
    signal = ChannelSignalParser(-100123, 23576).parse(
        Message(-100123, 1, EXAMPLE, utcnow(), topic_id=23576)
    )
    rendered = format_notification("signal_parsed", signal.model_dump(mode="json"))
    for expected in [
        "YENİ SİNYAL ALINDI",
        "CT/USDT",
        "LONG adayı",
        "0.3604 USDT",
        "%17,33",
        "10x",
        "İzole marjin",
        "0.397874392 USDT",
        "0.322925608 USDT",
        "kanalına özel",
        "risk kontrolüne alındı",
    ]:
        assert expected in rendered
    assert "signal_parsed" not in rendered and "notes" not in rendered


@pytest.mark.parametrize(
    "kind,title",
    [
        ("signal_received", "YENİ SİNYAL ALINDI"),
        ("signal_parsed", "YENİ SİNYAL ALINDI"),
        ("needs_review", "SİNYAL İNCELEME GEREKTİRİYOR"),
        ("risk_rejected", "SİNYAL REDDEDİLDİ"),
        ("failed", "DEMO İŞLEM AÇILAMADI"),
        ("tpsl_placed", "TP/SL EMİRLERİ DOĞRULANDI"),
        ("tp_executed", "KÂR ALMA (TP) GERÇEKLEŞTİ"),
        ("sl_executed", "ZARAR DURDURMA (SL) GERÇEKLEŞTİ"),
        ("position_closed", "POZİSYON KAPANDI"),
        ("okx_connection_error", "OKX BAĞLANTI HATASI"),
        ("telegram_disconnected", "TELEGRAM BAĞLANTI HATASI"),
        ("daily_risk_block", "GÜNLÜK ZARAR LİMİTİ"),
        ("emergency", "ACİL DURUM KİLİDİ AKTİF"),
        ("paused", "BOT DURDURULDU"),
        ("resumed", "BOT YENİDEN BAŞLATILDI"),
        ("reconciliation_failed", "HESAP KONTROLÜ BAŞARISIZ"),
        ("reconciliation_recovered", "HESAP BAĞLANTISI DÜZELDİ"),
    ],
)
def test_central_event_templates_do_not_leak_technical_errors(kind, title):
    result = format_notification(kind, {"reason": "SECRET English provider error"})
    assert title in result
    assert "SECRET" not in result and "provider" not in result


async def test_mz_sample_is_stored_verbatim_and_rejected_without_submitting(engine, exchange, bot):
    engine.parser = ChannelSignalParser(
        engine.settings.telegram_channel_id, engine.settings.telegram_topic_id
    )
    instrument = await exchange.instrument("BTC-USDT-SWAP")

    async def ct_instrument(inst_id):
        from dataclasses import replace
        from decimal import Decimal

        assert inst_id == "CT-USDT-SWAP"
        return replace(instrument, inst_id=inst_id, family="CT-USDT", tick=Decimal("0.0001"))

    exchange.instrument = ct_instrument
    from decimal import Decimal

    exchange.market = Decimal("0.3604")
    item = message(engine, EXAMPLE)
    assert await engine.ingest(item) == "rejected"
    assert await engine.ingest(item) == "duplicate"
    assert not exchange.submissions and not exchange.leverage_calls
    async with engine.db.sessions() as session:
        saved = await session.scalar(select(TelegramSignal))
        assert saved.original_text == EXAMPLE
        assert saved.parsed["interpretation_rule"] == "mztradingbot_support_positive_long_v1"
    await bot.deliver_pending()
    assert bot.send.await_count == 2
    summary, rejection = [call.args[0] for call in bot.send.await_args_list]
    assert "LONG adayı" in summary
    assert "CT/USDT" in rejection and "likidasyon fiyatının ötesinde" in rejection
    assert "İşlem açılmadı" in rejection
    assert not await events(engine, "order_opened")


async def test_verified_open_uses_exchange_fill_quantity_and_submitted_tpsl(engine, exchange, bot):
    original = exchange.place_entry

    async def fill_at_real_price(payload):
        ack = await original(payload)
        exchange.row["avgPx"] = "100.2"
        exchange.exposure[0]["avgPx"] = "100.2"
        return ack

    exchange.place_entry = fill_at_real_price
    item = message(engine)
    assert await engine.ingest(item) == "open"
    assert await engine.ingest(item) == "duplicate"
    opened = await events(engine, "order_opened")
    assert len(opened) == 1 and len(await events(engine, "tpsl_placed")) == 1
    attached = exchange.submissions[0]["attachAlgoOrds"][0]
    assert opened[0].details["fill_price"] == "100.2"
    assert opened[0].details["contracts"] == exchange.row["accFillSz"]
    assert opened[0].details["take_profit"] == exchange.protection["tpTriggerPx"]
    assert opened[0].details["stop_loss"] == exchange.protection["slTriggerPx"]
    assert opened[0].details["take_profit"] != attached["tpTriggerPx"]
    await bot.deliver_pending()
    messages = [call.args[0] for call in bot.send.await_args_list]
    assert len(messages) == 3  # Signal, submission acknowledgement and protected open.
    result = messages[-1]
    assert "DEMO İŞLEM AÇILDI" in result and "100.2 USDT" in result
    assert "106.2 USDT" in result and "94.2 USDT" in result
    assert f"{exchange.row['accFillSz']} kontrat" in result
    assert "Koruyucu emirler doğrulandı" in result
    await engine.reconcile()
    await bot.deliver_pending()
    assert bot.send.await_count == 3
    restarted = AdminBot(engine.settings, engine)
    restarted.send = AsyncMock()
    try:
        await restarted.deliver_pending()
        restarted.send.assert_not_awaited()
    finally:
        await restarted.http.aclose()


@pytest.mark.parametrize(
    "failure", ["partial", "no_protection", "no_fill", "rejection", "bad_liquidation"]
)
async def test_unconfirmed_or_unfilled_order_never_claims_open(engine, exchange, bot, failure):
    setattr(exchange, failure, True)
    await engine.ingest(message(engine))
    await bot.deliver_pending()
    assert not await events(engine, "order_opened")
    assert all("DEMO İŞLEM AÇILDI" not in c.args[0] for c in bot.send.await_args_list)


async def test_notification_failure_retries_outbox_without_changing_open_order(
    engine, exchange, bot
):
    assert await engine.ingest(message(engine)) == "open"
    bot.send.side_effect = httpx.ConnectError("SECRET token in provider error")
    with pytest.raises(httpx.ConnectError):
        await bot.deliver_pending()
    async with engine.db.sessions() as session:
        order = await session.scalar(select(TradeOrder))
        assert order.status == "open"
        pending = (
            await session.scalars(select(BotEvent).where(BotEvent.delivered_at.is_(None)))
        ).all()
        assert any(e.kind == "order_opened" for e in pending)
    assert exchange.exposure and not (await engine.state()).emergency
    bot.send.reset_mock(side_effect=True)
    await bot.deliver_pending()
    assert bot.send.await_count == 3


async def test_review_reason_is_specific_and_edit_does_not_change_queued_snapshot(engine, bot):
    first = message(engine, "BTCUSDT\nEntry: 100\nRange: 10")
    assert await engine.ingest(first) == "needs_review"
    assert await engine.ingest(message(engine, text(), edited=True)) == "open"
    await bot.deliver_pending()
    review = bot.send.await_args_list[0].args[0]
    assert "İşlem yönü kesin olarak belirlenemedi" in review
    assert "LONG" not in review


@pytest.mark.parametrize("cause,title", [("tp", "KÂR ALMA (TP)"), ("sl", "ZARAR DURDURMA (SL)")])
async def test_protective_exit_is_translated_and_sent_once(engine, exchange, bot, cause, title):
    await engine.ingest(message(engine))
    await bot.deliver_pending()
    bot.send.reset_mock()
    exchange.finish(cause)
    await engine.reconcile()
    await engine.reconcile()
    await bot.deliver_pending()
    bot.send.assert_awaited_once()
    assert title in bot.send.await_args.args[0]
    assert "Pozisyonun kapandığı doğrulandı" in bot.send.await_args.args[0]


@pytest.mark.parametrize(
    "command,expected",
    [
        ("/start", "Güvenlik kontrolü ve iki aşamalı devam onayı"),
        ("/status", "Telegram kanal bağlantısı: Aktif"),
        ("/balance", "Kullanılabilir bakiye: 1000 USDT"),
        ("/positions", "Açık pozisyon bulunmuyor"),
        ("/trades", "Kayıtlı işlem bulunmuyor"),
        ("/stats", "Gerçekleşen kayıt sayısı: 0"),
        ("/pause", "Yeni işlemler durduruldu"),
        ("/resume", "Demo işlemler zaten etkin"),
        ("/close", "Kullanım: /close BTC-USDT-SWAP"),
        ("/close BTC-USDT-SWAP", "botun yönettiği aktif bir işlem bulunmuyor"),
        ("/resume CONFIRM invalid", "Yönetici onayı gerekiyor"),
    ],
)
async def test_admin_commands_are_turkish_without_raw_json(bot, command, expected):
    response = await bot.command(update(command=command))
    assert expected in response
    assert '"equity"' not in response and '"live_enabled"' not in response


async def test_nonempty_positions_trades_and_resume_preview_are_turkish(engine, bot):
    await engine.ingest(message(engine))
    positions = await bot.command(update(command="/positions"))
    assert "BTC/USDT" in positions and "Yön: LONG" in positions
    trades = await bot.command(update(command="/trades"))
    assert "Durum: Açık" in trades and "Gerçekleşen:" in trades
    await engine.trip("Past failure")
    preview = await bot.command(update(command="/resume"))
    assert "Doğrulanan pozisyon: 1" in preview
    assert "gross_daily_loss" not in preview
    confirmation = next(line for line in preview.splitlines() if line.startswith("/resume CONFIRM"))
    assert (await engine.state()).emergency
    assert "yönetici onayıyla" in await bot.command(update(command=confirmation))
    assert not (await engine.state()).emergency
    assert "Yönetici onayı gerekiyor" in await bot.command(update(command=confirmation))


def test_unknown_reason_is_safe_turkish_fallback():
    assert "SECRET" not in reason_tr("SECRET", "SECRET English error")
    assert "Güvenlik kontrolü tamamlanamadı" in reason_tr("UNKNOWN")


async def test_pause_from_api_notifies_but_telegram_command_reply_is_not_repeated(engine, bot):
    await engine.pause()
    await bot.deliver_pending()
    bot.send.assert_awaited_once()
    assert "BOT DURDURULDU" in bot.send.await_args.args[0]
    bot.send.reset_mock()
    assert await bot.command(update(command="/pause")) == "Yeni işlemler durduruldu."
    await bot.deliver_pending()
    bot.send.assert_not_awaited()
