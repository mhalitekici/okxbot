import re
from datetime import UTC, datetime
from decimal import Decimal

from okx_bot.domain import DemoInstrumentMissing, ExchangeError, RiskRejected, utcnow
from okx_bot.exchange.client import OKXClient
from okx_bot.risk import Balance, Instrument, MarketQuote, decimal


class OKXGateway:
    def __init__(self, client: OKXClient):
        self.client = client

    async def account_config(self) -> dict:
        rows = await self.client.request("GET", "/api/v5/account/config")
        if len(rows) != 1 or rows[0].get("acctLv") != "2":
            raise RiskRejected(
                "Use a dedicated OKX demo account in Futures mode (acctLv=2)",
                code="UNSUPPORTED_ACCOUNT_MODE",
                context={
                    "account_level": rows[0].get("acctLv", "unknown") if rows else "missing",
                    "expected_account_level": "2",
                },
            )
        if rows[0].get("posMode") not in {"net_mode", "long_short_mode"}:
            raise RiskRejected(
                "Unsupported position mode",
                code="UNSUPPORTED_POSITION_MODE",
                context={"position_mode": rows[0].get("posMode", "missing")},
            )
        return rows[0]

    async def balance(self) -> Balance:
        rows = await self.client.request("GET", "/api/v5/account/balance", params={"ccy": "USDT"})
        details = [d for row in rows for d in row.get("details", []) if d.get("ccy") == "USDT"]
        if len(details) != 1:
            raise RiskRejected("USDT balance unavailable")
        row = details[0]
        isolated_upl = decimal(row.get("isoUpl") or "0")
        used = decimal(row["imr"]) if row.get("imr") not in {None, ""} else None
        if row.get("isoEq") not in {None, ""}:
            used = (used or Decimal(0)) + max(decimal(row["isoEq"]) - isolated_upl, Decimal(0))
        return Balance(
            decimal(row.get("eq")),
            decimal(row.get("availBal")),
            decimal(row.get("upl")),  # Currency-level upl already includes isolated positions.
            used,
            decimal(row["ordFrozen"]) if row.get("ordFrozen") not in {None, ""} else None,
        )

    async def instruments(self, inst_id: str | None = None) -> list[dict]:
        params = {"instType": "SWAP"}
        if inst_id:
            params["instId"] = inst_id
        return await self.client.request("GET", "/api/v5/account/instruments", params=params)

    async def instrument(self, inst_id: str) -> Instrument:
        if not re.fullmatch(r"[A-Z0-9]{1,20}-USDT-SWAP", inst_id):
            raise RiskRejected("Invalid perpetual symbol", code="INVALID_SYMBOL")
        # The account-eligible DEMO list distinguishes absence from an API failure.
        rows = await self.instruments()
        matches = [r for r in rows if r.get("instId") == inst_id]
        if not matches:
            raise DemoInstrumentMissing()
        if len(matches) != 1:
            raise RiskRejected("Ambiguous instrument metadata", code="INVALID_EXCHANGE_DATA")
        if matches[0].get("state") not in {"live", None, ""}:
            raise RiskRejected("Demo instrument is not trading", code="INSTRUMENT_SUSPENDED")
        return Instrument.from_okx(matches[0])

    async def quote(self, inst_id: str) -> MarketQuote:
        ticker = await self.client.request(
            "GET", "/api/v5/market/ticker", params={"instId": inst_id}, private=False
        )
        marks = await self.client.request(
            "GET",
            "/api/v5/public/mark-price",
            params={"instType": "SWAP", "instId": inst_id},
            private=False,
        )
        if len(ticker) != 1 or len(marks) != 1:
            raise RiskRejected("No market data")
        for row in (ticker[0], marks[0]):
            age_ms = Decimal(str(utcnow().timestamp() * 1000)) - decimal(row.get("ts"))
            if not -5000 <= age_ms <= 10000:
                raise RiskRejected("Stale market data or clock skew", code="STALE_MARKET_DATA")
        quote = MarketQuote(
            decimal(ticker[0].get("bidPx")),
            decimal(ticker[0].get("askPx")),
            decimal(marks[0].get("markPx")),
        )
        _ = quote.spread_percent
        return quote

    async def prices(self, inst_id: str, direction: str) -> tuple[Decimal, Decimal]:
        quote = await self.quote(inst_id)
        return quote.market(direction), quote.mark

    async def risk_metadata(self, instrument: Instrument):
        tiers = await self.client.request(
            "GET",
            "/api/v5/public/position-tiers",
            private=False,
            params={"instType": "SWAP", "tdMode": "isolated", "instFamily": instrument.family},
        )
        rates = await self.client.request(
            "GET",
            "/api/v5/account/trade-fee",
            params={"instType": "SWAP", "instFamily": instrument.family},
        )
        groups = [
            g
            for r in rates
            for g in r.get("feeGroup", [])
            if g.get("groupId") == instrument.group_id
        ]
        if len(groups) == 1:
            raw_fee = groups[0].get("taker")
        elif len(rates) == 1 and not rates[0].get("feeGroup"):
            raw_fee = rates[0].get("takerU")  # Legacy OKX demo response.
        else:
            raise RiskRejected("Exact instrument fee group unavailable")
        # Positive OKX rates are rebates. Do not budget any rebate as extra equity.
        fee = max(-decimal(raw_fee), Decimal(0))
        return fee, tiers

    async def set_leverage(self, inst_id: str, pos_side: str):
        payload = {"instId": inst_id, "lever": "10", "mgnMode": "isolated"}
        if pos_side != "net":
            payload["posSide"] = pos_side
        await self.client.request("POST", "/api/v5/account/set-leverage", payload=payload)
        rows = await self.client.request(
            "GET",
            "/api/v5/account/leverage-info",
            params={"instId": inst_id, "mgnMode": "isolated"},
        )
        matches = [r for r in rows if r.get("posSide", "net") == pos_side]
        if not matches or any(decimal(r.get("lever")) != 10 for r in matches):
            raise RiskRejected(
                "Exchange did not confirm 10x isolated leverage", code="LEVERAGE_UNAVAILABLE"
            )

    async def positions(self):
        rows = await self.client.request("GET", "/api/v5/account/positions")
        return [r for r in rows if decimal(r.get("pos")) != 0]

    async def open_orders(self):
        return await self._pages("/api/v5/trade/orders-pending", {}, "ordId")

    async def open_algo_orders(self):
        rows = []
        # OKX permits conditional,oco together; the other types require separate queries.
        for kind in (
            "conditional,oco",
            "trigger",
            "move_order_stop",
            "iceberg",
            "twap",
            "smart_iceberg",
            "chase",
        ):
            rows.extend(
                await self._pages(
                    "/api/v5/trade/orders-algo-pending",
                    {"ordType": kind},
                    "algoId",
                )
            )
        return rows

    async def order(self, inst_id: str, client_id: str):
        try:
            rows = await self.client.request(
                "GET", "/api/v5/trade/order", params={"instId": inst_id, "clOrdId": client_id}
            )
        except ExchangeError as exc:
            if exc.code == "51603":
                return None
            raise
        return rows[0] if rows else None

    async def algo(self, client_id: str):
        try:
            rows = await self.client.request(
                "GET", "/api/v5/trade/order-algo", params={"algoClOrdId": client_id}
            )
        except ExchangeError as exc:
            if exc.code == "51603":
                return None
            raise
        return rows[0] if rows else None

    async def order_by_id(self, inst_id: str, order_id: str):
        rows = await self.client.request(
            "GET", "/api/v5/trade/order", params={"instId": inst_id, "ordId": order_id}
        )
        if len(rows) != 1:
            raise ExchangeError("ORDER_DETAILS_UNAVAILABLE")
        return rows[0]

    async def place_entry(self, payload: dict):
        if payload.get("tdMode") != "isolated" or not payload.get("instId", "").endswith(
            "-USDT-SWAP"
        ):
            raise RiskRejected("Only isolated USDT SWAP entries are permitted")
        rows = await self.client.request("POST", "/api/v5/trade/order", payload=payload)
        if len(rows) != 1 or not rows[0].get("ordId"):
            raise ExchangeError("MISSING_ORDER_ACK", ambiguous=True)
        return rows[0]

    async def cancel_order(self, inst_id: str, client_id: str):
        return await self.client.request(
            "POST", "/api/v5/trade/cancel-order", payload={"instId": inst_id, "clOrdId": client_id}
        )

    async def cancel_algo(self, inst_id: str, algo_id: str):
        return await self.client.request(
            "POST", "/api/v5/trade/cancel-algos", payload=[{"instId": inst_id, "algoId": algo_id}]
        )

    async def amend_protection(self, inst_id: str, algo_id: str, tp: Decimal, sl: Decimal):
        rows = await self.client.request(
            "POST",
            "/api/v5/trade/amend-algos",
            payload={
                "instId": inst_id,
                "algoId": algo_id,
                "cxlOnFail": False,
                "newTpTriggerPx": str(tp),
                "newTpOrdPx": "-1",
                "newTpTriggerPxType": "mark",
                "newSlTriggerPx": str(sl),
                "newSlOrdPx": "-1",
                "newSlTriggerPxType": "mark",
            },
        )
        if len(rows) != 1 or rows[0].get("algoId") != algo_id:
            raise ExchangeError("PROTECTION_AMEND_UNCONFIRMED", ambiguous=True)

    async def close_position(self, inst_id: str, pos_side: str):
        raise RiskRejected(
            "Whole-position close is disabled; use a verified sized exit", code="UNKNOWN_OWNERSHIP"
        )

    async def close_owned(self, order, quantity, client_id):
        payload = {
            "instId": order.inst_id,
            "tdMode": "isolated",
            "posSide": order.pos_side,
            "side": "sell" if order.direction == "long" else "buy",
            "ordType": "market",
            "sz": str(quantity),
            "clOrdId": client_id,
        }
        if order.pos_side == "net":
            payload["reduceOnly"] = True
        rows = await self.client.request("POST", "/api/v5/trade/order", payload=payload)
        if len(rows) != 1 or not rows[0].get("ordId"):
            raise ExchangeError("MISSING_CLOSE_ACK", ambiguous=True)
        return rows[0]

    async def _pages(self, path: str, params: dict, cursor_field: str) -> list[dict]:
        result = []
        seen = set()
        for _ in range(100):
            rows = await self.client.request("GET", path, params={**params, "limit": "100"})
            result.extend(rows)
            if len(rows) < 100:
                return result
            cursor = rows[-1][cursor_field]
            if cursor in seen:
                break
            seen.add(cursor)
            params = {**params, "after": cursor}
        raise RiskRejected("Exchange history pagination incomplete; refusing risk estimate")

    async def daily_loss(self, day: datetime) -> Decimal:
        rows = await self._pages(
            "/api/v5/account/bills",
            {"ccy": "USDT", "begin": str(int(day.timestamp() * 1000))},
            "billId",
        )
        loss = Decimal(0)
        for row in rows:
            if row.get("type") == "8":  # Funding fee (negative is a debit).
                loss += max(-decimal(row.get("balChg")), Decimal(0))
            elif row.get("type") == "2":  # Trades: gross losses and fees, ignore gains/rebates.
                loss += max(-decimal(row.get("pnl")), Decimal(0))
                loss += max(-decimal(row.get("fee")), Decimal(0))
        return loss

    async def fills(self, begin: datetime) -> list[dict]:
        return await self._pages(
            "/api/v5/trade/fills-history",
            {"instType": "SWAP", "begin": str(int(begin.astimezone(UTC).timestamp() * 1000))},
            "billId",
        )

    async def bills(self, begin: datetime) -> list[dict]:
        return await self._pages(
            "/api/v5/account/bills-archive",
            {"ccy": "USDT", "begin": str(int(begin.astimezone(UTC).timestamp() * 1000))},
            "billId",
        )
