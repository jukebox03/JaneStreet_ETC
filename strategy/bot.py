#!/usr/bin/python

# ~~~~~==============   HOW TO RUN   ==============~~~~~
# 1) Configure things in CONFIGURATION section
# 2) Change permissions: chmod +x bot.py
# 3) Run in loop: while true; do ./bot.py; sleep 1; done

from __future__ import print_function
from enum import Enum

import sys
import socket
import orjson

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
# MM_SYMBOLS = ["GS", "MS", "WFC", "XLF"] # except VALBZ and VALE
# MM_WINDOW_RATIO = 0.003
# HARD_LIMIT  = {"GS": 50, "MS": 50, "WFC": 50, "XLF": 60}
# SKEW_K = 0.4
# mm_orders = {} # (symbol, side) -> order_id

# maximum number of in-flight orders
MAX_ORDERS = 100

# order ratio
# MM_order_ratio = 0.1

# bond slot
BOND_SLOTS = 5

# FEE
VALE_FEE = 10

# ARB PROFIT THRESHOLD
MIN_ARB_PROFIT = 0

# wallet
position = {s: 0 for s in SYMBOLS}
inflight_sell = {s: 0 for s in SYMBOLS}
inflight_buy = {s: 0 for s in SYMBOLS}
cash = 0

# order books
best_bid = {}
best_ask = {}

# fair value
fair = {}

# in-flight orders: order_id -> (symbol, dir, remaining_size)
orders = {}

# pending converts: order_id -> (symbol, dir, size)
converts = {}

# arb
# bid/sid -> { buy_id, sell_id, size, buy_filled, sell_filled, converted, case }
arb = {}

# ~~~~~============== NETWORKING CODE ==============~~~~~
def connect():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((exchange_hostname, port))
    return s.makefile('rw', 1)

def disconnect(exchange):
    exchange.close()

def write_to_exchange(exchange, obj):
    exchange.write(orjson.dumps(obj).decode())
    exchange.write("\n")

def read_from_exchange(exchange):
    return orjson.loads(exchange.readline())

# ~~~~~============== TRADING HELPERS ==============~~~~~

_oid = 0

def place_order(exchange, symbol, dir, price, size):
    global _oid
    _oid += 1
    orders[_oid] = (symbol, dir, size)
    write_to_exchange(exchange, {"type": "add", "order_id": _oid, "symbol": symbol, "dir": dir, "price": int(price), "size": int(size)})
    return _oid

def cancel_order(exchange, oid):
    write_to_exchange(exchange, {"type": "cancel", "order_id": oid})

def convert(exchange, symbol, dir, size):
    if symbol == "XLF" and size % 10 != 0:
        print("XLF can only be converted in multiples of 10.", file=sys.stderr)
        return -1
    global _oid
    _oid += 1
    converts[_oid] = (symbol, dir, size)
    write_to_exchange(exchange, {"type": "convert", "order_id": _oid, "symbol": symbol, "dir": dir, "size": int(size)})
    return _oid

# ~~~~~============== TRADING FUNCTIONS ==============~~~~~

def trade_bond(exchange):
    lim = POSITION_LIMIT["BOND"]
    pos = position["BOND"]
    make_buy = min(BOND_SLOTS - inflight_buy["BOND"], lim - pos)
    make_sell = min(BOND_SLOTS - inflight_sell["BOND"], lim + pos)
    if make_buy > 0:
        place_order(exchange, "BOND", "BUY", 999, make_buy)
        inflight_buy["BOND"] += make_buy
    if make_sell > 0:
        place_order(exchange, "BOND", "SELL", 1001, make_sell)
        inflight_sell["BOND"] += make_sell

