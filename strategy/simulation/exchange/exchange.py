import itertools
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from .order_book import OrderBook, Order, Trade, next_timestamp
from .config import (
    SYMBOLS, POSITION_LIMITS, MARKETPLACE_POSITION_LIMIT, XLF_BASKET,
    VALE_CONVERT_FEE_PER_SHARE, XLF_CONVERT_FEE_PER_10, BOOK_DEPTH,
)


def _is_marketplace_team(team: str) -> bool:
    return team.startswith("_MP")


@dataclass
class Session:
    team: str
    send_fn: Callable[[dict], None]
    positions: Dict[str, int] = field(default_factory=lambda: {s: 0 for s in SYMBOLS})
    cash: int = 0
    active_orders: Dict[int, Order] = field(default_factory=dict)
    used_order_ids: set = field(default_factory=set)

    def pending_buy_size(self, symbol: str) -> int:
        return sum(o.size for o in self.active_orders.values()
                   if o.symbol == symbol and o.side == "BUY")

    def pending_sell_size(self, symbol: str) -> int:
        return sum(o.size for o in self.active_orders.values()
                   if o.symbol == symbol and o.side == "SELL")


class Exchange:
    def __init__(self, verbose: bool = False):
        self.order_books: Dict[str, OrderBook] = {s: OrderBook(s) for s in SYMBOLS}
        self.sessions: Dict[str, Session] = {}
        self.lock = threading.RLock()
        self._server_order_counter = itertools.count(1)
        self.round_active = True
        self.verbose = verbose

    def register_team(self, team: str, send_fn: Callable[[dict], None]) -> Session:
        with self.lock:
            if team in self.sessions:
                self.sessions[team].send_fn = send_fn
            else:
                self.sessions[team] = Session(team=team, send_fn=send_fn)
            return self.sessions[team]

    def send_hello(self, team: str):
        with self.lock:
            if team not in self.sessions:
                return
            session = self.sessions[team]
            msg = {
                "type": "hello",
                "cash": session.cash,
                "symbols": [
                    {"symbol": s, "position": session.positions[s]}
                    for s in SYMBOLS
                ],
            }
            self._send(team, msg)
            for symbol in SYMBOLS:
                self._send_book(symbol, only_team=team)

    def handle_message(self, team: str, msg: dict):
        with self.lock:
            if team not in self.sessions:
                return
            if not self.round_active:
                self._send(team, {"type": "error", "error": "round not active"})
                return
            msg_type = msg.get("type")
            if msg_type == "add":
                self._handle_add(team, msg)
            elif msg_type == "cancel":
                self._handle_cancel(team, msg)
            elif msg_type == "convert":
                self._handle_convert(team, msg)
            elif msg_type == "hello":
                self.send_hello(team)
            else:
                self._send(team, {"type": "error", "error": f"unknown message type: {msg_type}"})

    def _handle_add(self, team: str, msg: dict):
        order_id = msg.get("order_id")
        symbol = msg.get("symbol")
        direction = msg.get("dir")
        price = msg.get("price")
        size = msg.get("size")

        session = self.sessions[team]

        if not isinstance(order_id, int):
            return self._reject(team, order_id, "invalid order_id")
        if order_id in session.used_order_ids:
            return self._reject(team, order_id, "duplicate order_id")
        if symbol not in SYMBOLS:
            return self._reject(team, order_id, "unknown symbol")
        if direction not in ("BUY", "SELL"):
            return self._reject(team, order_id, "invalid dir")
        if not isinstance(price, int) or price <= 0:
            return self._reject(team, order_id, "invalid price")
        if not isinstance(size, int) or size <= 0:
            return self._reject(team, order_id, "invalid size")

        limit = MARKETPLACE_POSITION_LIMIT if _is_marketplace_team(team) else POSITION_LIMITS[symbol]
        if direction == "BUY":
            exposure = session.positions[symbol] + session.pending_buy_size(symbol) + size
            if exposure > limit:
                return self._reject(team, order_id, "would exceed long position limit")
        else:
            exposure = session.positions[symbol] - session.pending_sell_size(symbol) - size
            if exposure < -limit:
                return self._reject(team, order_id, "would exceed short position limit")

        order = Order(
            server_id=next(self._server_order_counter),
            client_order_id=order_id,
            team=team,
            symbol=symbol,
            side=direction,
            price=price,
            size=size,
            original_size=size,
            timestamp=next_timestamp(),
        )
        session.used_order_ids.add(order_id)
        session.active_orders[order_id] = order

        self._send(team, {"type": "ack", "order_id": order_id})

        trades = self.order_books[symbol].add(order)
        for trade in trades:
            self._process_trade(trade)

        if order.size == 0 and order_id in session.active_orders:
            session.active_orders.pop(order_id, None)
            self._send(team, {"type": "out", "order_id": order_id})

        self._send_book(symbol)

    def _process_trade(self, trade: Trade):
        symbol = trade.symbol
        price = trade.price
        size = trade.size
        buy_order = trade.buy_order
        sell_order = trade.sell_order

        buyer = self.sessions[buy_order.team]
        seller = self.sessions[sell_order.team]

        buyer.positions[symbol] += size
        buyer.cash -= price * size
        seller.positions[symbol] -= size
        seller.cash += price * size

        self._send(buy_order.team, {
            "type": "fill",
            "order_id": buy_order.client_order_id,
            "symbol": symbol,
            "dir": "BUY",
            "price": price,
            "size": size,
        })
        self._send(sell_order.team, {
            "type": "fill",
            "order_id": sell_order.client_order_id,
            "symbol": symbol,
            "dir": "SELL",
            "price": price,
            "size": size,
        })

        resting = sell_order if trade.aggressor_side == "BUY" else buy_order
        if resting.size == 0:
            resting_sess = self.sessions[resting.team]
            if resting.client_order_id in resting_sess.active_orders:
                resting_sess.active_orders.pop(resting.client_order_id, None)
                self._send(resting.team, {"type": "out", "order_id": resting.client_order_id})

        self._broadcast({
            "type": "trade",
            "symbol": symbol,
            "price": price,
            "size": size,
        })

    def _handle_cancel(self, team: str, msg: dict):
        order_id = msg.get("order_id")
        session = self.sessions[team]

        if order_id not in session.active_orders:
            return self._reject(team, order_id, "unknown order_id")

        order = session.active_orders.pop(order_id)
        self.order_books[order.symbol].remove(order)
        self._send(team, {"type": "out", "order_id": order_id})
        self._send_book(order.symbol)

    def _handle_convert(self, team: str, msg: dict):
        order_id = msg.get("order_id")
        symbol = msg.get("symbol")
        direction = msg.get("dir")
        size = msg.get("size")

        session = self.sessions[team]

        if not isinstance(order_id, int):
            return self._reject(team, order_id, "invalid order_id")
        if order_id in session.used_order_ids:
            return self._reject(team, order_id, "duplicate order_id")
        if direction not in ("BUY", "SELL"):
            return self._reject(team, order_id, "invalid dir")
        if not isinstance(size, int) or size <= 0:
            return self._reject(team, order_id, "invalid size")
        session.used_order_ids.add(order_id)

        is_mp = _is_marketplace_team(team)

        if symbol == "VALE":
            fee = VALE_CONVERT_FEE_PER_SHARE * size
            if direction == "SELL":
                new_vale = session.positions["VALE"] - size
                new_valbz = session.positions["VALBZ"] + size
            else:
                new_vale = session.positions["VALE"] + size
                new_valbz = session.positions["VALBZ"] - size
            if not is_mp:
                if abs(new_vale) > POSITION_LIMITS["VALE"]:
                    return self._reject(team, order_id, "VALE position limit")
                if abs(new_valbz) > POSITION_LIMITS["VALBZ"]:
                    return self._reject(team, order_id, "VALBZ position limit")
            session.positions["VALE"] = new_vale
            session.positions["VALBZ"] = new_valbz
            session.cash -= fee
            self._send(team, {"type": "ack", "order_id": order_id})

        elif symbol == "XLF":
            if size % 10 != 0:
                return self._reject(team, order_id, "XLF convert size must be multiple of 10")
            multiplier = size // 10
            fee = XLF_CONVERT_FEE_PER_10 * multiplier
            if direction == "SELL":
                new_xlf = session.positions["XLF"] - size
                new_positions = {
                    s: session.positions[s] + XLF_BASKET[s] * multiplier
                    for s in XLF_BASKET
                }
            else:
                new_xlf = session.positions["XLF"] + size
                new_positions = {
                    s: session.positions[s] - XLF_BASKET[s] * multiplier
                    for s in XLF_BASKET
                }
            if not is_mp:
                if abs(new_xlf) > POSITION_LIMITS["XLF"]:
                    return self._reject(team, order_id, "XLF position limit")
                for s, p in new_positions.items():
                    if abs(p) > POSITION_LIMITS[s]:
                        return self._reject(team, order_id, f"{s} position limit")
            session.positions["XLF"] = new_xlf
            for s, p in new_positions.items():
                session.positions[s] = p
            session.cash -= fee
            self._send(team, {"type": "ack", "order_id": order_id})
        else:
            return self._reject(team, order_id, f"cannot convert {symbol}")

    def _reject(self, team: str, order_id, error: str):
        self._send(team, {"type": "reject", "order_id": order_id, "error": error})

    def _send(self, team: str, msg: dict):
        session = self.sessions.get(team)
        if session is None:
            return
        try:
            session.send_fn(msg)
        except Exception:
            pass

    def _broadcast(self, msg: dict):
        for team in list(self.sessions.keys()):
            self._send(team, msg)

    def _send_book(self, symbol: str, only_team: Optional[str] = None):
        snapshot = self.order_books[symbol].snapshot(BOOK_DEPTH)
        msg = {"type": "book", "symbol": symbol, **snapshot}
        if only_team:
            self._send(only_team, msg)
        else:
            self._broadcast(msg)

    def close_round(self):
        with self.lock:
            self.round_active = False
            self._broadcast({"type": "close"})
            for book in self.order_books.values():
                book.clear()
            for session in self.sessions.values():
                session.active_orders.clear()

    def reset_round(self):
        with self.lock:
            self.round_active = True
            for session in self.sessions.values():
                session.positions = {s: 0 for s in SYMBOLS}
                session.cash = 0
                session.active_orders.clear()
                session.used_order_ids.clear()
            for book in self.order_books.values():
                book.clear()
            for team in list(self.sessions.keys()):
                self.send_hello(team)

    def pnl_snapshot(self, get_fair_fn) -> Dict[str, dict]:
        with self.lock:
            result = {}
            for team, session in self.sessions.items():
                if _is_marketplace_team(team):
                    continue
                pnl = session.cash
                for s in SYMBOLS:
                    pnl += session.positions[s] * get_fair_fn(s)
                result[team] = {
                    "cash": session.cash,
                    "pnl": pnl,
                    "positions": dict(session.positions),
                }
            return result
