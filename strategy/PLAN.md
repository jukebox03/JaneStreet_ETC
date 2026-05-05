# Jane Street ETC - Strategy Plan

## 대회 기본 정보

- 라운드: 5분짜리 매치업 반복, 라운드 종료 시 cash/포지션 리셋
- 자산: BOND, GS, MS, WFC, VALBZ, VALE (ADR), XLF (ETF)
- 서버: TCP 소켓, JSON 메시지, rate limit 500개/초

### 자산 공정가

| 자산 | 공정가 |
|------|--------|
| BOND | 고정 $1000 |
| GS / MS / WFC / VALBZ / VALE | 시장가 |
| XLF | `(3×BOND + 2×GS + 3×MS + 2×WFC) / 10` |

### 전환 관계

| 전환 | 비율 | 수수료 |
|------|------|--------|
| VALE ↔ VALBZ | 1:1 | $10 |
| XLF ↔ (3 BOND + 2 GS + 3 MS + 2 WFC) | 10주 단위 | $100 |

---

## 전략 1: BOND 마켓 메이킹

BOND는 공정가가 $1000으로 고정이므로 리스크 없는 무위험 전략.

```
$999에 매수 / $1001에 매도
체결 또는 소멸 시 즉시 재주문
수익: 건당 $2
```

**구현 포인트**
- `out` 메시지 수신 시 동일 조건으로 재주문
- size를 크게 잡을수록 유리 (단, 체결 안 될 수 있음)

---

## 전략 2: 마켓 메이킹 (핵심 전략)

모든 자산에 대해 직전 체결가를 공정가로 보고 양쪽에 주문을 상시 유지.

```
trade 메시지 수신 (심볼 S, 가격 P)
  → 기존 S 매수/매도 주문 취소
  → P - delta 에 매수 1주
  → P + delta 에 매도 1주
```

**delta 선택**
- 고정 delta=2: 안정적, 라운드당 $3,000~$12,000
- 동적 delta=`price//300`: 가격 대비 ~0.33% spread, 추세에 덜 취약
- delta=1: 체결 잘 되지만 추세 시장에서 역선택 위험

**역선택 문제 (추세 시장)**
- 시장이 한 방향으로 계속 움직이면 한쪽 주문만 체결 → 포지션 한쪽으로 쌓임
- 대응: 포지션 한도 ±30~50 설정, 한도 초과 시 해당 방향 주문 중단

**구현 포인트**
- 자산별 order_id 고정 할당 (취소 시 id 추적 불필요)
- `out` 메시지 수신 시 재주문 (전량 체결 or 취소 완료 신호)
- VALE/VALBZ는 마켓 메이킹 제외하고 차익거래 전용으로 쓰는 것 고려

---

## 전략 3: ADR 차익거래 (VALE / VALBZ)

VALE와 VALBZ의 가격 괴리를 이용.

```
VALBZ 가격 > VALE 가격 + 전환수수료($10) + 마진 상황:

1. VALE 매수
2. VALE → VALBZ 전환 ($10)
3. VALBZ 매도

수익 = (VALBZ 가격 - VALE 가격 - $10) × 수량
```

반대 방향도 동일하게 적용 (VALE가 비싸면 VALBZ 매수 후 전환).

**구현 포인트**
- 최소 마진 기준: 가격 차 > $10(수수료) + 슬리피지 여유
- 직전 체결가 기준 또는 최근 N개 평균 사용 가능

---

## 전략 4: ETF 차익거래 (XLF)

XLF와 구성 바스켓의 가격 괴리를 이용.

```
바스켓 가격 = 3×BOND + 2×GS + 3×MS + 2×WFC

[XLF가 싸다] 바스켓 > 10×XLF + 임계값:
  XLF 10주 매수 → XLF를 바스켓으로 분해 → 각 주식 매도

[XLF가 비싸다] 10×XLF > 바스켓 + 임계값:
  각 주식 매수 → 바스켓을 XLF로 조립 → XLF 10주 매도
```

**임계값 설정**
- 최소 $100 (전환 수수료) + 슬리피지 여유
- 보수적: $150 (FOMO 팀)
- 공격적: $50 (fair-conversion.py)

---

## 우리 팀 전략 방향

### 기본 방향

1. **마켓 메이킹** (핵심 수익원)
2. **ADR / ETF 차익거래** (추가 수익원)

### 마켓 메이킹 설계: 동적 delta

**기본 형태**
```
delta = price × ratio(volatility, fill_rate)
```

가격에 비례한 delta로 자산 가격대에 독립적인 spread 비율 유지.

**ratio 결정 요소**
- 가격 변동성 → 변동성 클수록 delta 확대
- 체결량 피드백 → 체결 과다/과소 시 delta 조정

#### 변동성 추정: EWMA

```
var_t = (1-α) · var_{t-1} + α · obs_t²
σ_t = √var_t
```

- **관찰값(obs)**: 로그 수익률 `log(price_t / price_{t-1})` 사용 (스케일 불변)
- **α 값**: 0.003~0.005 (half-life ≈ 200~300 체결 ≈ 15~20초)
- **반영**: `ratio = base_ratio × (1 + k·σ_normalized)`