def trade_adr(exchange):
    va = best_ask.get("VALE")
    vb = best_bid.get("VALE")
    bza = best_ask.get("VALBZ")
    bzb = best_bid.get("VALBZ")

    if not (va and vb and bza and bzb):
        return

    # cancel pairs whose arb condition has disappeared
    for oid, pair in list(arb.items()):
        if oid != pair["buy_id"]:
            continue
        if pair["case"] == 1: # buy VALE, short VALBZ
            still_arb = bzb[0] - va[0] - VALE_FEE > MIN_ARB_PROFIT
        else: # buy VALBZ, short VALE
            still_arb = vb[0] - bza[0] - VALE_FEE > MIN_ARB_PROFIT
        if not still_arb:
            if pair["buy_id"] in orders:
                cancel_order(exchange, pair["buy_id"])
            if pair["sell_id"] in orders:
                cancel_order(exchange, pair["sell_id"])

    # create new pairs
    if bzb[0] - va[0] - VALE_FEE > MIN_ARB_PROFIT:  # CASE 1: buy VALE, short VALBZ
        size = min(
            va[1],
            bzb[1],
            POSITION_LIMIT["VALE"]  - position["VALE"]  - inflight_buy["VALE"],
            POSITION_LIMIT["VALBZ"] + position["VALBZ"] - inflight_sell["VALBZ"],
        )
        if size > 0:
            bid = place_order(exchange, "VALE", "BUY",  va[0],  size)
            sid = place_order(exchange, "VALBZ", "SELL", bzb[0], size)
            inflight_buy["VALE"]  += size
            inflight_sell["VALBZ"] += size
            pair = { "buy_id": bid, "sell_id": sid, "buy_filled": 0, "sell_filled": 0, "converted": 0, "case": 1 }
            arb[bid] = arb[sid] = pair
    elif vb[0] - bza[0] - VALE_FEE > MIN_ARB_PROFIT:  # CASE 2: buy VALBZ, short VALE
        size = min(
            bza[1],
            vb[1],
            POSITION_LIMIT["VALBZ"] - position["VALBZ"] - inflight_buy["VALBZ"],
            POSITION_LIMIT["VALE"]  + position["VALE"]  - inflight_sell["VALE"],
        )
        if size > 0:
            bid  = place_order(exchange, "VALBZ", "BUY",  bza[0],  size)
            sid = place_order(exchange, "VALE",  "SELL", vb[0],  size)
            inflight_buy["VALBZ"]  += size
            inflight_sell["VALE"]  += size
            pair = {"buy_id": bid, "sell_id": sid, "size": size, "buy_filled": 0, "sell_filled": 0, "converted": 0, "case": 2}
            arb[bid] = arb[sid] = pair


# ~~~~~============== MARKET MAKING ==============~~~~~

# def replace_quote(exchange, symbol, side, price):
#     key = (symbol, side)
#     old_id = mm_orders.get(key)
#     if old_id:
#         cancel_order(exchange, old_id)
#     new_id = place_order(exchange, symbol, side, price, int(MAX_ORDERS * MM_order_ratio))
#     mm_orders[key] = new_id

# def cancel_quote(exchange, symbol, side):
#     key = (symbol, side)
#     if mm_orders.get(key) is None:
#         return
#     old_id = mm_orders.pop(key)
#     if old_id:
#         cancel_order(exchange, old_id)
#         mm_orders[key] = None

# def calc_skewed_delta(symbol, base_d):
#     limit = HARD_LIMIT[symbol]
#     norm = max(-1.0, min(1.0, position[symbol] / limit))
#     bid_d = max(1, int(base_d * (1 - SKEW_K * norm)))
#     ask_d = max(1, int(base_d * (1 + SKEW_K * norm)))
#     return bid_d, ask_d

# def place_mm(exchange, symbol):
#     global position

#     fair_price = fair.get(symbol)
#     if not fair_price:
#         b = best_bid.get(symbol)
#         a = best_ask.get(symbol)
#         if b and a:
#             fair_price = (b[0] + a[0]) // 2
#     if not fair_price:
#         return

#     delta = max(2, int(fair_price * MM_WINDOW_RATIO))
#     bid_delta, ask_delta = calc_skewed_delta(symbol, delta)

#     bid = max(1, int(fair_price - bid_delta))
#     ask = max(bid + 1, int(fair_price + ask_delta))

#     hard_limit = HARD_LIMIT[symbol]
#     sym_position = position[symbol]
#     mm_size = int(MAX_ORDERS * MM_order_ratio)

