import random
import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import INITIAL_FAIR_PRICES, XLF_BASKET


@dataclass
class MarketState:
    fair_prices: Dict[str, float] = field(default_factory=lambda: dict(INITIAL_FAIR_PRICES))
    vale_basis: float = 0.0
    xlf_basis: float = 0.0
    sector_drift: float = 0.0
    volatility: float = 1.0
    liquidity: float = 1.0
    bias: Dict[str, float] = field(default_factory=dict)  # flow imbalance by symbol

    def vale_fair(self) -> float:
        return self.fair_prices["VALBZ"] + self.vale_basis

    def basket_fair(self) -> float:
        return (
            XLF_BASKET["BOND"] * self.fair_prices["BOND"] +
            XLF_BASKET["GS"] * self.fair_prices["GS"] +
            XLF_BASKET["MS"] * self.fair_prices["MS"] +
            XLF_BASKET["WFC"] * self.fair_prices["WFC"]
        ) / 10.0

    def xlf_fair(self) -> float:
        return self.basket_fair() + self.xlf_basis

    def get_fair(self, symbol: str) -> float:
        if symbol == "VALE":
            return self.vale_fair()
        if symbol == "XLF":
            return self.xlf_fair()
        return self.fair_prices[symbol]


class Effect:
    label: str = "effect"

    def __init__(self, start: float, duration: float):
        self.start = start
        self.end = start + duration

    def is_active(self, t: float) -> bool:
        return self.start <= t < self.end

    def is_expired(self, t: float) -> bool:
        return t >= self.end

    def apply(self, state: MarketState, t: float, dt: float):
        pass

    def describe(self) -> str:
        return self.label


class TrendEffect(Effect):
    label = "trend"

    def __init__(self, start, duration, symbol, velocity):
        super().__init__(start, duration)
        self.symbol = symbol
        self.velocity = velocity

    def apply(self, state, t, dt):
        state.fair_prices[self.symbol] += self.velocity * dt

    def describe(self):
        d = "up" if self.velocity > 0 else "down"
        return f"trend {self.symbol} {d} ({self.velocity:+.2f}/s, {self.end - self.start:.0f}s)"


class VolatilityEffect(Effect):
    label = "volatility"

    def __init__(self, start, duration, multiplier):
        super().__init__(start, duration)
        self.multiplier = multiplier

    def apply(self, state, t, dt):
        state.volatility *= self.multiplier

    def describe(self):
        return f"volatility spike x{self.multiplier:.1f} ({self.end - self.start:.0f}s)"


class ShockEffect(Effect):
    label = "shock"

    def __init__(self, start, symbol, magnitude):
        super().__init__(start, 0.05)
        self.symbol = symbol
        self.magnitude = magnitude
        self._applied = False

    def apply(self, state, t, dt):
        if not self._applied:
            state.fair_prices[self.symbol] += self.magnitude
            self._applied = True

    def describe(self):
        return f"shock {self.symbol} ({self.magnitude:+.0f})"


class ValeDivergenceEffect(Effect):
    label = "vale_div"

    def __init__(self, start, duration, target_basis):
        super().__init__(start, duration)
        self.target_basis = target_basis

    def apply(self, state, t, dt):
        state.vale_basis += (self.target_basis - state.vale_basis) * min(1.0, 0.3 * dt)

    def describe(self):
        return f"VALE divergence target={self.target_basis:+.0f}"


class XlfDivergenceEffect(Effect):
    label = "xlf_div"

    def __init__(self, start, duration, target_basis):
        super().__init__(start, duration)
        self.target_basis = target_basis

    def apply(self, state, t, dt):
        state.xlf_basis += (self.target_basis - state.xlf_basis) * min(1.0, 0.3 * dt)

    def describe(self):
        return f"XLF divergence target={self.target_basis:+.0f}"


class SectorCorrelationEffect(Effect):
    label = "sector"

    def __init__(self, start, duration, drift):
        super().__init__(start, duration)
        self.drift = drift

    def apply(self, state, t, dt):
        state.sector_drift += self.drift * dt

    def describe(self):
        return f"sector drift {self.drift:+.2f}/s"


class LiquidityEffect(Effect):
    label = "liquidity"

    def __init__(self, start, duration, multiplier):
        super().__init__(start, duration)
        self.multiplier = multiplier

    def apply(self, state, t, dt):
        state.liquidity *= self.multiplier

    def describe(self):
        return f"liquidity x{self.multiplier:.1f}"


class FlowBiasEffect(Effect):
    label = "flow_bias"

    def __init__(self, start, duration, symbol, bias):
        super().__init__(start, duration)
        self.symbol = symbol
        self.bias = bias

    def apply(self, state, t, dt):
        state.bias[self.symbol] = state.bias.get(self.symbol, 0.0) + self.bias

    def describe(self):
        side = "buy pressure" if self.bias > 0 else "sell pressure"
        return f"{self.symbol} {side}"


