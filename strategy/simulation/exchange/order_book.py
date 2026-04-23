from dataclasses import dataclass
from typing import List, Optional
import itertools

_timestamp_counter = itertools.count()


def next_timestamp() -> int:
    return next(_timestamp_counter)


@dataclass
class Order:
    server_id: int
    client_order_id: int
    team: str
    symbol: str
    side: str
    price: int
    size: int
    original_size: int
    timestamp: int

    def __hash__(self):
        return self.server_id

    def __eq__(self, other):
        return isinstance(other, Order) and self.server_id == other.server_id


@dataclass
class Trade:
    symbol: str
    price: int
    size: int
    buy_order: Order
    sell_order: Order
    aggressor_side: str


class OrderBook:
    """Continuous limit order book with price-time priority."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        # buys: best (highest price, earliest time) at index 0
        # sells: best (lowest price, earliest time) at index 0
        self.buys: List[Order] = []
        self.sells: List[Order] = []

    @staticmethod
    def _buy_key(order: Order):
        return (-order.price, order.timestamp)

    @staticmethod
    def _sell_key(order: Order):
        return (order.price, order.timestamp)

    def add(self, order: Order) -> List[Trade]:
        trades: List[Trade] = []

        if order.side == "BUY":
            while order.size > 0 and self.sells:
                best = self.sells[0]
                if best.price > order.price:
                    break
                match_size = min(order.size, best.size)
                trade_price = best.price
                trades.append(Trade(
                    symbol=self.symbol,
                    price=trade_price,
                    size=match_size,
                    buy_order=order,
                    sell_order=best,
                    aggressor_side="BUY",
                ))
                order.size -= match_size
                best.size -= match_size
                if best.size == 0:
                    self.sells.pop(0)
            if order.size > 0:
                self._insert_sorted(self.buys, order, self._buy_key)
        else:
            while order.size > 0 and self.buys:
                best = self.buys[0]
                if best.price < order.price:
                    break
                match_size = min(order.size, best.size)
                trade_price = best.price
                trades.append(Trade(
                    symbol=self.symbol,
                    price=trade_price,
                    size=match_size,
                    buy_order=best,
                    sell_order=order,
                    aggressor_side="SELL",
                ))
                order.size -= match_size
                best.size -= match_size
                if best.size == 0:
                    self.buys.pop(0)
            if order.size > 0:
                self._insert_sorted(self.sells, order, self._sell_key)

        return trades

    def remove(self, order: Order) -> bool:
        book = self.buys if order.side == "BUY" else self.sells
        for i, existing in enumerate(book):
            if existing.server_id == order.server_id:
                book.pop(i)
                return True
        return False

    @staticmethod
    def _insert_sorted(book: List[Order], order: Order, key_fn):
        key = key_fn(order)
        lo, hi = 0, len(book)
        while lo < hi:
            mid = (lo + hi) // 2
            if key_fn(book[mid]) < key:
                lo = mid + 1
            else:
                hi = mid
        book.insert(lo, order)

    def best_bid(self) -> Optional[Order]:
        return self.buys[0] if self.buys else None

    def best_ask(self) -> Optional[Order]:
        return self.sells[0] if self.sells else None

    def snapshot(self, depth: int):
        return {
            "buy": self._aggregate(self.buys, depth),
            "sell": self._aggregate(self.sells, depth),
        }

    @staticmethod
    def _aggregate(orders: List[Order], depth: int):
        levels = []
        i = 0
        while i < len(orders) and len(levels) < depth:
            price = orders[i].price
            size = 0
            while i < len(orders) and orders[i].price == price:
                size += orders[i].size
                i += 1
            levels.append([price, size])
        return levels

    def clear(self):
        self.buys.clear()
        self.sells.clear()
