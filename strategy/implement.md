# Bot 구현 가이드

공식 skeleton 기반으로 단계별 구현. 각 Phase 끝나면 시뮬레이터 돌려서 검증.

```bash
# 시뮬레이터
python3 strategy/simulation/run.py --round 30 --rounds 1 --print-interval 5 --seed 1

# 봇 (별도 터미널)
python3 strategy/bot.py
```

---

## 📋 전체 로드맵

```
Phase 0  핵심 개념    시장가(fair)를 어떻게 알아내나
Phase 1  메시지 루프  if/elif 디스패처
Phase 2  상태 추적    position, books, fair
Phase 3  주문 헬퍼    place_order / cancel / convert
Phase 4  BOND 전략    첫 P&L (fair=1000 고정)
Phase 5  ADR 차익     VALE ↔ VALBZ
Phase 6  ETF 차익     XLF ↔ basket
Phase 7  Market Make  최종 (advanced)
```

---

## Phase 0 — 시장가(fair)는 어떻게 알아내나

거래소가 "이게 진짜 가격이다"라고 직접 알려주지 않음. **봇이 메시지에서 추정**.

| 메시지 | 알려주는 것 |
|---|---|
| `trade` | "방금 X원에 거래됐다" (과거 체결) |
| `book`  | "지금 X원에 사겠다는 사람 / Y원에 팔겠다는 사람 있다" (현재 호가) |

### 4가지 fair 추정 방법

**1. `trade` 체결가 (가장 흔함)**
```python
# {"type": "trade", "symbol": "GS", "price": 1071, "size": 5}
fair[symbol] = price
```
한계: 거래가 없으면 안 옴.

**2. `book` mid price (호가창 중간값)**
```python
# {"type": "book", "symbol": "GS",
#  "buy":  [[1070, 5], ...],   매수 호가 (높은 순)
#  "sell": [[1072, 3], ...]}   매도 호가 (낮은 순)
mid = (buys[0][0] + sells[0][0]) / 2   # = 1071
```

**3. BOND는 고정**
```python
fair["BOND"] = 1000
```

**4. 합성 fair (파생)**
```python
fair["VALE"] ≈ fair["VALBZ"]                                    # 동일 자산
fair["XLF"]  = (3*BOND + 2*GS + 3*MS + 2*WFC) / 10              # 바스켓 가중합
```

### 실전 우선순위

```python
def get_fair(sym):
    if sym == "BOND":
        return 1000
    if sym in fair:                          # trade로 갱신된 적 있음
        return fair[sym]
    bid = best_bid.get(sym)
    ask = best_ask.get(sym)
    if bid and ask:
        return (bid[0] + ask[0]) / 2         # mid fallback
    return None                              # 모름 → 거래 안 함
```

---

## Phase 1 — 메시지 루프

`main()`의 hello 핸드셰이크 다음에 무한 루프 추가.

```python
while True:
    message = read_from_exchange(exchange)
    t = message["type"]

    if t == "close":
        print("round closed", file=sys.stderr)
    elif t == "hello":
        pass                                  # 라운드 시작 시 재수신
    elif t == "book":
        pass                                  # TODO Phase 2
    elif t == "trade":
        pass                                  # TODO Phase 2
    elif t == "fill":
        pass                                  # TODO Phase 2
    elif t == "out":
        pass
    elif t == "reject":
        print("reject:", message, file=sys.stderr)
    elif t == "error":
        print("error:", message, file=sys.stderr)
```

**검증**: 시뮬레이터 돌리고 봇 붙였을 때 라운드 끝까지 안 죽는지 확인.

---

## Phase 2 — 상태 추적

CONFIGURATION 아래 전역 추가:

```python
SYMBOLS = ["BOND", "GS", "MS", "WFC", "VALBZ", "VALE", "XLF"]
POSITION_LIMITS = {
    "BOND":100, "GS":100, "MS":100, "WFC":100,
    "VALBZ":10, "VALE":10, "XLF":100,
}

position = {s: 0 for s in SYMBOLS}
cash = 0
fair = {}                                    # 마지막 체결가
best_bid = {}                                # {sym: (price, size)}
best_ask = {}
```

각 핸들러 채우기:

```python
elif t == "book":
    sym = message["symbol"]
    buys  = message.get("buy")  or []
    sells = message.get("sell") or []
    best_bid[sym] = (buys[0][0],  buys[0][1])  if buys  else None
    best_ask[sym] = (sells[0][0], sells[0][1]) if sells else None

elif t == "trade":
    fair[message["symbol"]] = message["price"]

elif t == "fill":
    sym   = message["symbol"]
    size  = message["size"]
    price = message["price"]
    if message["dir"] == "BUY":
        position[sym] += size
        cash -= price * size
    else:
        position[sym] -= size
        cash += price * size
```