class OscillationEffect(Effect):
    label = "oscillation"

    def __init__(self, start, duration, symbol, amplitude, period):
        super().__init__(start, duration)
        self.symbol = symbol
        self.amplitude = amplitude
        self.period = period
        self._last_offset = 0.0

    def apply(self, state, t, dt):
        phase = 2 * math.pi * (t - self.start) / self.period
        offset = self.amplitude * math.sin(phase)
        state.fair_prices[self.symbol] += (offset - self._last_offset)
        self._last_offset = offset

    def describe(self):
        return f"oscillation {self.symbol} ±{self.amplitude:.0f}"


# ── FORCED SCENARIOS ──────────────────────────────────────────────────────
# 원하는 시나리오를 추가하세요. 되돌리려면 아래 리스트 항목들을 주석 처리하세요.
# 형식: (라운드_시작_후_몇_초, EffectClass, *생성자_인자들)
# 300초 라운드 기준: 초반=0~60, 중반=100~200, 후반=200~280
FORCED_SCENARIOS = [
    # 예시: 중반(180초)에 sector drift -1.61/s 를 40초간
    # (60, SectorCorrelationEffect, 40, -1.83), # -1.61),

    # 예시: 초반(30초)에 GS 급락 shock
    # (30, ShockEffect, "GS", -40),

    # 예시: 후반(220초)에 변동성 3배, 20초간
    # (220, VolatilityEffect, 20, 3.0),
]
# ─────────────────────────────────────────────────────────────────────────


