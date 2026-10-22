from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from okx_bot.config import Settings
from okx_bot.domain import Direction, RiskRejected, Signal
from okx_bot.limits import Limits

D = Decimal
LEVERAGE = D("10")


def decimal(value) -> Decimal:
    try:
        result = D(str(value))
        if not result.is_finite():
            raise ValueError
        return result
    except (ValueError, ArithmeticError) as exc:
        raise RiskRejected(
            "Missing or invalid exchange numeric data", code="INVALID_EXCHANGE_DATA"
        ) from exc


def step(value: Decimal, increment: Decimal, rounding=ROUND_FLOOR) -> Decimal:
    if increment <= 0:
        raise RiskRejected("Invalid tick/lot size")
    return (value / increment).to_integral_value(rounding=rounding) * increment


@dataclass(frozen=True)
class Instrument:
    inst_id: str
    family: str
    group_id: str
    contract_value: Decimal
    tick: Decimal
    lot: Decimal
    minimum: Decimal
    max_limit: Decimal

    @classmethod
    def from_okx(cls, row: dict):
        inst = row["instId"]
        if not (
            row.get("instType") == "SWAP"
            and row.get("settleCcy") == "USDT"
            and row.get("ctType") == "linear"
            and row.get("state") == "live"
            and row.get("ruleType") == "normal"
            and inst.endswith("-USDT-SWAP")
            and row.get("ctValCcy") == inst.split("-")[0]
        ):
            raise RiskRejected(
                "Instrument is not a live, normal USDT linear perpetual",
                code="INSTRUMENT_UNAVAILABLE",
            )
        if row.get("ctMult") and decimal(row["ctMult"]) != 1:
            raise RiskRejected("Unsupported contract multiplier")
        values = [decimal(row.get(k)) for k in ("ctVal", "tickSz", "lotSz", "minSz", "maxLmtSz")]
        if any(v <= 0 for v in values) or not row.get("instFamily") or not row.get("groupId"):
            raise RiskRejected("Incomplete instrument metadata")
        return cls(inst, row["instFamily"], row["groupId"], *values)


@dataclass(frozen=True)
class Balance:
    equity: Decimal
    available: Decimal
    unrealized: Decimal
    used_margin: Decimal | None = None
    order_frozen: Decimal | None = None


@dataclass(frozen=True)
class MarketQuote:
    bid: Decimal
    ask: Decimal
    mark: Decimal

    def market(self, direction: str) -> Decimal:
        return self.ask if direction == Direction.LONG else self.bid

    @property
    def spread_percent(self) -> Decimal:
        if self.bid <= 0 or self.ask < self.bid or self.mark <= 0:
            raise RiskRejected("Invalid bid/ask quote", code="INVALID_QUOTE")
        return (self.ask - self.bid) / ((self.ask + self.bid) / 2) * 100


@dataclass(frozen=True)
class Plan:
    contracts: Decimal
    limit_price: Decimal
    take_profit: Decimal
    stop_loss: Decimal
    liquidation_price: Decimal
    risk_amount: Decimal
    required_margin: Decimal
    position_margin: Decimal
    stop_distance_percent: Decimal


def safe_stop(direction: str, stop: Decimal, liquidation: Decimal, buffer=Decimal(0)):
    if liquidation <= 0 or not (
        stop > liquidation * (1 + buffer / 100)
        if direction == Direction.LONG
        else stop < liquidation * (1 - buffer / 100)
    ):
        raise RiskRejected(
            "Stop is beyond estimated liquidation, or liquidation data is absent",
            code="STOP_BEYOND_LIQUIDATION",
        )


