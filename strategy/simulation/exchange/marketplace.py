import itertools
import queue
import random
import threading
import time
from typing import Dict, Optional

from .config import SYMBOLS, MARKETPLACE_TICK_INTERVAL
from .market import MarketEngine
from .exchange import Exchange


class MarketplaceBot:
    """Simulates the exchange's background liquidity providers & takers."""

    NUM_BOTS = 8
    NUM_MAKERS = 6  # first N are makers; rest are takers-only

    def __init__(self, exchange: Exchange, market: MarketEngine,
                 activity: float = 1.0, seed: Optional[int] = None):
        self.exchange = exchange
        self.market = market
        self.activity = activity
        self._rng = random.Random(seed)
        self.running = False
        self._oid_counter = itertools.count(900_000_000)
        self._teams = [f"_MP{i}" for i in range(self.NUM_BOTS)]
        self._maker_teams = self._teams[:self.NUM_MAKERS]
        self._taker_teams = self._teams
        self._maker_orders: Dict[str, Dict[str, Dict[str, Optional[int]]]] = {
            team: {sym: {"BUY": None, "SELL": None} for sym in SYMBOLS}
            for team in self._teams
        }
        self._next_refresh: Dict[str, float] = {team: 0.0 for team in self._teams}
        self._msg_queue: "queue.Queue[tuple]" = queue.Queue()

    def start(self):
        if self.activity <= 0:
            return
        self.running = True
        for team in self._teams:
            self.exchange.register_team(team, self._make_send_fn(team))
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.running = False

    def reset(self):
        # Called after a round reset
        for team in self._teams:
            for sym in SYMBOLS:
                self._maker_orders[team][sym] = {"BUY": None, "SELL": None}
            self._next_refresh[team] = 0.0
        while not self._msg_queue.empty():
            try:
                self._msg_queue.get_nowait()
            except queue.Empty:
                break

    def _make_send_fn(self, team: str):
        def send(msg: dict):
            self._msg_queue.put((team, msg))
        return send

    def _run(self):
        while self.running:
            try:
                self._tick()
            except Exception:
                import traceback
                traceback.print_exc()
            interval = MARKETPLACE_TICK_INTERVAL / max(0.2, self.activity)
            time.sleep(interval)

    def _tick(self):
        self._drain_msgs()
        self._refresh_makers()
        self._send_taker_flow()
        self._send_noise()

    def _drain_msgs(self):
        while True:
            try:
                team, msg = self._msg_queue.get_nowait()
            except queue.Empty:
                return
            mtype = msg.get("type")
            if mtype == "out":
                oid = msg.get("order_id")
                for sym_dict in self._maker_orders[team].values():
                    for side in ("BUY", "SELL"):
                        if sym_dict[side] == oid:
                            sym_dict[side] = None

    def _refresh_makers(self):
        t = time.monotonic()
        for team in self._maker_teams:
            if t < self._next_refresh[team]:
                continue
            self._next_refresh[team] = t + self._rng.uniform(3.0, 8.0)
            for symbol in SYMBOLS:
                self._refresh_pair(team, symbol)

    def _refresh_pair(self, team: str, symbol: str):
        fair = self.market.state.get_fair(symbol)
        spread = self._spread_for(symbol)
        jitter = self._rng.uniform(-1.0, 1.0)
        buy_price = max(1, int(round(fair - spread + jitter)))
        sell_price = max(buy_price + 1, int(round(fair + spread + jitter)))
        size = self._rng.randint(1, 5)
        if symbol == "BOND":
            size = self._rng.randint(3, 10)

        for side, price in (("BUY", buy_price), ("SELL", sell_price)):
            old_oid = self._maker_orders[team][symbol][side]
            if old_oid is not None:
                self.exchange.handle_message(team, {"type": "cancel", "order_id": old_oid})
                self._maker_orders[team][symbol][side] = None
            new_oid = next(self._oid_counter)
            self._maker_orders[team][symbol][side] = new_oid
            self.exchange.handle_message(team, {
                "type": "add",
                "order_id": new_oid,
                "symbol": symbol,
                "dir": side,
                "price": price,
                "size": size,
            })

    def _send_taker_flow(self):
        liquidity = self.market.state.liquidity
        p = 0.55 * self.activity * liquidity
        if self._rng.random() > p:
            return

        # Weight symbols so each gets reasonable volume
        symbol = self._rng.choices(
            SYMBOLS,
            weights=[1.4, 1.2, 1.2, 1.2, 0.9, 0.9, 1.4],
        )[0]

        team = self._rng.choice(self._taker_teams)
        fair = self.market.state.get_fair(symbol)

        bias = self.market.state.bias.get(symbol, 0.0)
        # bias > 0 → more buys, < 0 → more sells
        p_buy = 0.5 + max(-0.4, min(0.4, bias))
        direction = "BUY" if self._rng.random() < p_buy else "SELL"

        size = self._rng.randint(1, 4)
        book = self.exchange.order_books[symbol]

        if direction == "BUY":
            best = book.best_ask()
            price = best.price if best else max(1, int(round(fair + 3)))
        else:
            best = book.best_bid()
            price = best.price if best else max(1, int(round(fair - 3)))

        oid = next(self._oid_counter)
        self.exchange.handle_message(team, {
            "type": "add",
            "order_id": oid,
            "symbol": symbol,
            "dir": direction,
            "price": max(1, price),
            "size": size,
        })

    def _send_noise(self):
        if self._rng.random() > 0.07 * self.activity:
            return
        symbol = self._rng.choice(SYMBOLS)
        team = self._rng.choice(self._taker_teams)
        direction = self._rng.choice(["BUY", "SELL"])
        size = self._rng.randint(1, 2)
        fair = self.market.state.get_fair(symbol)
        if direction == "BUY":
            price = max(1, int(round(fair + self._rng.uniform(3, 10))))
        else:
            price = max(1, int(round(fair - self._rng.uniform(3, 10))))
        oid = next(self._oid_counter)
        self.exchange.handle_message(team, {
            "type": "add",
            "order_id": oid,
            "symbol": symbol,
            "dir": direction,
            "price": price,
            "size": size,
        })

    def _spread_for(self, symbol: str) -> float:
        if symbol == "BOND":
            return self._rng.uniform(2, 4)
        if symbol in ("VALE", "XLF"):
            return self._rng.uniform(4, 10)
        return self._rng.uniform(3, 8)
