#!/usr/bin/python

# ~~~~~==============   HOW TO RUN   ==============~~~~~
# 1) Configure things in CONFIGURATION section
# 2) Change permissions: chmod +x bot.py
# 3) Run in loop: while true; do ./bot.py; sleep 1; done

from __future__ import print_function
from enum import Enum

import sys
import socket
import json

# ~~~~~============== CONFIGURATION  ==============~~~~~
# replace REPLACEME with your team name!
team_name = "nyam"
# This variable dictates whether or not the bot is connecting to the prod
# or test exchange. Be careful with this switch!
test_mode = False

# This setting changes which test exchange is connected to.
# 0 is prod-like
# 1 is slower
# 2 is empty
test_exchange_index = 0
prod_exchange_hostname = "localhost"

port = 25000 + (test_exchange_index if test_mode else 0)
exchange_hostname = "test-exch-" + team_name if test_mode else prod_exchange_hostname

# position limits
SYMBOLS = ["BOND", "VALBZ", "VALE", "GS", "MS", "WFC", "XLF"]
POSITION_LIMIT = {
    "BOND": 100,
    "VALBZ": 10,
    "VALE": 10,
    "GS": 100,
    "MS": 100,
    "WFC": 100,
    "XLF": 100
}

# Market Making
MM_SYMBOLS = ["GS", "MS", "WFC", "XLF"] # except VALBZ and VALE
MM_WINDOW_RATIO = 0.003
HARD_LIMIT  = {"GS": 50, "MS": 50, "WFC": 50, "XLF": 60}
SKEW_K = 0.4
mm_orders = {} # (symbol, side) -> order_id

# maximum number of in-flight orders
MAX_ORDERS = 100

# order ratio
BOND_order_ratio = 0.05
MM_order_ratio = 0.1

# FEE
VALE_FEE = 10

# wallet
position = {s: 0 for s in SYMBOLS}
inflight_sell = {s: 0 for s in SYMBOLS}
inflight_buy = {s: 0 for s in SYMBOLS}
cash = 0

# fair price
fair = {}

# order books
best_bid = {}
best_ask = {}

# in-flight orders: order_id -> (symbol, dir, remaining_size)
orders = {}

# pending converts: order_id -> (symbol, dir, size)
converts = {}


# ~~~~~============== NETWORKING CODE ==============~~~~~
def connect():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((exchange_hostname, port))
    return s.makefile('rw', 1)

def disconnect(exchange):
    exchange.close()

def write_to_exchange(exchange, obj):
    json.dump(obj, exchange)
    exchange.write("\n")

def read_from_exchange(exchange):
    return json.loads(exchange.readline())

# ~~~~~============== TRADING HELPERS ==============~~~~~

order_id = 0

def place_order(exchange, symbol, dir, price, size):
    global order_id
    order_id += 1
    orders[order_id] = (symbol, dir, size)
    write_to_exchange(exchange, {"type": "add", "order_id": order_id, "symbol": symbol, "dir": dir, "price": int(price), "size": int(size)})
    return order_id

def cancel_order(exchange, oid):
    write_to_exchange(exchange, {"type": "cancel", "order_id": oid})

def convert(exchange, symbol, dir, size):
    if symbol == "XLF" and size % 10 != 0:
        print("XLF can only be converted in multiples of 10.", file=sys.stderr)
        return -1
    global order_id
    order_id += 1
    converts[order_id] = (symbol, dir, size)
    write_to_exchange(exchange, {"type": "convert", "order_id": order_id, "symbol": symbol, "dir": dir, "size": int(size)})
    return order_id

# ~~~~~============== TRADING FUNCTIONS ==============~~~~~

def trade_bond(exchange):
    global position, inflight_buy, inflight_sell
    bond_position = position["BOND"]
    limit = POSITION_LIMIT["BOND"]
    bond_inflight_buy = inflight_buy["BOND"]
    bond_inflight_sell = inflight_sell["BOND"]
    make_buy  = min(MAX_ORDERS * BOND_order_ratio - bond_inflight_buy,  limit - bond_position)
    make_sell = min(MAX_ORDERS * BOND_order_ratio - bond_inflight_sell, limit + bond_position)
    if make_buy > 0:
        place_order(exchange, "BOND", "BUY", 999, make_buy)
        inflight_buy["BOND"] += make_buy
    if make_sell > 0:
        place_order(exchange, "BOND", "SELL", 1001, make_sell)
        inflight_sell["BOND"] += make_sell