class RiskManager:
    def __init__(self, settings: Settings):
        self.settings = settings

    def plan(
        self,
        signal: Signal,
        instrument: Instrument,
        balance: Balance,
        market: Decimal,
        mark: Decimal,
        fee: Decimal,
        tiers: list[dict],
        open_count: int,
        starting_equity: Decimal,
        daily_loss: Decimal,
        target_margin: Decimal | None = None,
        open_risk: Decimal = Decimal(0),
        pending_margin: Decimal = Decimal(0),
        limits: Limits | None = None,
    ) -> Plan:
        limits = limits or Limits.from_settings(self.settings)
        if balance.equity <= 0 or balance.available <= 0:
            raise RiskRejected(
                "Insufficient USDT equity/available balance", code="INSUFFICIENT_BALANCE"
            )
        if open_count >= min(limits.max_open_positions, limits.allocation_slots):
            raise RiskRejected("Maximum open positions reached", code="MAX_OPEN_POSITIONS")
        if (
            starting_equity <= 0
            or daily_loss >= starting_equity * limits.max_daily_loss_percent / 100
        ):
            raise RiskRejected("Daily loss limit reached", code="DAILY_LOSS_LIMIT")
        deviation = limits.max_entry_price_deviation_percent / 100
        if any(p <= 0 or abs(p - signal.entry) / signal.entry > deviation for p in (market, mark)):
            raise RiskRejected(
                "Market/mark exceeds entry deviation",
                code="ENTRY_DEVIATION",
                context={
                    "reference_price": str(signal.entry),
                    "market_price": str(market),
                    "mark_price": str(mark),
                    "price_deviation_percent": str(abs(market - signal.entry) / signal.entry * 100),
                    "max_deviation_percent": str(limits.max_entry_price_deviation_percent),
                },
            )
        long = signal.direction == Direction.LONG
        # A bounded IOC keeps the worst possible entry within the signal's band.
        slippage = limits.max_order_slippage_percent / 100
        limit = step(
            min(signal.entry * (1 + deviation), market * (1 + slippage))
            if long
            else max(signal.entry * (1 - deviation), market * (1 - slippage)),
            instrument.tick,
            ROUND_FLOOR if long else ROUND_CEILING,
        )
        tp = step(signal.take_profit, instrument.tick, ROUND_FLOOR if long else ROUND_CEILING)
        sl = step(signal.stop_loss, instrument.tick, ROUND_CEILING if long else ROUND_FLOOR)
        if not (tp > limit >= market > sl > 0 if long else 0 < tp < limit <= market < sl):
            raise RiskRejected(
                "Rounded TP/SL or IOC price conflicts with market", code="UNSAFE_LEVELS"
            )
        if fee < 0 or fee >= 1:
            raise RiskRejected("Invalid taker fee rate")
        # The channel stop determines risk; the legacy per-trade risk percentage never
        # narrows stops or reduces a slot's target budget.
        target_margin = (
            target_margin if target_margin is not None else balance.equity / limits.allocation_slots
        )
        if target_margin <= 0:
            raise RiskRejected("Invalid slot budget", code="INSUFFICIENT_BALANCE")
        margin_price = max(limit, market, signal.entry * (1 + deviation))
        contracts = step(
            target_margin * LEVERAGE / (instrument.contract_value * margin_price), instrument.lot
        )
        if contracts < instrument.minimum or contracts > instrument.max_limit:
            raise RiskRejected(
                "Contract quantity violates instrument limits", code="CONTRACT_LIMIT"
            )
        candidates = [
            t
            for t in tiers
            if decimal(t.get("minSz")) <= contracts <= decimal(t.get("maxSz"))
            and decimal(t.get("maxLever")) >= LEVERAGE
        ]
        if not candidates:
            raise RiskRejected(
                "No applicable exchange position tier for 10x", code="LEVERAGE_UNAVAILABLE"
            )
        mmr = max(decimal(t.get("mmr")) for t in candidates)
        if not 0 < mmr + fee < 1 / LEVERAGE:
            raise RiskRejected(
                "Missing or unsafe maintenance margin/fee data", code="UNSAFE_MARGIN_DATA"
            )
        quantity = contracts * instrument.contract_value
        margin = quantity * margin_price / LEVERAGE
        # Budget entry/exit fees and maintenance margin conservatively. Available
        # exchange balance already excludes exchange-frozen orders; local intents add a reserve.
        required_margin = margin + quantity * margin_price * (2 * fee + mmr)
        if required_margin + pending_margin + limits.minimum_free_margin > balance.available:
            raise RiskRejected(
                "Insufficient available margin and reserves", code="INSUFFICIENT_BALANCE"
            )
        # Rebase range levels at the most adverse allowed fill for the risk estimate.
        distance = (
            signal.range_percent * D("0.6") / 100 if signal.range_percent is not None else None
        )
        risk_stop = (
            margin_price * (1 - distance if long else 1 + distance)
            if signal.stop_loss_source == "range" and distance is not None
            else sl
        )
        unit_loss = instrument.contract_value * (
            abs(margin_price - risk_stop) + fee * (margin_price + risk_stop)
        )
        risk_amount = contracts * unit_loss
        if open_risk + risk_amount > balance.equity * limits.max_portfolio_risk_percent / 100:
            raise RiskRejected(
                "Portfolio estimated stop risk exceeds cap", code="PORTFOLIO_RISK_LIMIT"
            )
        # OKX's published isolated USDT linear formula; mmr and fee come from OKX.
        # Deduct the entry fee from allocated margin conservatively.
        margin_after_fee = quantity * limit / LEVERAGE - quantity * limit * fee
        liquidation = (
            (margin_after_fee - quantity * limit) / (quantity * (mmr + fee - 1))
            if long
            else (margin_after_fee + quantity * limit) / (quantity * (mmr + fee + 1))
        )
        safe_stop(signal.direction, sl, liquidation, limits.liquidation_buffer_percent)
        return Plan(
            contracts,
            limit,
            tp,
            sl,
            liquidation,
            risk_amount,
            required_margin,
            margin,
            abs(signal.entry - signal.stop_loss) / signal.entry * 100,
        )


def fill_levels(order, actual_price: Decimal, liquidation: Decimal) -> tuple[Decimal, Decimal]:
    """Rebase range-derived levels; preserve absolute levels, then recheck actual cash risk."""
    context = order.execution_context
    if not context:
        return (
            order.take_profit,
            order.stop_loss,
        )  # Existing orders retain their original protection.
    tick, fee, ct_value = (decimal(context[k]) for k in ("tick", "fee", "contract_value"))
    long = order.direction == Direction.LONG
    sign = 1 if long else -1
    spread = decimal(context["range_percent"]) if context["range_percent"] is not None else D(0)
    offset = actual_price * spread / 100 * D("0.6")
    tp = (
        step(actual_price + sign * offset, tick, ROUND_FLOOR if long else ROUND_CEILING)
        if context["take_profit_source"] == "range"
        else order.take_profit
    )
    sl = (
        step(actual_price - sign * offset, tick, ROUND_CEILING if long else ROUND_FLOOR)
        if context["stop_loss_source"] == "range"
        else order.stop_loss
    )
    if not (tp > actual_price > sl > 0 if long else sl > actual_price > tp > 0):
        raise RiskRejected("Fill-based levels contradict entry", code="UNSAFE_LEVELS")
    safe_stop(
        order.direction, sl, liquidation, decimal(context.get("liquidation_buffer_percent", "0"))
    )
    loss = order.contracts * ct_value * (abs(actual_price - sl) + fee * (actual_price + sl))
    if loss > decimal(context["risk_budget"]):
        raise RiskRejected("Actual fill risk exceeds account budget", code="FILL_RISK_EXCEEDED")
    return tp, sl
