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

fair 양쪽에 bid/ask를 깔아두고 spread(bid-ask 차이)를 수익으로 챙기는 전략.
누군가 내 bid를 치면 나는 싸게 사고, 누군가 내 ask를 치면 나는 비싸게 판 셈.

```
수익 구조: ask에 팔고 bid에 사면 → spread 만큼 이익
리스크:    fair가 내 예상과 다르게 움직이면 → 재고 손실
```

### 7-1. 기본 구조 (delta 고정)

**MM 심볼**: BOND, GS, MS, WFC, XLF (VALE/VALBZ는 차익거래 전용)

```python
MM_SYMBOLS  = ["BOND", "GS", "MS", "WFC", "XLF"]
BASE_RATIO  = 0.003    # spread 폭 = fair의 0.3%
MM_SIZE     = 5        # 주문당 수량
SOFT_LIMIT  = {"BOND": 80, "GS": 50, "MS": 50, "WFC": 50, "XLF": 60}

# active MM 주문 추적: {(sym, "BUY"/"SELL"): order_id}
mm_orders = {}
```

delta 계산:
```python
def calc_delta(sym, f):
    if sym == "BOND":
        return 1                          # BOND는 fair=1000 고정이라 spread 1로 충분
    return max(2, int(f * BASE_RATIO))
```

호가 제출:
```python
def place_mm(exchange, sym):
    f = get_fair(sym)
    if f is None:
        return

    d   = calc_delta(sym, f)
    bid = max(1, int(f - d))
    ask = max(bid + 1, int(f + d))
    lim  = POSITION_LIMITS[sym]
    soft = SOFT_LIMIT.get(sym, lim)
    pos  = position[sym]

    # BUY 호가: 포지션이 soft limit 이내일 때만
    if pos + MM_SIZE <= lim and pos < soft:
        replace_quote(exchange, sym, "BUY",  bid)
    else:
        cancel_quote(exchange, sym, "BUY")

    # SELL 호가: 포지션이 soft limit 이내일 때만
    if pos - MM_SIZE >= -lim and pos > -soft:
        replace_quote(exchange, sym, "SELL", ask)
    else:
        cancel_quote(exchange, sym, "SELL")
```

호가 교체 (가격 안 바뀌면 no-op):
```python
def replace_quote(exchange, sym, side, price):
    key = (sym, side)
    old_id = mm_orders.get(key)
    if old_id is not None:
        cancel_order(exchange, old_id)
    oid = place_order(exchange, sym, side, price, MM_SIZE)
    mm_orders[key] = oid

def cancel_quote(exchange, sym, side):
    key = (sym, side)
    old_id = mm_orders.pop(key, None)
    if old_id is not None:
        cancel_order(exchange, old_id)
```

`trade` 핸들러에서 MM 심볼이면 requote:
```python
elif t == "trade":
    sym = message["symbol"]
    fair[sym] = message["price"]
    if sym in MM_SYMBOLS:
        place_mm(exchange, sym)
```

**검증**: 시뮬레이터에서 `[pnl/update]` 주기적으로 올라가는지 확인. P&L이 서서히 양수로 증가하면 성공.

---

### 7-2. Inventory Skew (재고 자동 정리)

포지션이 쌓이면 한쪽 방향으로 계속 채워져서 limit에 막힘.
**skew = 재고 방향으로 호가를 유리하게 조정** → 자연스럽게 청산 유도.

```
long 포지션 → ask를 좁혀서 팔기 쉽게, bid를 넓혀서 더 사기 어렵게
short 포지션 → 반대
```

```python
SKEW_K = 0.4   # skew 강도

def calc_skewed_delta(sym, base_d):
    soft = SOFT_LIMIT.get(sym, POSITION_LIMITS[sym])
    norm = max(-1.0, min(1.0, position[sym] / soft))   # -1 ~ +1

    bid_d = max(1, int(base_d * (1 + SKEW_K * norm)))  # long이면 bid 더 넓게
    ask_d = max(1, int(base_d * (1 - SKEW_K * norm)))  # long이면 ask 더 좁게
    return bid_d, ask_d
```