# vale_low  = best_ask["VALE"]  (ask: price to BUY VALE)
# vale_high = best_bid["VALE"]  (bid: price received when SELLING VALE)
# valbz_low  = best_ask["VALBZ"]
# valbz_high = best_bid["VALBZ"]
def trade_adr(exchange, dir):
    global best_ask, best_bid, position

    vale_low   = best_ask.get("VALE")
    vale_high  = best_bid.get("VALE")
    valbz_low  = best_ask.get("VALBZ")
    valbz_high = best_bid.get("VALBZ")

    if not vale_high or not vale_low or not valbz_high or not valbz_low:
        return

    if valbz_high[0] - vale_low[0] - VALE_FEE > 0: # CASE 1: VALBZ more expensive
        if dir == "BUY":
            size = min(
                vale_low[1],
                valbz_high[1],
                POSITION_LIMIT["VALE"]  - position["VALE"]  - inflight_buy["VALE"],
                POSITION_LIMIT["VALBZ"] + position["VALBZ"] - inflight_sell["VALBZ"]  # fix: was inflight_buy
            )
            if size > 0:
                place_order(exchange, "VALE", "BUY", vale_low[0], size)
                inflight_buy["VALE"] += size
        elif dir == "SELL":
            size = position["VALE"]
            if size <= 0:
                return
            size_sell = max(0, min(
                valbz_high[1],
                POSITION_LIMIT["VALBZ"] + position["VALBZ"] - inflight_sell["VALBZ"]
            ))
            if size_sell >= size:
                place_order(exchange, "VALBZ", "SELL", valbz_high[0], size)
                inflight_sell["VALBZ"] += size
            elif 0 < size_sell < size:  # fix: was `size_sell < size` (allowed size_sell=0)
                place_order(exchange, "VALBZ", "SELL", valbz_high[0], size_sell)
                inflight_sell["VALBZ"] += size_sell
                size_remain = size - size_sell
                place_order(exchange, "VALE", "SELL", vale_high[0], size_remain)
                inflight_sell["VALE"] += size_remain

    elif vale_high[0] - valbz_low[0] - VALE_FEE > 0: # CASE 2: VALE more expensive
        if dir == "BUY":
            size = min(
                valbz_low[1],
                vale_high[1],
                POSITION_LIMIT["VALBZ"] - position["VALBZ"] - inflight_buy["VALBZ"],
                POSITION_LIMIT["VALE"]  + position["VALE"]  - inflight_sell["VALE"]   # fix: was inflight_buy
            )
            if size > 0:
                place_order(exchange, "VALBZ", "BUY", valbz_low[0], size)
                inflight_buy["VALBZ"] += size
        elif dir == "SELL":
            size = position["VALBZ"]
            if size <= 0:
                return
            size_sell = max(0, min(
                vale_high[1],
                POSITION_LIMIT["VALE"] + position["VALE"] - inflight_sell["VALE"]
            ))
            if size_sell >= size:
                place_order(exchange, "VALE", "SELL", vale_high[0], size)
                inflight_sell["VALE"] += size
            elif 0 < size_sell < size:  # fix: was `size_sell < size`
                place_order(exchange, "VALE", "SELL", vale_high[0], size_sell)
                inflight_sell["VALE"] += size_sell
                size_remain = size - size_sell
                place_order(exchange, "VALBZ", "SELL", valbz_high[0], size_remain)
                inflight_sell["VALBZ"] += size_remain

    else:
        # no arb: unwind any remaining long positions
        if dir == "SELL":
            size_valbz = max(0, position["VALBZ"] - inflight_sell["VALBZ"])  # fix: subtract inflight
            size_vale  = max(0, position["VALE"]  - inflight_sell["VALE"])   # fix: subtract inflight
            if size_valbz > 0:
                place_order(exchange, "VALBZ", "SELL", valbz_high[0], size_valbz)
                inflight_sell["VALBZ"] += size_valbz
            if size_vale > 0:
                place_order(exchange, "VALE", "SELL", vale_high[0], size_vale)
                inflight_sell["VALE"] += size_vale


# ~~~~~============== MARKET MAKING ==============~~~~~

def replace_quote(exchange, symbol, side, price):
    key = (symbol, side)
    old_id = mm_orders.get(key)
    if old_id:
        cancel_order(exchange, old_id)
    new_id = place_order(exchange, symbol, side, price, int(MAX_ORDERS * MM_order_ratio))
    mm_orders[key] = new_id

def cancel_quote(exchange, symbol, side):
    key = (symbol, side)
    if mm_orders.get(key) is None:
        return
    old_id = mm_orders.pop(key)
    if old_id:
        cancel_order(exchange, old_id)
        mm_orders[key] = None

def calc_skewed_delta(symbol, base_d):
    limit = HARD_LIMIT[symbol]
    norm = max(-1.0, min(1.0, position[symbol] / limit))
    bid_d = max(1, int(base_d * (1 - SKEW_K * norm)))
    ask_d = max(1, int(base_d * (1 + SKEW_K * norm)))
    return bid_d, ask_d

