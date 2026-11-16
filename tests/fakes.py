"""A controllable fake IB Gateway for failure simulation (uses ib_async's real order/trade types)."""
from types import SimpleNamespace

from ib_async import OrderStatus as IBOrderStatus
from ib_async import Trade

from atlas.config import UNIVERSE

PENCE = {"EMIM.L", "IUKP.L", "SGLN.L"}


class ChaosIB:
    def __init__(self, account="DU1234567", gbp_cash=100_000.0, prices=None):
        self.connected = False
        self.account = account
        self.cash = gbp_cash
        self.prices = prices or {}      # IBKR units
        self.pos = {}                   # conId -> [contract, qty, avgCost]
        self.next_id = 500
        self._trades = []
        # failure knobs
        self.refuse_connect = False
        self.drop_after_accept = False  # placeOrder registers the order, then raises (connection lost)
        self.fill_mode = "full"         # full | none | partial
        self.price_move = 0.0           # market moves this fraction between approval and fill

    def isConnected(self): return self.connected
    def connect(self, host, port, clientId, timeout):
        if self.refuse_connect:
            raise ConnectionRefusedError("gateway down")
        self.connected = True
    def disconnect(self): self.connected = False
    def managedAccounts(self): return [self.account]
    def sleep(self, s): pass
    def reqMarketDataType(self, t): pass

    def reqTickers(self, *contracts):
        out = []
        for c in contracts:
            row = self._bond_row(c.conId) if getattr(c, "secType", "") == "BOND" else None
            if row:
                p = row[5]
            else:
                sym = c.symbol + ".L"
                p = self.prices.get(sym)
            out.append(SimpleNamespace(marketPrice=lambda p=p: p, last=p, close=p))
        return out

    def qualifyContracts(self, c):
        sym = c.symbol + ".L"
        if sym not in UNIVERSE or c.exchange != "SMART" or c.primaryExchange != "LSEETF":
            return []
        c.conId = 1000 + list(UNIVERSE).index(sym)
        c.currency = "GBP"
        return [c]

    # ---- gilts: a small book of conventional gilts (clean price per £100) ----
    GILTS = [  # conId, isin, name, coupon, maturity YYYYMMDD, clean
        (7001, "GB00BLPK7110", "0 1/8% Treasury Gilt 2028", 0.125, "20280131", 95.30),
        (7002, "GB00BMBL1F74", "0 1/2% Treasury Gilt 2029", 0.5, "20290131", 92.40),
        (7003, "GB00BL68HJ26", "0 1/4% Treasury Gilt 2031", 0.25, "20310731", 82.10),
        (7004, "GB00BTHH2R79", "4 1/2% Treasury Gilt 2028", 4.5, "20280607", 100.60),
        (7005, "GB00B3Y1JG82", "0 1/8% Index-linked Treasury Gilt 2029", 0.125, "20290322", 99.0),
    ]

    def _bond_contract(self, row):
        from ib_async import Contract
        c = Contract(secType="BOND", symbol="UKT", currency="GBP", exchange="SMART")
        c.conId = row[0]
        return c

    def reqContractDetails(self, c):
        if getattr(c, "secType", "") == "BOND":
            out = []
            for row in self.GILTS:
                out.append(SimpleNamespace(contract=self._bond_contract(row), secIdList=[SimpleNamespace(tag="ISIN", value=row[1])],
                                           longName=row[2], descAppend=row[2], bondType="", couponType="FIXED",
                                           coupon=row[3], maturity=row[4], minSize=1.0, sizeIncrement=1.0, minTick=0.001))
            return out
        return [SimpleNamespace(minTick=0.01, longName=c.symbol, secIdList=[], contract=c)]

    def _bond_row(self, con_id):
        return next((r for r in self.GILTS if r[0] == con_id), None)

    def accountValues(self, acct):
        return [SimpleNamespace(tag="TotalCashValue", currency="GBP", value=str(self.cash))]

    def positions(self, acct):
        return [SimpleNamespace(contract=c, position=q, avgCost=a) for c, q, a in self.pos.values() if q]

    def external_trade(self, contract, qty, px):
        """Someone trades the account outside ATLAS."""
        c, q, a = self.pos.get(contract.conId, [contract, 0.0, px])
        self.pos[contract.conId] = [c, q + qty, a]

    def _fill(self, contract, order, qty):
        sym = contract.symbol + ".L"
        px = self.prices[sym] * (1 + (self.price_move if order.action == "BUY" else -self.price_move))
        unit = 0.01 if sym in PENCE else 1.0
        sign = 1 if order.action == "BUY" else -1
        self.cash -= sign * qty * px * unit + 3.0
        c, q, a = self.pos.get(contract.conId, [contract, 0.0, 0.0])
        nq = q + sign * qty
        self.pos[contract.conId] = [c, nq, (a * q + px * qty) / nq if sign > 0 and nq else a]
        return px

    def _place_bond(self, contract, order, row):
        from atlas.gilts import Gilt, accrued, settlement
        from datetime import date as _date
        nominal = order.totalQuantity
        clean = row[5]
        ok = order.orderType == "LMT" and (clean <= order.lmtPrice if order.action == "BUY" else clean >= order.lmtPrice)
        if self.fill_mode == "none" or not ok:
            st = IBOrderStatus(orderId=order.orderId, status="Submitted", filled=0, remaining=nominal, avgFillPrice=0)
            fills = []
        else:
            g = Gilt(row[2], row[3], _date(int(row[4][:4]), int(row[4][4:6]), int(row[4][6:])))
            acc = accrued(g, settlement(_date.today()))
            sign = 1 if order.action == "BUY" else -1
            self.cash -= sign * nominal * (clean + acc) / 100.0 + 1.0
            c, q, a = self.pos.get(contract.conId, [contract, 0.0, 0.0])
            self.pos[contract.conId] = [c, q + sign * nominal, clean * 100 / 100]
            st = IBOrderStatus(orderId=order.orderId, status="Filled", filled=nominal, remaining=0, avgFillPrice=clean)
            fills = [SimpleNamespace(commissionReport=SimpleNamespace(commission=1.0))]
        t = Trade(contract=contract, order=order, orderStatus=st, fills=fills, log=[])
        self._trades.append(t)
        return t

    def placeOrder(self, contract, order):
        row = self._bond_row(contract.conId) if getattr(contract, "secType", "") == "BOND" else None
        if row is not None:
            order.orderId = self.next_id
            order.permId = 90000 + self.next_id
            self.next_id += 1
            return self._place_bond(contract, order, row)
        order.orderId = self.next_id
        order.permId = 90000 + self.next_id
        self.next_id += 1
        qty = order.totalQuantity
        sym = contract.symbol + ".L"
        market_px = self.prices[sym] * (1 + (self.price_move if order.action == "BUY" else -self.price_move))
        limit_ok = True
        if getattr(order, "orderType", "MKT") == "LMT":
            limit_ok = market_px <= order.lmtPrice if order.action == "BUY" else market_px >= order.lmtPrice
        if self.fill_mode == "none" or not limit_ok:
            st = IBOrderStatus(orderId=order.orderId, status="Submitted", filled=0, remaining=qty, avgFillPrice=0)
            fills = []
        elif self.fill_mode == "partial":
            part = max(1, int(qty // 2))
            px = self._fill(contract, order, part)
            st = IBOrderStatus(orderId=order.orderId, status="Submitted", filled=part, remaining=qty - part, avgFillPrice=px)
            fills = [SimpleNamespace(commissionReport=SimpleNamespace(commission=3.0))]
        else:
            px = self._fill(contract, order, qty)
            st = IBOrderStatus(orderId=order.orderId, status="Filled", filled=qty, remaining=0, avgFillPrice=px)
            fills = [SimpleNamespace(commissionReport=SimpleNamespace(commission=3.0))]
        t = Trade(contract=contract, order=order, orderStatus=st, fills=fills, log=[])
        self._trades.append(t)
        if self.drop_after_accept:
            self.connected = False
            raise ConnectionResetError("connection lost after order was sent")
        return t

    def trades(self): return self._trades
    def openTrades(self): return [t for t in self._trades if t.orderStatus.status not in ("Filled", "Cancelled")]
    def reqCompletedOrders(self, apiOnly): return []

    def cancelOrder(self, o):
        for t in self._trades:
            if t.order.orderId == o.orderId and t.orderStatus.status != "Filled":
                t.orderStatus.status = "Cancelled"

    def reqGlobalCancel(self):
        for t in self.openTrades():
            t.orderStatus.status = "Cancelled"


def chaos_for(db, **kw):
    from atlas import data
    ref, _ = data.latest_prices_gbp(db)
    return ChaosIB(prices={s: (p * 100 if s in PENCE else p) for s, p in ref.items()}, **kw)