#### 체결량 피드백: TCP CUBIC 구조 차용

TCP CUBIC의 구조를 그대로 사용하되, "drop(congestion)" 판단 기준만 마켓 메이킹에 맞게 재정의.

```
평상시:    ratio를 CUBIC 곡선으로 점진적 축소 (더 공격적)
drop 발생: ratio ← ratio × 1/β  (급격히 후퇴)
이후:      CUBIC으로 이전 ratio_min까지 복귀
```

**drop 판단 기준 (핵심)**

단일 지표가 아닌 **P&L과 fill_rate의 AND 조건**으로 정의.

```python
def is_drop_event():
    pnl_bad   = recent_pnl < pnl_threshold        # 최근 N초 P&L 악화
    fills_hot = recent_fill_rate > fr_threshold   # 체결은 활발
    return pnl_bad and fills_hot
```

즉 drop = "체결은 많이 되는데 돈을 잃고 있는 상태" = 역선택 신호.

**두 조건 AND가 필요한 이유**

| 상황 | P&L | fill_rate | drop? | 근거 |
|------|-----|-----------|-------|------|
| 역선택 | ↓ | ↑ | ✓ | delta로 해결 가능 |
| 건강한 체결 | ↑ | ↑ | ✗ | 오히려 좋은 상태 |
| 시장 변동 | ↓ | ↓ | ✗ | delta 문제 아님 (변동성은 EWMA가 처리) |
| 이상적 | ↑ | ↓ | ✗ | 유지 |

**단일 지표의 함정**
- P&L만 봄: 변동성 급증 시 포지션 평가액 흔들림만으로도 drop 오판
- fill_rate만 봄: 시장 활발 + 수익 상황에도 drop 오판

**구현 시 해결해야 할 문제**

| 이슈 | 해결 방향 |
|------|----------|
| 시간 척도 | TCP는 ms, 마켓 메이킹은 초 단위 → K, C 재튜닝 필요 |
| Regime shift | W_max 자체에도 decay 적용 |
| 초기 구현 | AIMD 먼저 안정화 후 CUBIC 곡선 도입 |
| 임계값 튜닝 | `pnl_threshold`, `fr_threshold`는 시뮬레이터로 결정 |

### 추가 고려사항: Inventory skew (비대칭 delta)

포지션이 한쪽으로 쌓이면 대칭 delta는 추세를 증폭시킴.
→ 쌓인 방향으로는 체결이 덜 되도록 delta를 비대칭으로 조정.

**수식**
```
normalized = inventory / position_limit   # -1 ~ +1 범위
ask_delta = delta × (1 - skew · normalized)   # long 시 더 쉽게 팔리도록 좁힘
bid_delta = delta × (1 + skew · normalized)   # long 시 매수는 어렵게 넓힘
```

**동작 예시** (GS, delta=3, skew=0.5, limit=100, 현재 포지션=+50):
```
normalized = 0.5
ask_delta = 3 × (1 - 0.25) = 2.25   → 매도 호가 타이트 → 잘 팔림
bid_delta = 3 × (1 + 0.25) = 3.75   → 매수 호가 넓음 → 덜 사게 됨
→ 자연스럽게 long 포지션이 줄어드는 방향으로 유도
```

**효과**
- 추세 시장에서 한쪽으로 쏠리는 포지션 자가 조정
- 역선택 노출 감소 (CUBIC drop event와 상호 보완)
- Yeouido Street의 하드 한도(±30)보다 부드럽게 작동

**skew 계수 k**
- k=0: 대칭 delta (기본)
- k=0.5: 중간 정도 비대칭
- k=1.0: 한도 근처에서 delta 2배 비대칭
- 초기값 0.3~0.5 권장, 시뮬레이터로 튜닝

### 미결 결정 사항

- base_ratio 초기값 (0.002~0.005 범위에서 튜닝)
- EWMA α 확정 (0.003~0.005)
- AIMD β 계수 (0.7~0.9)
- Inventory skew 계수 k
- 각 자산별로 별도 ratio 운용할지, 통합할지
- BOND는 고정가($1000)이므로 다른 로직 적용 (Δ=±1 고정 or 미세 동적)
- 차익거래 임계값: $50 공격적 / $150 보수적 중 선택

### 검증 방법

로컬 시뮬레이터(`simulation/`)로 AB 테스트:
1. 기준선: 고정 delta=2
2. 실험: 제안된 동적 delta 함수
3. 여러 seed로 다수 라운드 반복 → P&L 분포 비교

---

## 로컬 시뮬레이터 (`simulation/`)

대회 서버에 접근할 수 없을 때 로컬에서 전략을 테스트하기 위한 거래소 시뮬레이터.

### 구성

| 파일 | 역할 |
|------|------|
| `run.py` | 진입점, CLI 인자 처리 |
| `exchange/config.py` | 자산/수수료/포지션 한도 설정 |
| `exchange/order_book.py` | Price-time priority 체결 엔진 |
| `exchange/market.py` | 공정가 진화 + 11종의 시장 시나리오 |
| `exchange/marketplace.py` | 8개의 marketplace 봇 (maker/taker/noise) |
| `exchange/exchange.py` | hello/add/cancel/convert 메시지 처리 + 포지션/cash 추적 |
| `exchange/server.py` | TCP 서버 + 라운드 관리 + P&L 리포트 |