def place_mm(exchange, symbol):
    global position

    fair_price = fair.get(symbol)
    if not fair_price:
        b = best_bid.get(symbol)
        a = best_ask.get(symbol)
        if b and a:
            fair_price = (b[0] + a[0]) // 2
    if not fair_price:
        return

    delta = max(2, int(fair_price * MM_WINDOW_RATIO))
    bid_delta, ask_delta = calc_skewed_delta(symbol, delta)

    bid = max(1, int(fair_price - bid_delta))
    ask = max(bid + 1, int(fair_price + ask_delta))

    hard_limit = HARD_LIMIT[symbol]
    sym_position = position[symbol]
    mm_size = int(MAX_ORDERS * MM_order_ratio)

    if sym_position + mm_size <= hard_limit:
        replace_quote(exchange, symbol, "BUY", bid)
    else:
        cancel_quote(exchange, symbol, "BUY")

    if sym_position - mm_size >= -hard_limit:
        replace_quote(exchange, symbol, "SELL", ask)
    else:
        cancel_quote(exchange, symbol, "SELL")


# ~~~~~============== MAIN LOOP ==============~~~~~

def main():
    global cash

    exchange = connect()
    write_to_exchange(exchange, {"type": "hello", "team": team_name.upper()})

    while True:
        message = read_from_exchange(exchange)
        t = message["type"]

        if t == "close":
            print("The round has ended.", file=sys.stderr)
            break
        elif t == "hello":
            print("The exchange replied:", message, file=sys.stderr)
            ############## trade_bond(exchange)
        elif t == "book":
            symbol = message["symbol"]
            buys  = message.get("buy")  or []
            sells = message.get("sell") or []
            best_bid[symbol] = (buys[0][0],  buys[0][1])  if buys  else None
            best_ask[symbol] = (sells[0][0], sells[0][1]) if sells else None
            # if symbol in ["VALBZ", "VALE"]:
            #     trade_adr(exchange, "BUY")
        elif t == "trade":
            fair[message["symbol"]] = message["price"]
            if message["symbol"] in MM_SYMBOLS:
                place_mm(exchange, message["symbol"])
        elif t == "fill":
            symbol = message["symbol"]
            size   = message["size"]
            price  = message["price"]
            dir    = message["dir"]

            # update wallet
            if dir == "BUY":
                position[symbol] += size
                cash -= price * size
                inflight_buy[symbol] -= size
            elif dir == "SELL":
                position[symbol] -= size
                cash += price * size
                inflight_sell[symbol] -= size

            # track remaining size for out handler
            oid = message["order_id"]
            if oid in orders:
                sym, d, remaining = orders[oid]
                remaining -= size
                if remaining <= 0:
                    orders.pop(oid)
                else:
                    orders[oid] = (sym, d, remaining)

            if symbol == "BOND":
                ############## trade_bond(exchange)
                pass
            # elif symbol == "VALE":
            #     if dir == "BUY":
            #         trade_adr(exchange, "SELL")
            #     elif dir == "SELL":
            #         # fix: only convert when we are short (not when unwinding a long)
            #         if position["VALE"] < 0:
            #             convert(exchange, "VALE", "BUY", size)
            # elif symbol == "VALBZ":
            #     if dir == "BUY":
            #         trade_adr(exchange, "SELL")
            #     elif dir == "SELL":
            #         if position["VALBZ"] < 0:
            #             convert(exchange, "VALE", "SELL", size)
            else:
                place_mm(exchange, symbol)
                print("trade:", message, file=sys.stderr)

        elif t == "ack":
            oid = message["order_id"]
            if oid in converts:
                sym, dir, size = converts.pop(oid)
                if sym == "VALE":
                    if dir == "SELL":   # give VALE, get VALBZ
                        position["VALE"]  -= size
                        position["VALBZ"] += size
                    elif dir == "BUY":  # give VALBZ, get VALE
                        position["VALE"]  += size
                        position["VALBZ"] -= size
                    cash -= VALE_FEE * size

        elif t == "out":
            oid = message["order_id"]
            if oid in orders:
                sym, dir, remaining = orders.pop(oid)
                # decrement inflight by whatever was not filled
                if dir == "BUY":
                    inflight_buy[sym]  -= remaining
                else:
                    inflight_sell[sym] -= remaining
            for key, val in mm_orders.items():
                if val == oid:
                    mm_orders[key] = None

        elif t == "reject":
            print("reject:", message, file=sys.stderr)
        elif t == "error":
            print("error:", message, file=sys.stderr)

    disconnect(exchange)

if __name__ == "__main__":
    assert team_name != "REPLAC" + "EME", "Please put your team name in the variable [team_name]."
    main()
