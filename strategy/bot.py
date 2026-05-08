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
MM_SYMBOLS = ["GS", "MS", "WFC"] # except VALBZ and VALE
MM_WINDOW_RATIO = 0.008 # 0.003 is bad
HARD_LIMIT  = {"GS": 15, "MS": 15, "WFC": 15} # 15
SKEW_K = 0.8 # 0.4 is bad
mm_orders = {} # (symbol, side) -> order_id

# emergency close # ewma # fast가 정찰단 느낌
PRICE_ALPHA_FAST = 0.2 # avg # EWMA alpha (degradation) ~ 0.1 means considering latest 10% value
PRICE_ALPHA_SLOW = 0.05
VOL_ALPHA_FAST = 0.15 # variance # EWMA alpha (degradation) ~ 0.1 means considering latest 10% value
VOL_ALPHA_SLOW = 0.01 # slow for finding out emergency
DRIFT_K = 3.5 # z-score threshold to pause trading (close MM)
RESUME_K = 1.0 # z-score threshold to resume trading (restart MM)
FLUSH_MIN_CD = 5 # minimum cooldown time

ewma_price_fast = {} # symbol -> ewma_price
ewma_price_slow = {} # symbol -> ewma_price
ewma_vol_fast = {} # symbol -> ewma_vol
ewma_vol_slow = {} # symbol -> ewma_vol

in_flush       = {s: False for s in SYMBOLS}    # whether currently in flush
in_soft_unwind = {s: False for s in MM_SYMBOLS} # non-adverse high-z: one-sided unwind to 0
flush_min      = {s: 0     for s in SYMBOLS}    # minimum cooldown counter
flush_oid      = {s: None  for s in MM_SYMBOLS} # order_id of active flush order (Bug 4)

EMERGENCY_BEST_COST_RANGE = 0.3 # market price * 0.7 ~ market price * 1.3
EMERGENCY_UNSTABLE_COST_DELTA = 50 # TODO : should be dynamic
GHOST_TICK_DELTA_RATIO = 0.3 # threshold for ghost tick detection

last_raw_price = {}
stable_count = {}

# maximum number of in-flight orders
MAX_ORDERS = 100

# order ratio
BOND_order_ratio = 0.05
MM_order_ratio = 0.1

# FEE
VALE_FEE = 10

# ARB PROFIT THRESHOLD
MIN_ARB_PROFIT = 0

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

# arb pairs: buy_id -> {sell_id, size, buy_filled, sell_filled, converted, buy_done, sell_done, case}
arb_pairs = {}
arb_pair_by_sell = {}  # sell_id -> buy_id



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
    line = exchange.readline()
    if not line:
        raise Exception("Connection closed by exchange.")
    return orjson.loads(line)

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

# try to convert
def _try_convert_pair(exchange, buy_id):
    pair = arb_pairs.get(buy_id)
    if not pair:
        return
    to_convert = min(pair["buy_filled"], pair["sell_filled"]) - pair["converted"]
    if to_convert <= 0:
        return
    if pair["case"] == 1:
        convert(exchange, "VALE", "SELL", to_convert)  # give VALE, get VALBZ (cover short)
    else:
        convert(exchange, "VALE", "BUY",  to_convert)  # give VALBZ, get VALE (cover short)
    pair["converted"] += to_convert

def _close_pair_leg(exchange, order_id):
    # check if this order is "buy" or "sell"
    is_buy_leg = order_id in arb_pairs
    if is_buy_leg:
        buy_id = order_id
    elif order_id in arb_pair_by_sell:
        # buy finished
        buy_id = arb_pair_by_sell.pop(order_id)
    else:
        return

    if buy_id not in arb_pairs:
        return
    pair = arb_pairs[buy_id]

    if is_buy_leg: # buy fiiled
        pair["buy_done"] = True
        # shorted more than bought → cover excess short at market
        excess = pair["sell_filled"] - pair["buy_filled"]
        if excess > 0:
            sym = "VALBZ" if pair["case"] == 1 else "VALE"
            ref = best_ask.get(sym)
            if ref:
                place_order(exchange, sym, "BUY", ref[0], excess)
                inflight_buy[sym] += excess
    else: # sell filled
        pair["sell_done"] = True
        # bought more than shorted → sell excess long at market
        excess = pair["buy_filled"] - pair["sell_filled"]
        if excess > 0:
            sym = "VALE" if pair["case"] == 1 else "VALBZ"
            ref = best_bid.get(sym)
            if ref:
                place_order(exchange, sym, "SELL", ref[0], excess)
                inflight_sell[sym] += excess

    if pair["buy_done"] and pair["sell_done"]:
        arb_pairs.pop(buy_id, None)
        if is_buy_leg:
            arb_pair_by_sell.pop(pair["sell_id"], None)