#     if sym_position + mm_size <= hard_limit:
#         replace_quote(exchange, symbol, "BUY", bid)
#     else:
#         cancel_quote(exchange, symbol, "BUY")

#     if sym_position - mm_size >= -hard_limit:
#         replace_quote(exchange, symbol, "SELL", ask)
#     else:
#         cancel_quote(exchange, symbol, "SELL")


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
            trade_bond(exchange)
        elif t == "book":
            symbol = message["symbol"]
            buys  = message.get("buy")  or []
            sells = message.get("sell") or []
            best_bid[symbol] = (buys[0][0],  buys[0][1])  if buys  else None
            best_ask[symbol] = (sells[0][0], sells[0][1]) if sells else None
            if symbol in ["VALBZ", "VALE"]:
                trade_adr(exchange)
        elif t == "trade":
            fair[message["symbol"]] = message["price"]
            # if message["symbol"] in MM_SYMBOLS:
            #     place_mm(exchange, message["symbol"])
        elif t == "fill": # our order was filled (fully or partially)
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

            # update orders
            order_id = message["order_id"]
            if order_id in orders:
                sym, d, remaining = orders[order_id]
                remaining -= size
                if remaining <= 0:
                    orders.pop(order_id)
                else:
                    orders[order_id] = (sym, d, remaining)

            if symbol == "BOND":
                trade_bond(exchange)
            elif symbol in ["VALBZ", "VALE"] and order_id in arb:
                pair = arb[order_id]
                if order_id == pair["buy_id"]:
                    pair["buy_filled"] += size
                else:
                    pair["sell_filled"] += size
                to_convert = min(pair["buy_filled"] - pair["converted"], pair["sell_filled"] - pair["converted"])
                if to_convert > 0:
                    conv_dir = "SELL" if pair["case"] == 1 else "BUY"
                    convert(exchange, "VALE", conv_dir, to_convert)
                    pair["converted"] += to_convert
            # else:
            #     place_mm(exchange, symbol)
            #     print("trade:", message, file=sys.stderr)

        elif t == "ack": # only for convert orders
            order_id = message["order_id"]
            if order_id in converts:
                sym, dir, size = converts.pop(order_id)
                if sym == "VALE":
                    if dir == "SELL":   # give VALE, get VALBZ
                        position["VALE"]  -= size
                        position["VALBZ"] += size
                    elif dir == "BUY":  # give VALBZ, get VALE
                        position["VALE"]  += size
                        position["VALBZ"] -= size
                    cash -= VALE_FEE * size

        elif t == "out": # order is no longer active (can be triggered by cancel or fill)
            order_id = message["order_id"]
            if order_id in orders:
                sym, dir, remaining = orders.pop(order_id)
                # decrement inflight by whatever was not filled
                if dir == "BUY":
                    inflight_buy[sym]  -= remaining
                else:
                    inflight_sell[sym] -= remaining
                if sym == "BOND":
                    trade_bond(exchange)
            if order_id in arb:
                pair = arb.pop(order_id)
                is_buy = (order_id == pair["buy_id"])
                if is_buy:
                    excess = pair["sell_filled"] - pair["buy_filled"]
                    if excess > 0:
                        sym = "VALBZ" if pair["case"] == 1 else "VALE"
                        ref = best_ask.get(sym)
                        if ref:
                            place_order(exchange, sym, "BUY", ref[0], excess)
                            inflight_buy[sym] += excess
                else:
                    excess = pair["buy_filled"] - pair["sell_filled"]
                    if excess > 0:
                        sym = "VALE" if pair["case"] == 1 else "VALBZ"
                        ref = best_bid.get(sym)
                        if ref:
                            place_order(exchange, sym, "SELL", ref[0], excess)
                            inflight_sell[sym] += excess
        elif t == "reject":
            print("reject:", message, file=sys.stderr)
        elif t == "error":
            print("error:", message, file=sys.stderr)

    disconnect(exchange)

if __name__ == "__main__":
    assert team_name != "REPLAC" + "EME", "Please put your team name in the variable [team_name]."
    main()