> 함수 안에서 전역 수정 시 `global position, cash, ...` 선언 필요.

**검증**: 봇 안에서 `print(position, cash, file=sys.stderr)` 찍고 시뮬레이터의 `[pnl/...]` 라인이랑 일치하는지 확인.

---

## Phase 3 — 주문 헬퍼

```python
order_id = 0

def place_order(exchange, symbol, direction, price, size):
    global order_id
    order_id += 1
    write_to_exchange(exchange, {
        "type": "add", "order_id": order_id,
        "symbol": symbol, "dir": direction,
        "price": int(price), "size": int(size),
    })
    return order_id

def cancel_order(exchange, oid):
    write_to_exchange(exchange, {"type": "cancel", "order_id": oid})

def convert(exchange, symbol, direction, size):
    """
    VALE  →  VALBZ : convert(ex, "VALE", "SELL", n)
    VALBZ →  VALE  : convert(ex, "VALE", "BUY",  n)
    XLF   →  basket: convert(ex, "XLF",  "SELL", n)   # n은 10의 배수
    basket→  XLF   : convert(ex, "XLF",  "BUY",  n)
    """
    global order_id
    order_id += 1
    write_to_exchange(exchange, {
        "type": "convert", "order_id": order_id,
        "symbol": symbol, "dir": direction, "size": int(size),
    })
    return order_id
```

**검증**: hello 직후 `place_order(exchange, "BOND", "BUY", 999, 1)` 한 번 넣고 → `fill` 메시지 오는지, `position["BOND"]`가 1로 바뀌는지 확인.

---

## Phase 4 — BOND 전략 (첫 수익)

BOND는 fair = **항상 1000**. 가장 단순한 차익거래.

```python
def trade_bond(exchange):
    lim = POSITION_LIMITS["BOND"]

    ask = best_ask.get("BOND")
    if ask and ask[0] < 1000 and position["BOND"] < lim:
        size = min(ask[1], lim - position["BOND"])
        place_order(exchange, "BOND", "BUY", ask[0], size)

    bid = best_bid.get("BOND")
    if bid and bid[0] > 1000 and position["BOND"] > -lim:
        size = min(bid[1], lim + position["BOND"])
        place_order(exchange, "BOND", "SELL", bid[0], size)
```

`book` 핸들러에서 BOND일 때 호출:
```python
elif t == "book":
    ...                                      # state 갱신
    if sym == "BOND":
        trade_bond(exchange)
```

**검증**: 라운드 끝나면 시뮬레이터가 `[pnl/final] YUM: P&L=+XXX` 찍어줌. 양수면 성공.

---

## Phase 5 — ADR 차익거래 (VALE ↔ VALBZ)

같은 자산, 변환비용 $10/주.

```python
VALE_FEE = 10

def trade_adr(exchange):
    av = best_ask.get("VALE");  bb = best_bid.get("VALBZ")
    ab = best_ask.get("VALBZ"); bv = best_bid.get("VALE")

    # VALE 사서 → VALBZ 변환 → VALBZ 팔기
    if av and bb and bb[0] - av[0] - VALE_FEE > 0:
        size = min(av[1], bb[1], 1,
                   POSITION_LIMITS["VALE"] - position["VALE"])
        if size > 0:
            place_order(exchange, "VALE",  "BUY",  av[0], size)
            convert(exchange, "VALE", "SELL", size)
            place_order(exchange, "VALBZ", "SELL", bb[0], size)

    # VALBZ 사서 → VALE 변환 → VALE 팔기
    if ab and bv and bv[0] - ab[0] - VALE_FEE > 0:
        size = min(ab[1], bv[1], 1,
                   POSITION_LIMITS["VALBZ"] - position["VALBZ"])
        if size > 0:
            place_order(exchange, "VALBZ", "BUY",  ab[0], size)
            convert(exchange, "VALE", "BUY", size)
            place_order(exchange, "VALE",  "SELL", bv[0], size)
```

`book` 핸들러에서 `sym in ("VALE", "VALBZ")`일 때 호출.

> 차익 기회는 시장 이벤트(`ValeDivergenceEffect`)가 발동될 때만 자주 나타남. 짧은 라운드(5초 등)에선 거의 안 잡힘. `--round 120 --verbose`로 확인.