def trade_adr(exchange):
    global best_ask, best_bid, position

    vale_low   = best_ask.get("VALE")
    vale_high  = best_bid.get("VALE")
    valbz_low  = best_ask.get("VALBZ")
    valbz_high = best_bid.get("VALBZ")

    if not vale_high or not vale_low or not valbz_high or not valbz_low:
        return

    # Cancel pairs whose arb condition has disappeared
    for buy_id, pair in list(arb_pairs.items()):
        if pair["case"] == 1:
            still_arb = valbz_high[0] - vale_low[0] - VALE_FEE > MIN_ARB_PROFIT
        else:
            still_arb = vale_high[0] - valbz_low[0] - VALE_FEE > MIN_ARB_PROFIT
        if not still_arb:
            if buy_id in orders:
                cancel_order(exchange, buy_id)
            if pair["sell_id"] in orders:
                cancel_order(exchange, pair["sell_id"])

    if valbz_high[0] - vale_low[0] - VALE_FEE > MIN_ARB_PROFIT:  # CASE 1: buy VALE, short VALBZ
        size = min(
            vale_low[1],
            valbz_high[1],
            POSITION_LIMIT["VALE"]  - position["VALE"]  - inflight_buy["VALE"],
            POSITION_LIMIT["VALBZ"] + position["VALBZ"] - inflight_sell["VALBZ"],
        )
        if size > 0:
            buy_id  = place_order(exchange, "VALE",  "BUY",  vale_low[0],   size)
            sell_id = place_order(exchange, "VALBZ", "SELL", valbz_high[0], size)
            inflight_buy["VALE"]   += size
            inflight_sell["VALBZ"] += size
            arb_pairs[buy_id] = {
                "sell_id": sell_id, "size": size,
                "buy_filled": 0, "sell_filled": 0, "converted": 0,
                "buy_done": False, "sell_done": False, "case": 1,
            }
            arb_pair_by_sell[sell_id] = buy_id

    elif vale_high[0] - valbz_low[0] - VALE_FEE > MIN_ARB_PROFIT:  # CASE 2: buy VALBZ, short VALE
        size = min(
            valbz_low[1],
            vale_high[1],
            POSITION_LIMIT["VALBZ"] - position["VALBZ"] - inflight_buy["VALBZ"],
            POSITION_LIMIT["VALE"]  + position["VALE"]  - inflight_sell["VALE"],
        )
        if size > 0:
            buy_id  = place_order(exchange, "VALBZ", "BUY",  valbz_low[0],  size)
            sell_id = place_order(exchange, "VALE",  "SELL", vale_high[0],  size)
            inflight_buy["VALBZ"]  += size
            inflight_sell["VALE"]  += size
            arb_pairs[buy_id] = {
                "sell_id": sell_id, "size": size,
                "buy_filled": 0, "sell_filled": 0, "converted": 0,
                "buy_done": False, "sell_done": False, "case": 2,
            }
            arb_pair_by_sell[sell_id] = buy_id


# ~~~~~============== MARKET MAKING ==============~~~~~

# basic MM
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

# dynamic delta
## ev fast for window(delta)
def calc_skewed_delta(symbol, base_d):
    limit = HARD_LIMIT[symbol]
    norm = max(-1.0, min(1.0, position[symbol] / limit)) # long position -> position >> 0 and norm ~= 1 -> low buy, high sell
    bid_d = max(1, int(base_d * (1 + SKEW_K * norm))) # high buying price in long position -> low buy
    ask_d = max(1, int(base_d * (1 - SKEW_K * norm))) # low selling price in long position -> high sell
    return bid_d, ask_d

