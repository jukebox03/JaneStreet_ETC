#!/usr/bin/env python3
"""Team bot skeleton. 전략은 TeamBot의 핸들러 안에서 직접 구현.

Usage:
    python3 strategy/bot.py                          # localhost:25000 (default)
    python3 strategy/bot.py --address localhost:25001
"""

import argparse
import json
import socket
import time
from collections import deque

# ~~~~~============== CONFIGURATION ==============~~~~~
TEAM_NAME = "nyam"

SYMBOLS = ["BOND", "GS", "MS", "WFC", "VALBZ", "VALE", "XLF"]

POSITION_LIMITS = {
    "BOND": 100, "GS": 100, "MS": 100, "WFC": 100,
    "VALBZ": 10, "VALE": 10, "XLF": 100,
}


# ~~~~~============== STRATEGY ==============~~~~~

class TeamBot:
    def __init__(self, exchange):
        self.ex = exchange
        self.position = {s: 0 for s in SYMBOLS}
        self.cash = 0
        self.best_bid = {}  # sym -> (price, size)
        self.best_ask = {}  # sym -> (price, size)

    # --------------- message handlers ---------------
    def on_hello(self, msg):
        self.cash = msg.get("cash", 0)
        for entry in msg.get("symbols", []):
            self.position[entry["symbol"]] = entry["position"]

    def on_trade(self, msg):
        # TODO: 체결 가격으로 fair 업데이트 / 전략 트리거
        pass

    def on_book(self, msg):
        sym = msg["symbol"]
        buys = msg.get("buy") or []
        sells = msg.get("sell") or []
        self.best_bid[sym] = (buys[0][0], buys[0][1]) if buys else None
        self.best_ask[sym] = (sells[0][0], sells[0][1]) if sells else None

    def on_fill(self, msg):
        sym = msg["symbol"]
        d = msg["dir"]
        price = msg["price"]
        size = msg["size"]
        if d == "BUY":
            self.position[sym] += size
            self.cash -= price * size
        else:
            self.position[sym] -= size
            self.cash += price * size

    def on_out(self, msg):
        # TODO: 주문 종료/취소된 order_id 정리
        pass

    def on_close(self, msg):
        print("[bot] round closed, resetting")
        self.position = {s: 0 for s in SYMBOLS}
        self.cash = 0


# ~~~~~============== EXCHANGE CONNECTION ==============~~~~~

class ExchangeConnection:
    def __init__(self, host, port):
        self.timestamps = deque(maxlen=500)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(15)
        s.connect((host, port))
        self.reader = s.makefile("r", 1)
        self.writer = s
        self._write({"type": "hello", "team": TEAM_NAME.upper()})

    def read(self):
        line = self.reader.readline()
        if not line:
            raise ConnectionError("exchange closed")
        return json.loads(line)

    def send_add(self, oid, sym, side, price, size):
        self._write({"type": "add", "order_id": oid, "symbol": sym,
                     "dir": side, "price": int(price), "size": int(size)})

    def send_cancel(self, oid):
        self._write({"type": "cancel", "order_id": oid})

    def send_convert(self, oid, sym, side, size):
        self._write({"type": "convert", "order_id": oid, "symbol": sym,
                     "dir": side, "size": int(size)})

    def _write(self, msg):
        data = (json.dumps(msg) + "\n").encode("utf-8")
        sent = 0
        while sent < len(data):
            n = self.writer.send(data[sent:])
            if n == 0:
                raise Exception("socket closed")
            sent += n
        now = time.time()
        self.timestamps.append(now)
        if len(self.timestamps) == self.timestamps.maxlen and self.timestamps[0] > now - 1:
            print("WARN: approaching 500 msg/s rate limit")


def parse_arguments():
    p = argparse.ArgumentParser()
    p.add_argument("--address", type=str, default="localhost:25000",
                   metavar="HOST:PORT",
                   help="exchange address (default: localhost:25000)")
    args = p.parse_args()
    host, port = args.address.split(":")
    args.host, args.port = host, int(port)
    return args


def main():
    args = parse_arguments()
    ex = ExchangeConnection(args.host, args.port)
    bot = TeamBot(ex)

    hello = ex.read()
    print("[bot] hello:", hello)
    bot.on_hello(hello)

    while True:
        try:
            msg = ex.read()
        except (ConnectionError, socket.timeout) as e:
            print(f"[bot] connection ended: {e}")
            return

        t = msg.get("type")
        if t == "trade":
            bot.on_trade(msg)
        elif t == "book":
            bot.on_book(msg)
        elif t == "fill":
            bot.on_fill(msg)
        elif t == "out":
            bot.on_out(msg)
        elif t == "close":
            bot.on_close(msg)
        elif t == "hello":
            bot.on_hello(msg)
        elif t in ("ack", "ok"):
            pass
        elif t == "reject":
            err = msg.get("error", "")
            if err != "unknown order_id":
                print(f"[bot] reject: {msg}")
        elif t == "error":
            print(f"[bot] error: {msg}")


if __name__ == "__main__":
    main()