---

## Phase 6 — ETF 차익거래 (XLF ↔ basket)

XLF 10주 = 3·BOND + 2·GS + 3·MS + 2·WFC, 변환비용 $100.

```python
XLF_BASKET = {"BOND": 3, "GS": 2, "MS": 3, "WFC": 2}
XLF_FEE = 100

def trade_etf(exchange):
    if not all(s in best_bid and s in best_ask for s in XLF_BASKET):
        return
    if "XLF" not in best_bid or "XLF" not in best_ask:
        return

    basket_bid = sum(best_bid[s][0] * n for s, n in XLF_BASKET.items())
    basket_ask = sum(best_ask[s][0] * n for s, n in XLF_BASKET.items())
    xlf_bid = best_bid["XLF"][0]
    xlf_ask = best_ask["XLF"][0]

    # XLF 싸다: XLF 10주 사서 → 분해 → 각각 팔기
    if basket_bid - 10 * xlf_ask - XLF_FEE > 0:
        if position["XLF"] + 10 <= POSITION_LIMITS["XLF"]:
            place_order(exchange, "XLF", "BUY", xlf_ask, 10)
            convert(exchange, "XLF", "SELL", 10)
            for s, n in XLF_BASKET.items():
                place_order(exchange, s, "SELL", best_bid[s][0], n)

    # XLF 비싸다: 바스켓 사서 → 합쳐서 → XLF 팔기
    if 10 * xlf_bid - basket_ask - XLF_FEE > 0:
        if position["XLF"] - 10 >= -POSITION_LIMITS["XLF"]:
            for s, n in XLF_BASKET.items():
                place_order(exchange, s, "BUY", best_ask[s][0], n)
            convert(exchange, "XLF", "BUY", 10)
            place_order(exchange, "XLF", "SELL", xlf_bid, 10)
```

`book` 핸들러에서 `sym == "XLF" or sym in XLF_BASKET`일 때 호출.

---

## Phase 7 — Market Making (advanced)

각 종목 fair 양옆에 bid/ask 깔아서 spread 수익. 핵심 고려사항:

```
1. delta = max(2, fair * 0.003)         # spread 폭 (가격의 ~0.3%)
2. inventory skew                        # 재고 많으면 매도 호가 좁히고 매수 호가 넓혀서 자동 정리
3. 호가 갱신 시 기존 주문 cancel 후 add  # 가격 바뀔 때마다
4. EWMA volatility로 delta 동적 조정    # 시장 흔들릴 때 spread 넓힘
5. AIMD fill-feedback                    # 너무 많이 체결되고 손실 나면 spread 확대
```

기존 풀버전 참고: `git show 9075da5:strategy/bot.py`

---

## 💡 디버깅 팁

| 증상 | 의심 |
|---|---|
| `reject` 폭주 | 같은 order_id 재사용 / position limit 초과 / price·size가 int 아님 |
| P&L 적자 | 변환비용 빼먹음 / 한 방향만 체결되고 반대편이 슬리피지 |
| `WARN: 500 msg/s` | book 한 번에 주문 N개씩 → 메시지 폭증, 조건 더 빡세게 |
| 차익 기회 못 봄 | 라운드 너무 짧음 → `--round 300`로 길게 |

### 검증 워크플로

```bash
# 1) 빠른 sanity check (30초, 1라운드)
python3 strategy/simulation/run.py --round 30 --rounds 1 --print-interval 5 --seed 1

# 2) 안정성 (5분 풀라운드)
python3 strategy/simulation/run.py --round 300 --rounds 1 --seed 1

# 3) 차익 시나리오 관찰 (verbose로 시장 이벤트 확인)
python3 strategy/simulation/run.py --round 300 --rounds 1 --verbose --seed 1
```

라운드 끝나면 `[pnl/final] YUM: P&L=+XXX` 찍히는 게 점수. **+로 나오면 성공.**

---

## 🎯 진행 체크리스트

- [ ] Phase 1: 메시지 루프 — 라운드 끝까지 안 죽음
- [ ] Phase 2: 상태 추적 — `position`이 fill 따라 갱신됨
- [ ] Phase 3: 주문 헬퍼 — 수동 주문 한 건 체결 확인
- [ ] Phase 4: BOND 전략 — P&L > 0
- [ ] Phase 5: ADR 차익 — VALE divergence 이벤트 잡음
- [ ] Phase 6: ETF 차익 — XLF divergence 이벤트 잡음
- [ ] Phase 7: Market making — 모든 종목에 호가 제출