# EWMA for emergency close
def update_ewma(symbol, price):
    ep_slow = ewma_price_slow.get(symbol, price)
    ep_fast = ewma_price_fast.get(symbol, price)
    dev = abs(price - ep_slow)
    ev_slow = ewma_vol_slow.get(symbol, dev)
    ev_fast = ewma_vol_fast.get(symbol, dev)

    ep_fast_now = ep_fast * (1 - PRICE_ALPHA_FAST) + price * PRICE_ALPHA_FAST
    ewma_price_fast[symbol] = ep_fast_now

    last_p = last_raw_price.get(symbol, price)
    last_raw_price[symbol] = price
    dynamic_threshold = max(ev_slow, 1.0) * 1.5

    gap_closed = abs(price - ep_fast_now) < dynamic_threshold
    price_flat = abs(price - last_p) < dynamic_threshold

    if gap_closed and price_flat:
        stable_count[symbol] = stable_count.get(symbol, 0) + 1
    else:
        stable_count[symbol] = 0

    is_locally_stable = stable_count.get(symbol, 0) >= 3

    # detect ghost tick
    if ep_slow is not None and dev / ep_slow > GHOST_TICK_DELTA_RATIO:
        if is_locally_stable: # exit logic
            ewma_price_slow[symbol] = price
            stable_count[symbol] = 0
        else:
            print(f"[IGNORED_UNSTABLE_TICK] {symbol} price={price} ewma_fast={ep_fast_now:.1f} dev={dev:.1f} vol_fast={ev_fast:.1f} vol_slow={ev_slow:.1f}", file=sys.stderr)
            return
        return

    paused = in_flush.get(symbol, False) or in_soft_unwind.get(symbol, False)
    if not paused:
        ewma_price_slow[symbol] = ep_slow * (1 - PRICE_ALPHA_SLOW) + price * PRICE_ALPHA_SLOW
        ewma_vol_slow[symbol] = ev_slow * (1 - VOL_ALPHA_SLOW) + dev * VOL_ALPHA_SLOW
        ewma_vol_fast[symbol] = ev_fast * (1 - VOL_ALPHA_FAST) + dev * VOL_ALPHA_FAST

## ep, ev slow for z
def z_score_abs(symbol, price):
    ep_slow = ewma_price_slow.get(symbol)
    ev_slow = ewma_vol_slow.get(symbol)
    if ep_slow is None or ev_slow is None or ev_slow < 1: # TODO : ev == 0 ??
        return 0
    return abs(price - ep_slow) / ev_slow # TODO : why abs?

def is_flush(symbol, price):
    ep_slow = ewma_price_slow.get(symbol)
    ev_slow = ewma_vol_slow.get(symbol)
    if ep_slow is None or ev_slow is None or ev_slow < 1:
        return False
    dev = price - ep_slow
    pos = position[symbol]
    reverse = (dev < 0 and pos > 0) or (dev > 0 and pos < 0) # position in the opposite direction of drift -> risky
    return reverse and abs(dev) > DRIFT_K * ev_slow  # same with "z > DRIFT_K"

def is_stable(symbol, price):
    ep_slow = ewma_price_slow.get(symbol)

    if ep_slow and abs(price - ep_slow) > EMERGENCY_UNSTABLE_COST_DELTA:
        return False
    return z_score_abs(symbol, price) < RESUME_K

def emergency_close(exchange, symbol):
    cancel_quote(exchange, symbol, "BUY")
    cancel_quote(exchange, symbol, "SELL")

    pos = position[symbol]
    fair_price = fair.get(symbol)
    flush_oid[symbol] = None
    if pos > 0:
        ref = best_bid.get(symbol)
        price = ref[0] if ref and ref[0] >= fair_price * (1 - EMERGENCY_BEST_COST_RANGE) else int(fair_price * 0.99)
        oid = place_order(exchange, symbol, "SELL", price, pos)
        flush_oid[symbol] = oid
        mm_orders[(symbol, "SELL")] = oid
    elif pos < 0:
        ref = best_ask.get(symbol)
        price = ref[0] if ref and ref[0] <= fair_price * (1 + EMERGENCY_BEST_COST_RANGE) else int(fair_price * 1.01)
        oid = place_order(exchange, symbol, "BUY", price, -pos)
        flush_oid[symbol] = oid
        mm_orders[(symbol, "BUY")] = oid
    else:
        price = None
    in_flush[symbol] = True
    flush_min[symbol] = FLUSH_MIN_CD
    print(f"[FLUSH] {symbol} pos={pos} fair={fair_price} price={price}", file=sys.stderr)