`place_mm` 수정:
```python
def place_mm(exchange, sym):
    f = get_fair(sym)
    if f is None:
        return

    base_d      = calc_delta(sym, f)
    bid_d, ask_d = calc_skewed_delta(sym, base_d)    # ← 추가

    bid = max(1, int(f - bid_d))
    ask = max(bid + 1, int(f + ask_d))
    ...
```

**검증**: 포지션이 한쪽으로 몰렸을 때 자동으로 줄어드는지 확인 (`print(position, file=sys.stderr)`).

---

### 7-3. Emergency Liquidation (긴급 청산)

가격이 급변하고 되돌아오지 않으면 skew만으로는 포지션을 털기 어려움.

**기준: 최근 누적 drift가 기존 변동성의 K배를 넘고 포지션이 불리한 방향일 때 청산.**

```
drift     = 현재 가격 - EWMA 기준가 (최근 가격의 지수이동평균)
ewma_vol  = EWMA of |drift|             (변동성 크기)
z-score   = |drift| / ewma_vol

z > DRIFT_K AND 포지션이 drift 반대 방향  →  긴급 청산
```

파라미터 추가:
```python
DRIFT_ALPHA   = 0.1   # 기준가 EWMA 감쇠 (~최근 10틱 반영)
VOL_ALPHA     = 0.05  # 변동성 EWMA 감쇠 (~최근 20틱, 더 안정적)
DRIFT_K       = 2.0   # flush 트리거 z-score 임계값
RESUME_K      = 0.5   # 재개 조건 z-score (< DRIFT_K)
FLUSH_MIN_CD  = 5     # 안정화 체크 전 최소 대기 틱 (즉시 재진입 방지)

ewma_price  = {}                           # {sym: float} 기준선 (최근 가격 평균)
ewma_vol    = {}                           # {sym: float} 기준선에서의 평균 이탈폭
in_flush    = {s: False for s in MM_SYMBOLS}  # 긴급 청산 모드 여부
flush_min   = {s: 0     for s in MM_SYMBOLS}  # 최소 대기 카운터
```

**`ewma_price` vs `ewma_vol`**:
```
ewma_price  = 최근 가격들의 지수평균  →  "기준선이 어디인가"
ewma_vol    = |price - ewma_price|의 지수평균  →  "기준선에서 보통 얼마나 벗어나나"

z = |현재가 - ewma_price| / ewma_vol  →  "지금 이탈이 평소의 몇 배인가"
```

EWMA 갱신 (trade 수신 시마다 호출):
```python
def update_drift(sym, price):
    ep  = ewma_price.get(sym, price)
    dev = abs(price - ep)
    ewma_vol[sym]   = (1 - VOL_ALPHA)  * ewma_vol.get(sym, dev)  + VOL_ALPHA  * dev
    ewma_price[sym] = (1 - DRIFT_ALPHA) * ep                      + DRIFT_ALPHA * price
```

flush 트리거 / 안정화 판단 (update_drift 호출 전에 평가):
```python
def z_score(sym, price):
    ep  = ewma_price.get(sym)
    vol = ewma_vol.get(sym, 0.0)
    if ep is None or vol < 1.0:
        return 0.0
    return abs(price - ep) / vol

def is_drift_flush(sym, price):
    ep  = ewma_price.get(sym)
    vol = ewma_vol.get(sym, 0.0)
    if ep is None or vol < 1.0:
        return False
    drift = price - ep
    pos   = position[sym]
    adverse = (drift < 0 and pos > 0) or (drift > 0 and pos < 0)
    return adverse and abs(drift) > DRIFT_K * vol

def is_stable(sym, price):
    """z-score가 RESUME_K 아래로 내려오면 시장 안정으로 판단."""
    return z_score(sym, price) < RESUME_K
```