### 구현된 시장 시나리오 (무작위 발생)

- 트렌드 (상승/하락), 쇼크 (즉시 점프), 변동성 스파이크
- VALE/VALBZ 괴리, XLF/바스켓 괴리
- 섹터 동조 (GS/MS/WFC), 유동성 변화, 플래시 크래시
- 주문 흐름 편향, 진동, 섹터 연쇄 쇼크

### 실행 방법

```bash
cd simulation/
python3 run.py --mode prod --port 25000      # 정상 활동
python3 run.py --mode slower --port 25001    # 활동 감소
python3 run.py --mode empty --port 25002     # marketplace 없음
```

옵션: `--round 300`, `--seed 42`, `--verbose`

### 봇 연결

기존 봇에서 `--specific-address` 플래그 사용:

```bash
python3 fair-price-fix.py --specific-address localhost:25000
```

### 검증 결과

- `fair-price-fix.py` 봇이 라운드 20초 테스트에서 +$339 수익
- 초당 ~15~17건 체결 (대회 실전과 유사한 수준)
- 라운드 전환 시 포지션/cash 리셋 정상 동작
- ADR 괴리 시나리오 트리거 확인

---

## 팀 봇 구현 (2026-04-24)

### `strategy/bot.py`

PLAN.md 의 전략을 모두 담은 단일 파일 봇. 상단 상수만 바꿔 튜닝.

| 구성 요소 | 구현 |
|-----------|------|
| Market making | BOND/GS/MS/WFC/XLF (VALE/VALBZ 는 arb 전용) |
| 동적 delta | `fair × BASE_RATIO(0.003) × (1 + 25·σ) × ratio_mult` |
| 변동성 | EWMA α=0.004 on log returns |
| Inventory skew | `SKEW_K=0.4`, soft limit: BOND 80 / GS·MS·WFC 50 / XLF 60 |
| 체결 피드백 | AIMD (β=0.75, 선형 회복 0.02/s) — drop 판정: 3초 윈도우 fill_rate > 1/s **AND** 최근 P&L < -$200 |
| ADR arb | 호가 스프레드 > $10 + $3 마진 시 매수/convert/매도 |
| ETF arb | \|10×XLF − 바스켓\| > $100 + $30 마진 시 10주 단위 실행, 포지션 헤드룸 10 확보 |

### 시나리오별 거동

- **차익거래 기회 있는 시장** (XLF 또는 VALE/VALBZ 괴리 발생): ETF/ADR arb 가 주력 수익원, 수익 규모 큼
- **조용한 시장** (괴리 없음, 낮은 변동성): MM spread 수익만. 안정적이지만 규모 작음
- **추세/쇼크 시장** (섹터 동조 급등락): **현재 전략의 최대 취약점.** MM 이 한쪽 방향만 체결당해 반대 포지션이 누적, MTM 손실 폭발. PLAN.md §전략 2 "역선택 문제" 의 실제 발현

### 추세 시장 취약점의 원인 분석

1. **σ 기반 delta 확대가 추세를 노이즈로 착각** — EWMA 가 |return|² 만 보므로 한 방향 연속 이동과 등가. 변동성은 오르지만 추세 자체가 분리 감지되지 않음
2. **AIMD drop 판정이 너무 느슨** — 3초 윈도우 + P&L -$200 + fill_rate 1/s AND 조건은 급격한 추세에서 감지 전에 이미 큰 포지션 누적
3. **Soft limit 은 신규 주문만 차단** — 이미 쌓인 포지션을 스스로 줄이는 메커니즘 없음 (skew 는 bias 만 줄 뿐 강제 청산 없음)

### 다음 개선 우선순위

1. **추세 감지 별도 트랙**: EWMA 로 평균 수익률(drift) 을 분리 측정. `|drift| > 임계치` 시 MM 일시 중단 또는 delta 급확대
2. **Hard stop-out**: `|pos| > soft × 0.9` 이면 반대쪽 best 건너편 가격으로 공격적 청산 — 추세에 끌려가기 전에 손절
3. **drop-event 재튜닝**: 윈도우 1~2초로 축소, β 0.5~0.6 으로 확대 반응 강화
4. **Regime-aware delta**: 추세 국면 / 평온 국면을 분류해서 delta 함수 자체를 분기

### 미해결 튜닝 (AB 테스트로 결정)

| 파라미터 | 현재값 | 탐색 범위 |
|----------|--------|-----------|
| `BASE_RATIO` | 0.003 | 0.002~0.005 |
| `EWMA_ALPHA` | 0.004 | half-life 150~300 obs |
| `AIMD_BETA` | 0.75 | 추세장에서 약함 → 0.5 검토 |
| `SKEW_K` | 0.4 | 0.3~0.7 |
| `ADR_MARGIN` | $3 | $1~$5 |
| `ETF_MARGIN` | $30 | 공격적 $20 ~ 보수적 $50 |
