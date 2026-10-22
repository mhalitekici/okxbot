from decimal import Decimal as D

import pytest

from okx_bot.domain import Message, RiskRejected, utcnow
from okx_bot.parser import RegexSignalParser
from okx_bot.risk import Balance, Instrument, RiskManager, decimal, safe_stop
from tests.fakes import text


def build(settings, direction="LONG", **changes):
    params = dict(
        signal=RegexSignalParser().parse(Message(-1, 1, text(direction), utcnow())),
        instrument=Instrument(
            "BTC-USDT-SWAP", "BTC-USDT", "4", D("0.01"), D("0.1"), D(1), D(1), D(10000)
        ),
        balance=Balance(D(1000), D(1000), D(0)),
        market=D(100),
        mark=D(100),
        fee=D("0.0005"),
        tiers=[{"minSz": "0", "maxSz": "10000", "maxLever": "100", "mmr": "0.005"}],
        open_count=0,
        starting_equity=D(1000),
        daily_loss=D(0),
    )
    params.update(changes)
    return RiskManager(settings).plan(**params)


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_contract_sizing_includes_fees_and_margin(settings, direction):
    plan = build(settings, direction)
    assert plan.contracts > 100  # Contracts differ from coin quantity.
    assert plan.risk_amount <= 400
    assert plan.position_margin <= D(1000) / 3
    assert plan.required_margin <= 1000
    safe_stop(direction.lower(), plan.stop_loss, plan.liquidation_price)


@pytest.mark.parametrize(
    "change",
    [
        {"open_count": 3},
        {"market": D(101)},
        {"mark": D(101)},
        {"tiers": []},
        {"daily_loss": D(30)},
        {"balance": Balance(D(1000), D(0), D(0))},
        {"tiers": [{"minSz": "0", "maxSz": "10000", "maxLever": "5", "mmr": "0.005"}]},
    ],
)
def test_risk_rejections(settings, change):
    with pytest.raises(RiskRejected):
        build(settings, **change)


def test_liquidation_does_not_use_assumed_percentage(settings):
    signal = RegexSignalParser().parse(Message(-1, 1, text().replace("%10", "%17.33"), utcnow()))
    with pytest.raises(RiskRejected, match="liquidation"):
        build(settings, signal=signal)


@pytest.mark.parametrize(
    "direction,stop,liq", [("long", "90", "91"), ("short", "110", "109"), ("long", "95", "0")]
)
def test_stop_must_precede_liquidation(direction, stop, liq):
    with pytest.raises(RiskRejected):
        safe_stop(direction, D(stop), D(liq))


@pytest.mark.parametrize("value", ["", None, "NaN", "Infinity", "bad"])
def test_invalid_exchange_numbers(value):
    with pytest.raises(RiskRejected):
        decimal(value)


def test_small_balance_cannot_round_up_risk(settings):
    with pytest.raises(RiskRejected, match="quantity"):
        build(settings, balance=Balance(D("0.01"), D("0.01"), D(0)), starting_equity=D("0.01"))


def test_instrument_validation_blocks_spot_and_inverse():
    row = {
        "instId": "BTC-USDT-SWAP",
        "instFamily": "BTC-USDT",
        "groupId": "4",
        "instType": "SWAP",
        "settleCcy": "USDT",
        "ctValCcy": "BTC",
        "ctType": "linear",
        "ruleType": "normal",
        "state": "live",
        "ctVal": "0.01",
        "tickSz": "0.1",
        "lotSz": "1",
        "minSz": "1",
        "maxLmtSz": "10000",
    }
    assert Instrument.from_okx(row).contract_value == D("0.01")
    for key, value in [
        ("instType", "SPOT"),
        ("ctType", "inverse"),
        ("state", "suspend"),
        ("ctValCcy", "USD"),
        ("ctMult", "10"),
        ("groupId", ""),
    ]:
        with pytest.raises(RiskRejected):
            Instrument.from_okx({**row, key: value})