긴급 청산 실행:
```python
def emergency_flush(exchange, sym):
    cancel_quote(exchange, sym, "BUY")
    cancel_quote(exchange, sym, "SELL")
    pos = position[sym]
    if pos > 0:
        ref = best_bid.get(sym)
        if ref:
            place_order(exchange, sym, "SELL", ref[0], pos)
    elif pos < 0:
        ref = best_ask.get(sym)
        if ref:
            place_order(exchange, sym, "BUY", ref[0], abs(pos))
    in_flush[sym]  = True
    flush_min[sym] = FLUSH_MIN_CD
    print(f"[FLUSH] {sym} pos={pos}", file=sys.stderr)
```

`trade` 핸들러:
```python
elif t == "trade":
    sym   = message["symbol"]
    price = message["price"]
    if sym in MM_SYMBOLS:
        if in_flush[sym]:
            if flush_min[sym] > 0:
                flush_min[sym] -= 1
            update_drift(sym, price)
            fair[sym] = price
            # 최소 대기 끝 + z-score 안정화 → MM 재개
            if flush_min[sym] == 0 and is_stable(sym, price):
                in_flush[sym] = False
                place_mm(exchange, sym)
                print(f"[RESUME] {sym}", file=sys.stderr)
        elif is_drift_flush(sym, price):   # ← update_drift 전에 평가
            update_drift(sym, price)
            fair[sym] = price
            if position[sym] != 0:
                emergency_flush(exchange, sym)
        else:
            update_drift(sym, price)
            fair[sym] = price
            place_mm(exchange, sym)
    else:
        fair[sym] = price
```

**안정화 재개가 동작하는 원리**:
```
가격이 새 수준에 안착한 경우:
  ewma_price가 천천히 새 수준으로 수렴 → drift 감소 → z < RESUME_K → 재개

가격이 급변 후 원래 수준으로 회귀:
  price가 ewma_price 쪽으로 돌아옴 → drift 감소 → z < RESUME_K → 재개 (더 빠름)
```

파라미터 튜닝:
| 파라미터 | 낮추면 | 높이면 |
|---|---|---|
| `DRIFT_K` | flush 더 자주 (과잉 반응 위험) | flush 더 드물게 (대형 손실 방치 위험) |
| `RESUME_K` | 더 완전히 안정된 뒤 재개 | 조금만 안정되면 바로 재개 |
| `FLUSH_MIN_CD` | 최소 대기 짧음 (flush 후 빠른 재진입) | 최소 대기 긺 |
| `DRIFT_ALPHA` | 기준가 천천히 이동 (drift 오래 유지 → 재개 느림) | 기준가 빠르게 현재가 추적 (재개 빠름) |

> **주의**: `is_drift_flush`와 `is_stable`을 반드시 `update_drift` 전에 호출해야 함.  
> 순서가 바뀌면 이미 갱신된 ewma_price 기준으로 비교해 z-score가 실제보다 작게 측정됨.

**검증**: `--round 300 --verbose`로 가격 급변 시 `[FLUSH]` 로그가 찍히고 `position`이 0으로 수렴하는지 확인.

---

### 7-4. EWMA Volatility (변동성 대응)

시장이 급격히 움직일 때 spread를 넓혀서 불리한 체결 줄이기.

```
sigma 커지면 → delta 커짐 → spread 넓어짐 → 왠만한 가격엔 안 체결됨
```

```python
import math

EWMA_ALPHA = 0.004    # 반감기 ~170 틱
VOL_K      = 25.0     # sigma가 ratio에 미치는 영향

ewma_var = {}         # {sym: float}  EWMA 분산

def update_vol(sym, price):
    if sym in fair and fair[sym] > 0 and price > 0:
        r = math.log(price / fair[sym])              # log return
        prev = ewma_var.get(sym, 0.0)
        ewma_var[sym] = (1 - EWMA_ALPHA) * prev + EWMA_ALPHA * r * r

def calc_delta(sym, f):
    if sym == "BOND":
        return 1
    sigma  = math.sqrt(ewma_var.get(sym, 0.0))
    ratio  = BASE_RATIO * (1 + VOL_K * sigma)        # 변동성 반영
    return max(2, int(f * ratio))
```

