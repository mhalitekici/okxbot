from datetime import timedelta
from decimal import Decimal as D

import pytest

from okx_bot.config import Settings
from okx_bot.domain import Direction, Message, ReviewRequired, utcnow
from okx_bot.parser import ChannelSignalParser, MZTradingBotParser

CHANNEL = -100123
EXAMPLE = (
    "CTUSDT destekten dönüş için pozitif durumda, kontrol etmenizi öneririm.\n\n"
    "Anlık Fiyat: 0.3604000000$\n"
    "Ortalama Aralık: %17.33\n"
    "Hedef Fiyat: Ortalama aralığın 0.3 - 0.9 katı\n"
    "Stop Fiyat: Ortalama aralığın 0.3 - 0.9 katı"
)


def message(content=EXAMPLE, channel=CHANNEL):
    return Message(channel, 1, content, utcnow(), topic_id=23576)


@pytest.mark.parametrize("symbol", ["CTUSDT", "CT/USDT", "CT-USDT"])
def test_mz_example_exact_levels_and_recorded_channel_rule(symbol):
    original = EXAMPLE.replace("CTUSDT", symbol)
    signal = ChannelSignalParser(CHANNEL, 23576).parse(message(original))
    assert signal.symbol == "CTUSDT" and signal.inst_id == "CT-USDT-SWAP"
    assert signal.direction == Direction.LONG
    assert signal.entry == D("0.3604") and signal.range_percent == D("17.33")
    assert signal.take_profit == D("0.397874392")
    assert signal.stop_loss == D("0.322925608")
    assert signal.level_coefficient == D("0.6")
    assert signal.interpretation_rule == MZTradingBotParser.support_rule
    assert signal.parser_name == "mztradingbot_v1"
    assert signal.notes == original


@pytest.mark.parametrize("explicit", ["LONG", "SHORT"])
def test_explicit_direction_uses_fixed_coefficient_and_no_inference(explicit):
    signal = ChannelSignalParser(CHANNEL, 23576).parse(
        message(
            (EXAMPLE if explicit == "LONG" else EXAMPLE.replace("destekten", "dirençten"))
            + "\n"
            + explicit
        )
    )
    assert signal.direction == explicit.lower()
    assert signal.interpretation_rule is None
    offset = D("0.037474392") * (1 if explicit == "LONG" else -1)
    assert signal.take_profit == signal.entry + offset
    assert signal.stop_loss == signal.entry - offset


@pytest.mark.parametrize(
    "parser,channel",
    [
        (ChannelSignalParser(), CHANNEL),
        (ChannelSignalParser(CHANNEL, 23576), -100999),
        (MZTradingBotParser(CHANNEL, 23576), -100999),
    ],
)
def test_inference_is_disabled_outside_explicit_mz_binding(parser, channel):
    with pytest.raises(ReviewRequired):
        parser.parse(message(channel=channel))


@pytest.mark.parametrize(
    "headline",
    [
        "CTUSDT kontrol etmenizi öneririm.",
        "CTUSDT dirençten dönüş için negatif durumda.",
        "CTUSDT destekten dönüş için pozitif durumda değil.",
        "CTUSDT destekten dönüş için pozitif durumda, ancak işlem açmayın.",
    ],
)
def test_unknown_or_negated_direction_is_not_guessed(headline):
    content = headline + "\n" + "\n".join(EXAMPLE.splitlines()[1:])
    with pytest.raises(ReviewRequired) as caught:
        ChannelSignalParser(CHANNEL, 23576).parse(message(content))
    assert caught.value.code == "MISSING_DIRECTION"


@pytest.mark.parametrize(
    "content",
    [
        EXAMPLE.replace("Anlık Fiyat: 0.3604000000$\n", ""),
        EXAMPLE.replace("Ortalama Aralık: %17.33\n", ""),
        EXAMPLE.replace("0.3604000000$", "0$"),
        EXAMPLE.replace("%17.33", "%100"),
        EXAMPLE.replace("0.3604000000$", "1e-3$"),
        EXAMPLE + "\nAnlık Fiyat: 0.4$",
        EXAMPLE + "\nSHORT LONG",
        EXAMPLE + "\nTP: 0.5",
        EXAMPLE.replace("0.3 - 0.9 katı", "0.4 - 0.8 katı"),
        EXAMPLE + "\nETHUSDT",
    ],
)
def test_missing_ambiguous_or_conflicting_mz_values_require_review(content):
    with pytest.raises(ReviewRequired):
        ChannelSignalParser(CHANNEL, 23576).parse(message(content))


def test_turkish_decimal_comma_and_uppercase_and_original_time():
    old = utcnow() - timedelta(hours=1)
    content = EXAMPLE.upper().replace("0.3604000000", "0,3604").replace("%17.33", "17,33%")
    signal = MZTradingBotParser(CHANNEL, 23576).parse(
        Message(CHANNEL, 1, content, old, topic_id=23576)
    )
    assert signal.entry == D("0.3604") and signal.timestamp == old


def test_other_channel_binding_is_rejected_by_settings():
    with pytest.raises(ValueError, match="MZTRADINGBOT_CHANNEL_ID"):
        Settings(_env_file=None, telegram_channel_id=CHANNEL, mztradingbot_channel_id=-100999)


def test_mz_binding_does_not_change_legacy_explicit_signals():
    from tests.fakes import text

    signal = ChannelSignalParser(CHANNEL, 23576).parse(message(text()))
    assert signal.symbol == "BTCUSDT" and signal.parser_name == "regex_v1"


async def test_engine_selects_mz_parser_from_configuration_without_execution(
    settings, database, exchange
):
    from sqlalchemy import select

    from okx_bot.engine import TradingEngine
    from okx_bot.models import TelegramSignal

    settings.mztradingbot_channel_id = settings.telegram_channel_id
    engine = TradingEngine(settings, database, exchange)
    # Unreconciled engine stays disabled, but parsing and persistence still run.
    from dataclasses import replace

    assert (
        await engine.ingest(replace(message(), topic_id=settings.telegram_topic_id)) == "rejected"
    )
    async with database.sessions() as session:
        row = await session.scalar(select(TelegramSignal))
        assert row.parsed["parser_name"] == "mztradingbot_v1"
        assert row.parsed["interpretation_rule"] == MZTradingBotParser.support_rule
    assert not exchange.submissions