def soft_unwind(exchange, symbol):
    pos = position[symbol]
    fair_price = fair.get(symbol)
    if not fair_price:
        b = best_bid.get(symbol)
        a = best_ask.get(symbol)
        if b and a:
            fair_price = (b[0] + a[0]) // 2
    if not fair_price:
        return
    delta = max(2, int(fair_price * MM_WINDOW_RATIO))
    if pos > 0:
        cancel_quote(exchange, symbol, "BUY")
        key = (symbol, "SELL")
        old_id = mm_orders.get(key)
        if old_id:
            cancel_order(exchange, old_id)
        sell_price = fair_price + delta
        new_id = place_order(exchange, symbol, "SELL", sell_price, pos)
        mm_orders[key] = new_id
        print(f"[SOFT_UNWIND_ORDER] {symbol} SELL {pos} @ {sell_price} (fair={fair_price})", file=sys.stderr)
    elif pos < 0:
        cancel_quote(exchange, symbol, "SELL")
        key = (symbol, "BUY")
        old_id = mm_orders.get(key)
        if old_id:
            cancel_order(exchange, old_id)
        buy_price = fair_price - delta
        new_id = place_order(exchange, symbol, "BUY", buy_price, -pos)
        mm_orders[key] = new_id
        print(f"[SOFT_UNWIND_ORDER] {symbol} BUY {-pos} @ {buy_price} (fair={fair_price})", file=sys.stderr)
    else:
        cancel_quote(exchange, symbol, "BUY")
        cancel_quote(exchange, symbol, "SELL")

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
            # trade_bond(exchange)
        elif t == "book":
            symbol = message["symbol"]
            buys  = message.get("buy")  or []
            sells = message.get("sell") or []
            best_bid[symbol] = (buys[0][0],  buys[0][1])  if buys  else None
            best_ask[symbol] = (sells[0][0], sells[0][1]) if sells else None
            # if symbol in ["VALBZ", "VALE"]:
            #     trade_adr(exchange)
        elif t == "trade":
            symbol = message["symbol"]
            price = message["price"]
            fair[symbol] = price
            if symbol in MM_SYMBOLS:
                if in_flush[symbol]:
                    if flush_min[symbol] > 0:
                        flush_min[symbol] -= 1
                    update_ewma(symbol, price)
                    if flush_min[symbol] == 0 and is_stable(symbol, price):
                        in_flush[symbol] = False
                        place_mm(exchange, symbol)
                        print(f"[RESUME] {symbol}", file=sys.stderr)
                elif in_soft_unwind[symbol]:
                    if flush_min[symbol] > 0:
                        flush_min[symbol] -= 1
                    update_ewma(symbol, price)
                    if is_flush(symbol, price) and position[symbol] != 0:
                        # direction reversed → became adverse, escalate to emergency close
                        ep_slow = ewma_price_slow.get(symbol, price)
                        ev_slow = ewma_vol_slow.get(symbol, 1)
                        dev = price - ep_slow
                        z = abs(dev) / ev_slow if ev_slow >= 1 else 0
                        print(f"[FLUSH_ESCALATE] {symbol} pos={position[symbol]} price={price} ewma={ep_slow:.1f} dev={dev:+.1f} vol_slow={ev_slow:.1f} z={z:.2f}", file=sys.stderr)
                        in_soft_unwind[symbol] = False
                        emergency_close(exchange, symbol)
                    elif flush_min[symbol] == 0 and is_stable(symbol, price):
                        in_soft_unwind[symbol] = False
                        place_mm(exchange, symbol)
                        print(f"[RESUME] {symbol}", file=sys.stderr)
                    elif position[symbol] == 0:
                        cancel_quote(exchange, symbol, "BUY")
                        cancel_quote(exchange, symbol, "SELL")
                    else:
                        soft_unwind(exchange, symbol)
                elif z_score_abs(symbol, price) > DRIFT_K:
                    update_ewma(symbol, price)
                    ep_slow = ewma_price_slow.get(symbol, price)
                    ev_slow = ewma_vol_slow.get(symbol, 1)
                    dev = price - ep_slow
                    z = abs(dev) / ev_slow if ev_slow >= 1 else 0
                    if is_flush(symbol, price) and position[symbol] != 0:
                        print(f"[FLUSH_TRIGGER] {symbol} pos={position[symbol]} price={price} ewma={ep_slow:.1f} dev={dev:+.1f} vol_slow={ev_slow:.1f} z={z:.2f}", file=sys.stderr)
                        emergency_close(exchange, symbol)
                    else:
                        in_soft_unwind[symbol] = True
                        flush_min[symbol] = FLUSH_MIN_CD
                        print(f"[SOFT_UNWIND] {symbol} pos={position[symbol]} price={price} ewma={ep_slow:.1f} dev={dev:+.1f} vol_slow={ev_slow:.1f} z={z:.2f}", file=sys.stderr)
                        if position[symbol] == 0:
                            cancel_quote(exchange, symbol, "BUY")
                            cancel_quote(exchange, symbol, "SELL")
                        else:
                            soft_unwind(exchange, symbol)
                else:
                    update_ewma(symbol, price)
                    place_mm(exchange, symbol)
                # print(f"{t}\t{symbol}\t-\t{price}\t{message['size']}")
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
                # trade_bond(exchange)
                pass
            elif symbol in ("VALE", "VALBZ"):
                # if order_id in arb_pairs:
                #     arb_pairs[order_id]["buy_filled"] += size
                #     _try_convert_pair(exchange, order_id)
                # elif order_id in arb_pair_by_sell:
                #     buy_id = arb_pair_by_sell[order_id]
                #     if buy_id in arb_pairs:
                #         arb_pairs[buy_id]["sell_filled"] += size
                #         _try_convert_pair(exchange, buy_id)
                pass
            elif symbol in MM_SYMBOLS:
                print(f"FILLLLL\t{symbol}\t{dir}\t{price}\t{size}")
                if in_flush[symbol]:
                    pass
                elif in_soft_unwind[symbol]:
                    if position[symbol] == 0:
                        cancel_quote(exchange, symbol, "BUY")
                        cancel_quote(exchange, symbol, "SELL")
                    else:
                        soft_unwind(exchange, symbol)
                else:
                    place_mm(exchange, symbol)

        elif t == "ack":
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

        elif t == "out":
            order_id = message["order_id"]
            if order_id in orders:
                sym, dir, remaining = orders.pop(order_id)
                # decrement inflight by whatever was not filled
                if dir == "BUY":
                    inflight_buy[sym]  -= remaining
                else:
                    inflight_sell[sym] -= remaining
            _close_pair_leg(exchange, order_id)
            for key, oid in list(mm_orders.items()):
                if oid == order_id:
                    mm_orders.pop(key, None)
            # flush 주문이 미체결로 out되면 잔여 포지션 재청산 (stale price 체크 + mm_orders 등록)
            for sym in MM_SYMBOLS:
                if flush_oid.get(sym) == order_id:
                    flush_oid[sym] = None
                    if in_flush[sym] and position[sym] != 0:
                        pos = position[sym]
                        fp = fair.get(sym)
                        if pos > 0:
                            ref = best_bid.get(sym)
                            if ref and fp and ref[0] >= fp * (1 - EMERGENCY_BEST_COST_RANGE):
                                price = ref[0]
                            elif fp:
                                price = int(fp * 0.99)
                            else:
                                break
                            oid = place_order(exchange, sym, "SELL", price, pos)
                            flush_oid[sym] = oid
                            mm_orders[(sym, "SELL")] = oid
                        elif pos < 0:
                            ref = best_ask.get(sym)
                            if ref and fp and ref[0] <= fp * (1 + EMERGENCY_BEST_COST_RANGE):
                                price = ref[0]
                            elif fp:
                                price = int(fp * 1.01)
                            else:
                                break
                            oid = place_order(exchange, sym, "BUY", price, -pos)
                            flush_oid[sym] = oid
                            mm_orders[(sym, "BUY")] = oid
                    break

        elif t == "reject":
            print("reject:", message, file=sys.stderr)
        elif t == "error":
            print("error:", message, file=sys.stderr)

    disconnect(exchange)

if __name__ == "__main__":
    assert team_name != "REPLAC" + "EME", "Please put your team name in the variable [team_name]."
    main()