`trade` 핸들러에서 fair 갱신 전에 vol 업데이트:
```python
elif t == "trade":
    sym   = message["symbol"]
    price = message["price"]
    update_vol(sym, price)     # ← fair 갱신 전에
    fair[sym] = price
    if sym in MM_SYMBOLS:
        place_mm(exchange, sym)
```

---

### 7-5. Order Book Pressure (호가창 압력)

7-3의 flush는 trade 메시지(체결) 기반 — 이미 가격이 움직인 후에 반응.  
**book 메시지의 bid/ask 수량 불균형을 보면 가격 이동 전에 선제 대응 가능**.

```
bid 수량 >> ask 수량  →  매수 압력 → 가격 오를 가능성  →  short이면 위험
ask 수량 >> bid 수량  →  매도 압력 → 가격 내릴 가능성  →  long이면 위험
```

파라미터:
```python
IMB_FAIR_FACTOR = 0.5   # imbalance가 fair 보정에 미치는 영향
IMB_CANCEL_THR  = 0.7   # |imbalance| > 0.7 이면 선제 호가 취소
```

Imbalance 계산 및 fair 보정:
```python
def book_imbalance(sym):
    b = best_bid.get(sym)
    a = best_ask.get(sym)
    if not b or not a or (b[1] + a[1]) == 0:
        return 0.0
    return (b[1] - a[1]) / (b[1] + a[1])   # +1=bid 우세, -1=ask 우세

def get_fair_imbalance_adjusted(sym):
    b = best_bid.get(sym)
    a = best_ask.get(sym)
    if not b or not a:
        return fair.get(sym)
    mid  = (b[0] + a[0]) / 2
    imb  = book_imbalance(sym)
    half = (a[0] - b[0]) / 2
    # bid 압도적이면 fair를 mid보다 약간 높게, ask 압도적이면 낮게
    return mid + imb * half * IMB_FAIR_FACTOR
```

`place_mm`에서 fair 추정에 활용:
```python
def place_mm(exchange, sym):
    f = get_fair_imbalance_adjusted(sym)   # ← fair.get(sym) 대신
    if f is None:
        return
    ...
```

선제적 호가 취소 (`book` 핸들러에서):
```python
elif t == "book":
    sym = message["symbol"]
    ...  # best_bid / best_ask 갱신
    if sym in MM_SYMBOLS and flush_cd.get(sym, 0) == 0:
        imb = book_imbalance(sym)
        pos = position[sym]
        if imb > IMB_CANCEL_THR and pos < 0:    # bid 압도 + short → BUY 호가 취소
            cancel_quote(exchange, sym, "BUY")
        elif imb < -IMB_CANCEL_THR and pos > 0: # ask 압도 + long → SELL 호가 취소
            cancel_quote(exchange, sym, "SELL")
```

> full flush(포지션 청산)보다 보수적인 반응: 호가만 취소하고 기존 포지션은 유지.  
> 시장이 회복하면 다음 trade 틱에서 `place_mm`이 재호가를 올림.

**검증**: 한쪽 imbalance가 클 때 적절한 호가 취소 로그 찍히는지 확인.

---

### 7-6. Adaptive Flush Threshold (적응형 청산 임계값)

7-3의 `DRIFT_K`는 고정값. **7-4의 `ewma_var`(spread 단위 σ)를 활용해 `DRIFT_K`를 동적으로 낮추면, 이미 변동성이 높은 국면에서 더 빠르게 청산 트리거.**

```
σ 낮음 (calm)    →  DRIFT_K 유지 (기본값, 느슨하게)
σ 높음 (turbulent) →  DRIFT_K 축소 (더 민감하게)
```

