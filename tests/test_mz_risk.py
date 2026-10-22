from decimal import Decimal as D

import pytest

from okx_bot.domain import RiskRejected
from okx_bot.risk import Balance, Instrument, RiskManager
from tests.test_mz_parser import CHANNEL, message


def plan(settings, signal):
    return RiskManager(settings).plan(
        signal=signal,
        instrument=Instrument(
            signal.inst_id, "CT-USDT", "4", D(1), D("0.0001"), D(1), D(1), D(100000)
        ),
        balance=Balance(D(1000), D(1000), D(0)),
        market=signal.entry,
        mark=signal.entry,
        fee=D("0.0005"),
        tiers=[{"minSz": "0", "maxSz": "100000", "maxLever": "10", "mmr": "0.005"}],
        open_count=0,
        starting_equity=D(1000),
        daily_loss=D(0),
    )


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_ct_example_stop_beyond_liquidation_is_rejected_independently_of_parser(settings, side):
    from okx_bot.parser import MZTradingBotParser

    item = message()
    signal = MZTradingBotParser(CHANNEL, 23576).parse(
        message(
            (item.text if side == "LONG" else item.text.replace("destekten", "dirençten"))
            + "\n"
            + side
        )
    )
    with pytest.raises(RiskRejected) as caught:
        plan(settings, signal)
    assert caught.value.code == "STOP_BEYOND_LIQUIDATION"


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_smaller_range_passes_risk_sizing_and_stop_precedes_liquidation(settings, side):
    from okx_bot.parser import MZTradingBotParser

    item = message()
    signal = MZTradingBotParser(CHANNEL, 23576).parse(
        message(
            (item.text if side == "LONG" else item.text.replace("destekten", "dirençten")).replace(
                "%17.33", "%5"
            )
            + "\n"
            + side
        )
    )
    result = plan(settings, signal)
    assert result.contracts > 0 and result.risk_amount <= 400
    assert result.position_margin <= D(1000) / 3
    assert result.required_margin <= 1000
    assert (
        result.stop_loss > result.liquidation_price
        if side == "LONG"
        else result.stop_loss < result.liquidation_price
    )
