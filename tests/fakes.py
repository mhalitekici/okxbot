from copy import deepcopy
from decimal import Decimal

from telethon import types

from okx_bot.domain import DemoInstrumentMissing, ExchangeError, utcnow
from okx_bot.risk import Balance, Instrument, MarketQuote


def forum_topic(identifier=23576, title="Signal room"):
    return types.ForumTopic(
        id=identifier,
        date=utcnow(),
        peer=types.PeerChannel(123),
        title=title,
        icon_color=0,
        top_message=32876,
        read_inbox_max_id=0,
        read_outbox_max_id=0,
        unread_count=0,
        unread_mentions_count=0,
        unread_reactions_count=0,
        unread_poll_votes_count=0,
        from_id=types.PeerUser(1),
        notify_settings=types.PeerNotifySettings(),
    )


class DemoExchange:
    """Stateful test double, including accepted-but-lost order acknowledgements."""

    def __init__(self):
        self.mode = "net_mode"
        self.submissions = []
        self.leverage_calls = []
        self.closed = []
        self.canceled = []
        self.row = None
        self.protection = None
        self.exposure = []
        self.available = Decimal("1000")
        self.equity = Decimal("1000")
        self.loss = Decimal(0)
        self.fail_preflight = False
        self.rejection = False
        self.disconnect_after_accept = False
        self.disconnect_before_accept = False
        self.partial = False
        self.no_fill = False
        self.no_protection = False
        self.close_fails = False
        self.bad_liquidation = False
        self.fills_data = []
        self.bills_data = []
        self.orders = {}
        self.algos = {}
        self.latest_client = None
        self.latest_algo_client = None
        self.market = Decimal("100")
        self.pending_orders = []
        self.extra_algos = []
        self.available_instruments = {"BTC-USDT-SWAP"}
        self.bid = self.ask = self.mark = self.fill_price = None
        self.instrument_tick = Decimal("0.1")
        self.amendments = []
        self.fail_amend = self.ignore_amend = False

    async def account_config(self):
        if self.fail_preflight:
            raise ExchangeError("TRANSPORT_OR_RESPONSE")
        return {"acctLv": "2", "posMode": self.mode, "uid": "offline-demo-account"}

    async def instrument(self, inst_id):
        if inst_id not in self.available_instruments:
            raise DemoInstrumentMissing()
        return Instrument(
            inst_id,
            inst_id.removesuffix("-SWAP"),
            "4",
            Decimal(1),
            self.instrument_tick,
            Decimal(1),
            Decimal(1),
            Decimal(10000),
        )

    async def balance(self):
        return Balance(self.equity, self.available, Decimal(0))

    async def daily_loss(self, day):
        return self.loss

    async def prices(self, inst_id, direction):
        return self.market, self.market

    async def quote(self, inst_id):
        return MarketQuote(
            self.bid if self.bid is not None else self.market,
            self.ask if self.ask is not None else self.market,
            self.mark if self.mark is not None else self.market,
        )

    async def risk_metadata(self, instrument):
        return Decimal("0.0005"), [
            {"minSz": "0", "maxSz": "10000", "maxLever": "100", "mmr": "0.005"}
        ]

    async def set_leverage(self, inst_id, pos_side):
        self.leverage_calls.append((inst_id, pos_side, "10", "isolated"))

    async def positions(self):
        return deepcopy(self.exposure)

    async def open_orders(self):
        return deepcopy(self.pending_orders)

    async def open_algo_orders(self):
        rows = (
            [self.protection] if self.protection and self.protection.get("state") == "live" else []
        )
        others = [
            a
            for k, a in self.algos.items()
            if k != self.latest_algo_client and a.get("state") == "live"
        ]
        return deepcopy(rows + others + self.extra_algos)

    async def place_entry(self, payload):
        self.submissions.append(deepcopy(payload))
        if self.rejection:
            raise ExchangeError("51008")
        if self.disconnect_before_accept:
            raise ExchangeError("TRANSPORT_OR_RESPONSE", ambiguous=True)
        quantity = Decimal(payload["sz"])
        order_id = str(int(payload["clOrdId"][1:13], 16))
        filled = Decimal(0) if self.no_fill else quantity / 2 if self.partial else quantity
        fill_price = self.fill_price if self.fill_price is not None else self.market
        self.row = {
            "ordId": order_id,
            "state": "canceled" if self.partial or self.no_fill else "filled",
            "accFillSz": str(filled),
            "avgPx": str(fill_price) if filled else "",
            "instId": payload["instId"],
            "side": payload["side"],
            "posSide": payload["posSide"],
            "clOrdId": payload["clOrdId"],
            "category": "normal",
        }
        self.latest_client = payload["clOrdId"]
        self.orders[payload["clOrdId"]] = self.row
        long = payload["side"] == "buy"
        if filled:
            self.exposure += [
                {
                    "instId": payload["instId"],
                    "posSide": payload["posSide"],
                    "mgnMode": "isolated",
                    "lever": "10",
                    "pos": str(-filled if not long and payload["posSide"] == "net" else filled),
                    "avgPx": str(fill_price),
                    "liqPx": str(
                        fill_price
                        * (
                            Decimal("0.99")
                            if self.bad_liquidation
                            else Decimal("0.91")
                            if long
                            else Decimal("1.09")
                        )
                    ),
                    "posId": "pos" + order_id,
                    "upl": "0",
                    "markPx": str(fill_price),
                }
            ]
            self.add_fill(self.row, filled, fill_price, Decimal(0))
        attached = payload["attachAlgoOrds"][0]
        if not self.no_protection and filled == quantity:
            self.protection = {
                "algoId": str(int(attached["attachAlgoClOrdId"][1:13], 16)),
                "algoClOrdId": attached["attachAlgoClOrdId"],
                "state": "live",
                "ordType": "oco",
                "tdMode": "isolated",
                "instId": payload["instId"],
                "posSide": payload["posSide"],
                "sz": str(quantity),
                "side": "sell" if long else "buy",
                "reduceOnly": "true",
                **{k: v for k, v in attached.items() if k != "attachAlgoClOrdId"},
            }
            self.latest_algo_client = attached["attachAlgoClOrdId"]
            self.algos[self.latest_algo_client] = self.protection
        if self.disconnect_after_accept:
            raise ExchangeError("TRANSPORT_OR_RESPONSE", ambiguous=True)
        return {"ordId": order_id}

    async def order(self, inst_id, client_id):
        return deepcopy(
            self.row
            if not client_id or client_id == self.latest_client
            else self.orders.get(client_id)
        )

    async def algo(self, client_id):
        return deepcopy(
            self.protection
            if not client_id or client_id == self.latest_algo_client
            else self.algos.get(client_id)
        )

    async def order_by_id(self, inst_id, order_id):
        return deepcopy(
            next(
                (
                    o
                    for o in self.orders.values()
                    if o["ordId"] == order_id and o["instId"] == inst_id
                ),
                None,
            )
        )

    def add_fill(self, order, quantity, price, pnl):
        self.fills_data.append(
            {
                "instId": order["instId"],
                "tradeId": f"{order['ordId']}f{len(self.fills_data) + 1}",
                "ordId": order["ordId"],
                "fillPx": str(price),
                "fillSz": str(quantity),
                "fillPnl": str(pnl),
                "fee": str(-quantity * price * Decimal("0.0005")),
                "feeCcy": "USDT",
                "side": order["side"],
                "ts": str(int(utcnow().timestamp() * 1000)),
            }
        )

    def finish(self, cause="tp", inst_id=None, fraction=Decimal(1)):
        algo = (
            next(a for a in self.algos.values() if a["instId"] == inst_id)
            if inst_id
            else self.protection
        )
        position = next(p for p in self.exposure if p["instId"] == algo["instId"])
        quantity = abs(Decimal(position["pos"])) * fraction
        price = Decimal(algo["tpTriggerPx"] if cause == "tp" else algo["slTriggerPx"])
        exit_id = "exit" + algo["algoId"] + str(len(self.orders))
        row = {
            "ordId": exit_id,
            "instId": algo["instId"],
            "posSide": algo["posSide"],
            "side": algo["side"],
            "state": "filled",
            "accFillSz": str(quantity),
            "avgPx": str(price),
            "category": "normal",
        }
        self.orders[exit_id] = row
        algo.update(
            state="effective", actualSide=cause, ordIdList=[*(algo.get("ordIdList") or []), exit_id]
        )
        signed = 1 if row["side"] == "sell" else -1
        self.add_fill(
            row, quantity, price, (price - Decimal(position["avgPx"])) * quantity * signed
        )
        if fraction == 1:
            self.exposure.remove(position)
        else:
            position["pos"] = str(Decimal(position["pos"]) * (1 - fraction))

    async def cancel_order(self, inst_id, client_id):
        self.canceled.append(client_id)
        if self.row:
            self.row["state"] = "canceled"

    async def cancel_algo(self, inst_id, algo_id):
        self.protection["state"] = "canceled"

    async def amend_protection(self, inst_id, algo_id, tp, sl):
        self.amendments.append((inst_id, algo_id, str(tp), str(sl)))
        if self.fail_amend:
            raise ExchangeError("51000")
        if not self.ignore_amend:
            self.protection.update(tpTriggerPx=str(tp), slTriggerPx=str(sl))

    async def close_position(self, inst_id, pos_side):
        if self.close_fails:
            raise ExchangeError("TRANSPORT_OR_RESPONSE", ambiguous=True)
        self.closed.append((inst_id, pos_side))
        self.exposure = []

    async def close_owned(self, order, quantity, client_id):
        if self.close_fails:
            raise ExchangeError("TRANSPORT_OR_RESPONSE", ambiguous=True)
        self.closed.append((order.inst_id, order.pos_side))
        position = next(
            p
            for p in self.exposure
            if p["instId"] == order.inst_id and p["posSide"] == order.pos_side
        )
        row = {
            "ordId": "close" + order.client_id,
            "instId": order.inst_id,
            "posSide": order.pos_side,
            "side": "sell" if order.direction == "long" else "buy",
            "state": "filled",
            "accFillSz": str(quantity),
            "avgPx": str(self.market),
            "clOrdId": client_id,
            "category": "normal",
        }
        self.orders[client_id] = row
        self.add_fill(
            row,
            quantity,
            self.market,
            (self.market - Decimal(position["avgPx"]))
            * quantity
            * (1 if order.direction == "long" else -1),
        )
        self.exposure.remove(position)
        return {"ordId": row["ordId"]}

    async def fills(self, begin):
        return deepcopy(self.fills_data)

    async def bills(self, begin):
        return deepcopy(self.bills_data)


def text(direction="LONG"):
    return f"Coin: BTCUSDT\nFiyat: 100\nOrtalama Aralık: %10\n{direction}"


def update(admin_id=42, command="/status", **changes):
    return {
        "update_id": 1,
        "message": {
            "from": {"id": admin_id},
            "chat": {"id": admin_id, "type": "private"},
            "text": command,
            "date": int(utcnow().timestamp()),
            **changes,
        },
    }