class MarketEngine:
    """Drives fair-value evolution and schedules market scenarios."""

    def __init__(self, seed: Optional[int] = None, verbose: bool = False):
        self._rng = random.Random(seed)
        self.state = MarketState()
        self.effects: List[Effect] = []
        self.verbose = verbose
        now = time.monotonic()
        self.last_tick = now
        self.round_start = now
        self.next_scenario_at = now + self._rng.uniform(8, 20)
        self._forced_pending = list(FORCED_SCENARIOS)

    def reset(self):
        now = time.monotonic()
        self.state = MarketState()
        self.effects.clear()
        self.last_tick = now
        self.round_start = now
        self.next_scenario_at = now + self._rng.uniform(8, 20)
        self._forced_pending = list(FORCED_SCENARIOS)

    def tick(self):
        t = time.monotonic()
        dt = t - self.last_tick
        self.last_tick = t
        if dt <= 0:
            return
        if dt > 1.0:
            dt = 1.0  # cap catch-up steps

        self.state.volatility = 1.0
        self.state.liquidity = 1.0
        self.state.bias.clear()

        self.effects = [e for e in self.effects if not e.is_expired(t)]
        for effect in self.effects:
            if effect.is_active(t):
                effect.apply(self.state, t, dt)

        # FORCED_SCENARIOS 주입 — 되돌리려면 FORCED_SCENARIOS 리스트를 비우세요
        elapsed = t - self.round_start
        remaining = []
        for entry in self._forced_pending:
            delay, cls, *args = entry
            if elapsed >= delay:
                effect = cls(t, *args)
                self.effects.append(effect)
                self._log(f"[forced] {effect.describe()}")
            else:
                remaining.append(entry)
        self._forced_pending = remaining

        self.state.fair_prices["BOND"] = 1000.0

        vol = self.state.volatility
        common = self._rng.gauss(0, 0.25 * vol) * math.sqrt(dt)
        for sym in ("GS", "MS", "WFC"):
            self.state.fair_prices[sym] += common
            self.state.fair_prices[sym] += self._rng.gauss(0, 0.7 * vol) * math.sqrt(dt)
            self.state.fair_prices[sym] += self.state.sector_drift * dt

        self.state.fair_prices["VALBZ"] += self._rng.gauss(0, 0.8 * vol) * math.sqrt(dt)

        has_vale_div = any(isinstance(e, ValeDivergenceEffect) and e.is_active(t) for e in self.effects)
        if not has_vale_div:
            self.state.vale_basis *= math.exp(-0.15 * dt)

        has_xlf_div = any(isinstance(e, XlfDivergenceEffect) and e.is_active(t) for e in self.effects)
        if not has_xlf_div:
            self.state.xlf_basis *= math.exp(-0.15 * dt)

        has_sector = any(isinstance(e, SectorCorrelationEffect) and e.is_active(t) for e in self.effects)
        if not has_sector:
            self.state.sector_drift *= math.exp(-0.4 * dt)

        for sym in list(self.state.fair_prices):
            if sym == "BOND":
                continue
            self.state.fair_prices[sym] = max(50.0, min(3000.0, self.state.fair_prices[sym]))
        self.state.vale_basis = max(-80.0, min(80.0, self.state.vale_basis))
        self.state.xlf_basis = max(-250.0, min(250.0, self.state.xlf_basis))

        if t >= self.next_scenario_at:
            self._trigger_random_scenario(t)
            self.next_scenario_at = t + self._rng.uniform(12, 35)

    def _log(self, msg):
        if self.verbose:
            print(f"[market] {msg}")

    def _trigger_random_scenario(self, t):
        scenarios = [
            (self._scenario_trend, 18),
            (self._scenario_shock, 10),
            (self._scenario_volatility, 10),
            (self._scenario_vale_divergence, 12),
            (self._scenario_xlf_divergence, 12),
            (self._scenario_sector_correlation, 10),
            (self._scenario_liquidity, 8),
            (self._scenario_flash_crash, 6),
            (self._scenario_flow_bias, 10),
            (self._scenario_oscillation, 6),
            (self._scenario_correlated_shock, 4),
        ]
        total = sum(w for _, w in scenarios)
        r = self._rng.uniform(0, total)
        acc = 0
        for fn, w in scenarios:
            acc += w
            if r <= acc:
                fn(t)
                return

    def _scenario_trend(self, t):
        symbol = self._rng.choice(["GS", "MS", "WFC", "VALBZ"])
        direction = self._rng.choice([-1, 1])
        velocity = direction * self._rng.uniform(0.4, 1.6)
        duration = self._rng.uniform(20, 60)
        e = TrendEffect(t, duration, symbol, velocity)
        self.effects.append(e)
        self._log(e.describe())

    def _scenario_shock(self, t):
        symbol = self._rng.choice(["GS", "MS", "WFC", "VALBZ"])
        magnitude = self._rng.choice([-1, 1]) * self._rng.uniform(15, 45)
        e = ShockEffect(t, symbol, magnitude)
        self.effects.append(e)
        self._log(e.describe())

    def _scenario_volatility(self, t):
        multiplier = self._rng.uniform(2.0, 5.0)
        duration = self._rng.uniform(15, 30)
        e = VolatilityEffect(t, duration, multiplier)
        self.effects.append(e)
        self._log(e.describe())

    def _scenario_vale_divergence(self, t):
        target = self._rng.choice([-1, 1]) * self._rng.uniform(15, 35)
        duration = self._rng.uniform(30, 90)
        e = ValeDivergenceEffect(t, duration, target)
        self.effects.append(e)
        self._log(e.describe())

    def _scenario_xlf_divergence(self, t):
        target = self._rng.choice([-1, 1]) * self._rng.uniform(80, 200)
        duration = self._rng.uniform(30, 90)
        e = XlfDivergenceEffect(t, duration, target)
        self.effects.append(e)
        self._log(e.describe())

    def _scenario_sector_correlation(self, t):
        drift = self._rng.choice([-1, 1]) * self._rng.uniform(0.5, 2.0)
        duration = self._rng.uniform(25, 60)
        e = SectorCorrelationEffect(t, duration, drift)
        self.effects.append(e)
        self._log(e.describe())

    def _scenario_liquidity(self, t):
        multiplier = self._rng.choice([0.3, 0.5, 1.6, 2.2])
        duration = self._rng.uniform(15, 40)
        e = LiquidityEffect(t, duration, multiplier)
        self.effects.append(e)
        self._log(e.describe())

    def _scenario_flash_crash(self, t):
        symbol = self._rng.choice(["GS", "MS", "WFC", "VALBZ"])
        drop = -self._rng.uniform(30, 60)
        self.effects.append(ShockEffect(t, symbol, drop))
        recovery = TrendEffect(t + 0.5, 15, symbol, -drop / 15)
        self.effects.append(recovery)
        self._log(f"flash crash {symbol} ({drop:+.0f} then recover)")

    def _scenario_flow_bias(self, t):
        symbol = self._rng.choice(["GS", "MS", "WFC", "VALBZ", "VALE", "XLF"])
        bias = self._rng.choice([-1, 1]) * self._rng.uniform(0.3, 0.7)
        duration = self._rng.uniform(20, 50)
        e = FlowBiasEffect(t, duration, symbol, bias)
        self.effects.append(e)
        self._log(e.describe())

    def _scenario_oscillation(self, t):
        symbol = self._rng.choice(["GS", "MS", "WFC", "VALBZ"])
        amplitude = self._rng.uniform(5, 15)
        period = self._rng.uniform(8, 20)
        duration = self._rng.uniform(30, 60)
        e = OscillationEffect(t, duration, symbol, amplitude, period)
        self.effects.append(e)
        self._log(e.describe())

    def _scenario_correlated_shock(self, t):
        for sym in ("GS", "MS", "WFC"):
            magnitude = self._rng.choice([-1, 1]) * self._rng.uniform(15, 30)
            self.effects.append(ShockEffect(t, sym, magnitude))
        self._log("correlated sector shock")