7-3의 `is_drift_flush`만 수정:
```python
def is_drift_flush(sym, price):
    ep  = ewma_price.get(sym)
    vol = ewma_vol.get(sym, 0.0)
    if ep is None or vol < 1.0:
        return False
    drift = price - ep
    pos   = position[sym]
    adverse = (drift < 0 and pos > 0) or (drift > 0 and pos < 0)
    if not adverse:
        return False

    # vol_ratio: 현재 σ가 spread 폭 대비 얼마나 큰가
    sigma     = math.sqrt(ewma_var.get(sym, 0.0))
    vol_ratio = sigma / max(MM_WINDOW_RATIO, 1e-9)
    k = max(1.0, DRIFT_K / (1.0 + vol_ratio))   # ← 동적 DRIFT_K
    return abs(drift) > k * vol
```

> **구현 순서**: 반드시 7-4 이후에 적용 (`ewma_var` 없으면 vol_ratio=0 → 기본 `DRIFT_K`와 동일).

**검증**: 변동성 높은 구간에서 flush가 더 일찍 발생하는지 `[FLUSH]` 로그 타이밍 비교.

---

### 7-7. 전체 흐름 요약

```
book 수신
    ↓
best_bid/best_ask 갱신
book_imbalance 계산 (7-5)
    → 압력 극단적 + 역방향 포지션 → cancel_quote (선제 취소)

trade 수신
    ↓
in_flush[sym]?  (7-3)
    ├── YES → flush_min -= 1, update_drift, fair 갱신
    │         flush_min==0 AND is_stable? → in_flush=False, place_mm (재개)
    └── NO  →
        is_drift_flush?  ← update_drift 전에 평가 (k는 7-6으로 동적 조정)
            ├── YES → update_drift, fair 갱신, emergency_flush (in_flush=True)
            └── NO  → update_drift, fair 갱신
                           update_vol(sym, price)         # EWMA 분산 갱신 (7-4)
                           place_mm(exchange, sym)
                               ├── get_fair_imbalance_adjusted  # imbalance 보정 (7-5)
                               ├── calc_delta                   # vol 반영 spread (7-4)
                               ├── calc_skewed_delta             # 재고 비대칭 조정 (7-2)
                               ├── position limit 체크
                               └── replace_quote
```

---

### 7-8. 주의사항

| 함정 | 해결 |
|---|---|
| trade마다 cancel+add → 500 msg/s 초과 | 가격 안 바뀌면 no-op (replace_quote에서 체크) |
| on_out 안 처리 → mm_orders가 stale | `out` 수신 시 mm_orders에서 해당 oid 삭제 |
| 양쪽 동시 체결 → 재고 0인데 호가 양쪽 다 있음 | fill 후 place_mm 다시 호출해서 limit 재확인 |
| 차익거래와 MM이 같은 종목 두고 충돌 | SOFT_LIMIT으로 MM 여유 남겨두기 (hard limit보다 작게) |

`on_out` 핸들러:
```python
elif t == "out":
    oid = message["order_id"]
    # mm_orders에서 stale 항목 제거
    for key, val in list(mm_orders.items()):
        if val == oid:
            del mm_orders[key]
            break
```

`on_fill` 핸들러 뒤에 requote 추가:
```python
elif t == "fill":
    ...   # position/cash 갱신
    sym = message["symbol"]
    if sym in MM_SYMBOLS:
        place_mm(exchange, sym)    # 체결 후 즉시 새 호가 제출
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
- [ ] Phase 7-1: Market making — 모든 종목에 호가 제출
- [ ] Phase 7-2: Inventory Skew — 포지션 한쪽 쏠림 자동 해소
- [ ] Phase 7-3: Emergency Liquidation — shock/trend 시 포지션 강제 0
- [ ] Phase 7-4: EWMA Volatility — 변동성 급등 시 spread 자동 확대
- [ ] Phase 7-5: Order Book Pressure — imbalance 기반 fair 보정 + 선제 취소
- [ ] Phase 7-6: Adaptive Flush — σ에 따라 청산 임계값 자동 조정 (7-4 필요)
