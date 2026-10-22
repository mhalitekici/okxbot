from datetime import timedelta
from decimal import Decimal

import pytest

from okx_bot.domain import Message, ReviewRequired, utcnow
from okx_bot.parser import RegexSignalParser
from tests.fakes import text


def parse(content):
    return RegexSignalParser().parse(Message(-1, 1, content, utcnow()))


@pytest.mark.parametrize("direction,tp,sl", [("LONG", "106", "94"), ("SHORT", "94", "106")])
def test_exact_decimal_levels(direction, tp, sl):
    signal = parse(text(direction))
    assert signal.take_profit == Decimal(tp)
    assert signal.stop_loss == Decimal(sl)
    assert signal.inst_id == "BTC-USDT-SWAP"


def test_user_example_missing_direction_requires_review():
    with pytest.raises(ReviewRequired):
        parse("Coin: CTUSDT\nFiyat: 0.3604\nOrtalama Aralık: %17.33")


def test_small_price_decimal_and_turkish_comma():
    signal = parse("Coin: CT/USDT\nFiyat: 0,3604\nOrtalama Aralık: %17,33\nLONG")
    assert signal.take_profit == Decimal("0.397874392")
    assert signal.stop_loss == Decimal("0.322925608")


@pytest.mark.parametrize(
    "content",
    [
        "LONG BTCUSDT",
        text() + "\nSHORT",
        text() + "\nETHUSDT",
        text() + "\nTP: 90",
        text() + "\nSL: 101",
        text() + "\nTP1: 106",
        text() + "\nTP: 106\nTP: 107",
        text().replace("100", "0"),
        text().replace("%10", "%100"),
        text() + "\ntime: 2026-01-01",
        text() + "\nTP: bad",
        text() + "\nSL: 95%",
        text().replace("100", "1e-3"),
        text().replace("100", "1,000.5"),
        text() + "\nTP: 106-110",
    ],
)
def test_ambiguous_or_conflicting_signals(content):
    with pytest.raises(ReviewRequired):
        parse(content)


def test_explicit_levels_override_calculated_values():
    signal = parse(text() + "\nTP: 108\nSL: 96")
    assert signal.take_profit == 108 and signal.stop_loss == 96
    signal = parse("BTCUSDT LONG\nEntry: 100\nTP: 110\nSL: 95")
    assert signal.range_percent is None


def test_explicit_timestamp_cannot_refresh_old_telegram_message():
    old = utcnow() - timedelta(hours=1)
    message = Message(-1, 1, text() + f"\ntime: {utcnow().isoformat()}", old)
    assert RegexSignalParser().parse(message).timestamp == old
