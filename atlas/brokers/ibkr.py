"""Interactive Brokers adapter (IBKR UK: FCA-regulated, LSE UCITS ETFs, full API, free paper account).

Talks to a locally running IB Gateway / TWS through ib_async.
  * Paper gateway port 4002, live gateway port 4001. Paper account IDs start with 'DU'.
  * The adapter DETECTS live vs paper from the account ID. The risk engine refuses every order on a
    live account unless the investor has recorded a live-trading authorisation.
  * Use a dedicated IBKR API user with trading permission only (no withdrawals/transfers).

Price units: some LSE lines quote in pence. `python -m atlas.cli ibkr-check` measures IBKR's price
against the platform's GBP price for each ETF and stores a per-symbol scale (`ibkr_scale`), which
is applied to fills and average costs so every £ figure in the platform is in pounds.
"""
from __future__ import annotations

import asyncio
import math
import os
import time

from .base import Account, Broker, OrderStatus, Position

from ..gilts import GILT_PREFIX  # noqa: E402

EXCHANGES = ("LSEETF", "LSE")


def _ensure_loop() -> None:
    """ib_async needs an asyncio loop in the calling thread (API requests run in worker threads)."""
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


class IBKRBroker(Broker):
    name = "ibkr"

    def __init__(self, host: str | None = None, port: int | None = None, client_id: int | None = None,
                 scales: dict[str, float] | None = None, ib=None):
        _ensure_loop()
        if ib is None:
            from ib_async import IB  # lazy import: the platform works without ib_async installed
            ib = IB()
        self.ib = ib
        self.host = host or os.environ.get("IBKR_HOST", "127.0.0.1")
        self.port = int(port or os.environ.get("IBKR_PORT", "4002"))
        self.client_id = int(client_id or os.environ.get("IBKR_CLIENT_ID", "17"))
        self.scales = scales or {}
        self._contracts: dict = {}
        self._trades: dict = {}

    # -- connection ---------------------------------------------------------------------------
    def _ensure(self, attempts: int = 3):
        """Connect (or reconnect after a gateway drop) with exponential backoff."""
        last = None
        for i in range(attempts):
            if self.ib.isConnected():
                return self.ib
            try:
                self.ib.connect(self.host, self.port, clientId=self.client_id, timeout=15)
                return self.ib
            except Exception as e:  # gateway restarting / daily logout / network blip
                last = e
                try:
                    self.ib.disconnect()
                except Exception:
                    pass
                if i < attempts - 1:
                    time.sleep(2 ** (i + 1))
        raise ConnectionError(f"IB Gateway {self.host}:{self.port} unreachable after {attempts} attempts: {last}")

    def close(self) -> None:
        try:
            if self.ib.isConnected():
                self.ib.disconnect()
        except Exception:
            pass

    @property
    def account_id(self) -> str:
        accts = self._ensure().managedAccounts()
        return accts[0] if accts else ""

    @property
    def is_live(self) -> bool:  # type: ignore[override]
        # Fail safe: if we cannot tell, treat the account as LIVE so the risk gate applies.
        try:
            return not self.account_id.startswith("DU")
        except Exception:
            return True

    def health(self) -> dict:
        try:
            ib = self._ensure()
            acct = self.account_id
            return {"ok": ib.isConnected() and bool(acct), "broker": "ibkr", "account": acct,
                    "live": not acct.startswith("DU"), "port": self.port}
        except Exception as e:
            return {"ok": False, "broker": "ibkr", "live": None, "error": str(e), "port": self.port}

    # -- contracts ----------------------------------------------------------------------------
    def contract(self, symbol: str):
        from ib_async import Stock
        if symbol.startswith(GILT_PREFIX):
            if symbol not in self._contracts:
                self.load_gilts()
            if symbol not in self._contracts:
                raise RuntimeError(f"{symbol} is not a conventional UK gilt known to IBKR")
            return self._contracts[symbol]
        if symbol not in self._contracts:
            tick = symbol.removesuffix(".L")
            # SMART routing with the LSE ETF line as primary listing. Direct-routed orders
            # (exchange=LSEETF) are refused by IB Gateway's API precautionary settings (error 10311);
            # we keep those precautions and route SMART instead. The instrument is unchanged:
            # same conId / ISIN / GBP line, verified below - never a silent substitute.
            q = []
            for ex in EXCHANGES:
                q = [c for c in self._ensure().qualifyContracts(
                    Stock(tick, "SMART", "GBP", primaryExchange=ex)) if c]
                if q:
                    break
            if not q:
                raise RuntimeError(f"IBKR could not qualify an LSE GBP contract for {symbol}")
            c = q[0]
            if c.currency != "GBP" or c.primaryExchange not in EXCHANGES:
                raise RuntimeError(f"{symbol}: IBKR returned {c.primaryExchange}/{c.currency}, not an LSE GBP line")
            self._contracts[symbol] = c
        return self._contracts[symbol]

    def quote(self, symbol: str) -> float | None:
        """Latest IBKR price in IBKR's own quote units (delayed data is fine for unit checks)."""
        ib = self._ensure()
        ib.reqMarketDataType(4)  # delayed-frozen: works without a paid data subscription
        t = ib.reqTickers(self.contract(symbol))[0]
        for v in (t.marketPrice(), t.last, t.close):
            if v is not None and not (isinstance(v, float) and math.isnan(v)) and v > 0:
                return float(v)
        return None

    # -- UK gilts -----------------------------------------------------------------------------
    GILT_ISSUER_SYMBOL = os.environ.get("IBKR_GILT_SYMBOL", "UKT")  # IBKR issuer symbol for UK gilts

    def load_gilts(self, max_years: float = 20.0) -> dict:
        """Conventional UK gilts from IBKR contract details (coupon, maturity, ISIN, size rules).
        Index-linked gilts and anything not GBP / not UK Treasury are excluded."""
        from datetime import date as _date

        from ib_async import Contract

        from ..gilts import Gilt
        if getattr(self, "_gilts", None):
            return self._gilts
        ib = self._ensure()
        details = ib.reqContractDetails(Contract(secType="BOND", symbol=self.GILT_ISSUER_SYMBOL, currency="GBP",
                                                 exchange="SMART"))
        gilts, today = {}, _date.today()
        for cd in details or []:
            c = cd.contract
            ids = {x.tag: x.value for x in (cd.secIdList or [])}
            isin = ids.get("ISIN") or (c.secIdType == "ISIN" and c.secId) or ""
            desc = f"{cd.longName or ''} {cd.descAppend or ''} {cd.bondType or ''} {cd.couponType or ''}".upper()
            if c.currency != "GBP" or not isin.startswith("GB") or "LINK" in desc or "INDEX" in desc:
                continue
            try:
                mat = _date(int(cd.maturity[:4]), int(cd.maturity[4:6]), int(cd.maturity[6:8]))
            except Exception:
                continue
            if mat <= today or (mat - today).days / 365.25 > max_years:
                continue
            g = Gilt(name=(cd.descAppend or cd.longName or isin).strip(), coupon=float(cd.coupon or 0.0),
                     maturity=mat, isin=isin, con_id=c.conId)
            gilts[g.key] = g
            self._contracts[g.key] = c
            self._gilt_rules = getattr(self, "_gilt_rules", {})
            self._gilt_rules[g.key] = {"min_size": float(cd.minSize or 1.0), "increment": float(cd.sizeIncrement or 1.0),
                                       "min_tick": float(cd.minTick or 0.0001)}
        self._gilts = gilts
        return gilts

    def gilt_rules(self, key: str) -> dict:
        self.load_gilts()
        return getattr(self, "_gilt_rules", {}).get(key, {"min_size": 1.0, "increment": 1.0, "min_tick": 0.0001})

    def quote_gilts(self) -> dict:
        """Clean prices (per £100 nominal) for all loaded gilts. Missing quotes are left out."""
        ib = self._ensure()
        gilts = self.load_gilts()
        if not gilts:
            return {}
        ib.reqMarketDataType(4)
        keys = list(gilts)
        tickers = ib.reqTickers(*[self._contracts[k] for k in keys])
        out = {}
        for k, tk in zip(keys, tickers):
            for v in (tk.marketPrice(), tk.last, tk.close):
                if v is not None and not (isinstance(v, float) and math.isnan(v)) and 20 < v < 200:
                    out[k] = float(v)
                    break
        return out

    def _scale(self, symbol: str) -> float:
        return float(self.scales.get(symbol, 1.0))

    # -- account (broker-confirmed) -----------------------------------------------------------
    def account(self) -> Account:
        ib = self._ensure()
        acct = self.account_id
        cash = None
        for v in ib.accountValues(acct):
            if v.tag == "TotalCashValue" and v.currency == "GBP":
                cash = float(v.value)
        pos = {}
        held = list(ib.positions(acct))
        if any(getattr(p.contract, "secType", "") == "BOND" for p in held):
            self.load_gilts()
        rev = {c.conId: s for s, c in self._contracts.items()}
        for p in held:
            if getattr(p.contract, "secType", "") == "BOND":
                sym = rev.get(p.contract.conId, f"{GILT_PREFIX}conId{p.contract.conId}")
                if p.position:  # nominal £; IBKR avgCost for bonds is per 100 nominal -> per £1 nominal
                    pos[sym] = Position(sym, float(p.position), float(p.avgCost) / 100.0)
                continue
            sym = rev.get(p.contract.conId, p.contract.symbol + ".L")
            if p.position:
                pos[sym] = Position(sym, float(p.position), float(p.avgCost) * self._scale(sym))
        # No GBP cash line => report 0 so the risk engine refuses buys rather than assuming funds.
        return Account(cash=cash or 0.0, positions=pos)

    # -- orders -------------------------------------------------------------------------------
    def submit_market(self, symbol: str, side: str, qty: float, ref_price: float,
                      ref: str | None = None, limit_price: float | None = None) -> OrderStatus:
        from ib_async import LimitOrder, MarketOrder

        from ..config import MODE, PAPER_ONLY
        gilt = symbol.startswith(GILT_PREFIX)
        if not gilt and (qty != int(qty) or qty <= 0):
            return OrderStatus("", "rejected", message="IBKR LSE ETFs trade in whole shares")
        if gilt and (limit_price is None or qty <= 0):
            return OrderStatus("", "rejected", message="gilt orders must be limit orders with a positive nominal")
        ib = self._ensure()
        # Last line of defence, independent of the risk engine and the cycle's account gate.
        is_paper = self.account_id.startswith("DU")
        if not is_paper and (PAPER_ONLY or MODE != "live"):
            return OrderStatus("", "rejected", message="paper-only lock: account is not a DU paper account")
        if is_paper and MODE == "live":
            return OrderStatus("", "rejected", message="live mode refuses a DU paper account")
        action = "BUY" if side == "buy" else "SELL"
        if gilt:
            from ..gilts import accrued, settlement
            g = self.load_gilts().get(symbol)
            if g is None:
                return OrderStatus("", "rejected", message=f"{symbol} not in IBKR conventional gilt list")
            rules = self.gilt_rules(symbol)
            inc = rules["increment"] or 1.0
            nominal = math.floor(qty / inc) * inc
            if nominal < rules["min_size"]:
                return OrderStatus("", "rejected", message=f"nominal £{qty:,.0f} below IBKR minimum {rules['min_size']:g}")
            from datetime import date as _date
            clean_limit = limit_price * 100.0 - accrued(g, settlement(_date.today()))  # GBP per £1 dirty -> clean/100
            tick = rules["min_tick"] or 0.0001
            steps = clean_limit / tick
            clean_limit = (math.floor(steps) if side == "buy" else math.ceil(steps)) * tick
            order = LimitOrder(action, nominal, round(clean_limit, 6))
        elif limit_price is not None:
            px = limit_price / self._scale(symbol)               # GBP -> IBKR quote units (pence lines)
            tick = self._min_tick(symbol)
            steps = (px / tick)
            px = (math.floor(steps) if side == "buy" else math.ceil(steps)) * tick  # never worse than the collar
            order = LimitOrder(action, int(qty), round(px, 6))
        else:
            order = MarketOrder(action, int(qty))
        order.tif = "DAY"
        if ref:
            order.orderRef = ref
        trade = ib.placeOrder(self.contract(symbol), order)
        ib.sleep(2)
        oid = str(trade.order.orderId)
        self._trades[oid] = (symbol, trade)
        return self._status(symbol, trade)

    def order_status(self, broker_order_id: str) -> OrderStatus:
        ib = self._ensure()
        ib.sleep(0.5)
        found = self._trades.get(broker_order_id)
        if found is None:  # after a restart: look through the gateway's open/completed trades
            rev = {c.conId: s for s, c in self._contracts.items()}
            for tr in list(ib.trades()) + list(ib.reqCompletedOrders(apiOnly=True)):
                if str(tr.order.orderId) == broker_order_id or str(tr.order.permId) == broker_order_id:
                    found = (rev.get(tr.contract.conId, tr.contract.symbol + ".L"), tr)
                    break
        return self._status(*found) if found else OrderStatus(broker_order_id, "unknown")

    def _min_tick(self, symbol: str) -> float:
        if not hasattr(self, "_ticks"):
            self._ticks = {}
        if symbol not in self._ticks:
            try:
                d = self._ensure().reqContractDetails(self.contract(symbol))
                self._ticks[symbol] = float(d[0].minTick) if d and d[0].minTick else 0.01
            except Exception:
                self._ticks[symbol] = 0.01
        return self._ticks[symbol]

    def cancel_all(self) -> int:
        ib = self._ensure()
        working = [t for t in ib.openTrades() if not t.isDone()]
        ib.reqGlobalCancel()
        ib.sleep(2)
        return len(working)

    def find_by_ref(self, ref: str) -> OrderStatus | None:
        ib = self._ensure()
        rev = {c.conId: s for s, c in self._contracts.items()}
        for tr in list(ib.trades()) + list(ib.reqCompletedOrders(apiOnly=True)):
            if getattr(tr.order, "orderRef", "") == ref:
                sym = rev.get(tr.contract.conId, tr.contract.symbol + ".L")
                self._trades[str(tr.order.orderId)] = (sym, tr)
                return self._status(sym, tr)
        return None

    def contract_details(self, symbol: str) -> dict:
        """Name, ISIN, exchange, currency and trading class as IBKR reports them."""
        d = self._ensure().reqContractDetails(self.contract(symbol))
        if not d:
            return {}
        cd = d[0]
        ids = {t.tag: t.value for t in (cd.secIdList or [])}
        c = cd.contract
        return {"conId": c.conId, "name": cd.longName, "isin": ids.get("ISIN"), "exchange": c.exchange,
                "primary": c.primaryExchange, "currency": c.currency, "trading_class": c.tradingClass,
                "min_tick": cd.minTick, "valid_exchanges": cd.validExchanges}

    def cancel(self, broker_order_id: str) -> None:
        found = self._trades.get(broker_order_id)
        if found:
            self._ensure().cancelOrder(found[1].order)

    def _status(self, symbol: str, trade) -> OrderStatus:
        s = trade.orderStatus
        mapping = {"Filled": "filled", "Cancelled": "cancelled", "ApiCancelled": "cancelled",
                   "Inactive": "rejected"}
        status = mapping.get(s.status, "partially_filled" if s.filled else "submitted")
        comm = sum((f.commissionReport.commission or 0) for f in trade.fills if f.commissionReport)
        if not s.avgFillPrice:
            px = None
        elif symbol.startswith(GILT_PREFIX):
            # IBKR bond fills are clean prices per £100 nominal; the ledger records cash per £1 nominal
            # actually paid (clean + accrued), so reconciliation of cash is exact.
            from datetime import date as _date

            from ..gilts import accrued, settlement
            g = (getattr(self, "_gilts", None) or {}).get(symbol)
            acc = accrued(g, settlement(_date.today())) if g else 0.0
            px = (float(s.avgFillPrice) + acc) / 100.0
        else:
            px = float(s.avgFillPrice) * self._scale(symbol)
        msg = s.status
        if trade.log and status == "rejected":
            msg = f"{s.status}: {trade.log[-1].message}"
        return OrderStatus(str(trade.order.orderId), status, float(s.filled or 0), px, float(comm), msg)
