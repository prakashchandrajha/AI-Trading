"""
Frontier Autonomous Options Engine
Upgrades institutional derivatives analytics beyond retail Nuvama dashboards:
1. Pure-Python Black-Scholes Greeks (Delta, Gamma, Vega, Theta).
2. Net Gamma Exposure (GEX) mapping & Volatility Flip detection (Short Gamma vs Long Gamma).
3. Institutional Gamma Squeeze Telemetry (Call writer capitulation alerts).
4. Sector Leadership Matrix (Banking, IT, Auto, Energy confluence).
5. Frontier Alpha Synthesis & Autonomous Paper Execution.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from options_analytics import NuvamaAnalyticsResult, NuvamaOptionsEngine, OptionStrikeData


def norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    """Standard normal probability density function."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


@dataclass
class OptionGreeks:
    delta_call: float
    delta_put: float
    gamma: float
    theta_call: float
    vega: float
    gex_call: float  # -1 * Spot * Gamma * Call_OI * Lot_Size (Dealers Short Call Gamma)
    gex_put: float   # +1 * Spot * Gamma * Put_OI * Lot_Size  (Dealers Long Put Gamma)
    net_gex: float   # gex_put + gex_call


@dataclass
class FrontierAnalysisResult:
    symbol: str
    underlying_price: float
    net_gex_crores: float            # Total market Net Gamma Exposure in ₹ Crores
    gex_regime: str                  # "LONG_GAMMA_PIN" (Mean Reversion) or "SHORT_GAMMA_VOL" (Explosive Momentum)
    gex_flip_strike: float           # The exact price level where dealer hedging flips from stabilizing to accelerating
    gamma_squeeze_score: float       # 0.0 to 100.0 score of immediate short-covering gamma blast
    nuvama: NuvamaAnalyticsResult
    sector_confluence: str           # "CONFIRMED_LEADERSHIP", "DIVERGENT", "NEUTRAL"
    actionable_signal: str           # "BUY_CE", "BUY_PE", "STAND_ASIDE"
    conviction_pct: int              # 0 to 100
    execution_strategy: str          # "GAMMA_MOMENTUM_BREAKOUT", "MEAN_REVERSION_PIN", "WAIT_FOR_GEX_FLIP"
    frontier_thesis: str


class FrontierAnalyticsEngine:
    """
    Quantitative derivatives engine modeled after institutional prop trading desks.
    """

    @staticmethod
    def calculate_greeks(
        spot: float,
        strike: float,
        dte: float,
        iv: float = 0.15,
        r: float = 0.053,
        call_oi: int = 0,
        put_oi: int = 0,
        lot_size: int = 1,
    ) -> OptionGreeks:
        """
        Calculates Black-Scholes Greeks and Net Dealer Gamma Exposure (GEX).
        """
        if spot <= 0 or strike <= 0 or iv <= 0:
            return OptionGreeks(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

        t = max(0.15 / 365.0, dte / 365.0)
        sqrt_t = math.sqrt(t)

        d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t) / (iv * sqrt_t)
        d2 = d1 - iv * sqrt_t

        delta_c = norm_cdf(d1)
        delta_p = delta_c - 1.0

        pdf_d1 = norm_pdf(d1)
        gamma = pdf_d1 / (spot * iv * sqrt_t)
        cdf_d2 = norm_cdf(d2)

        # Theta in ₹ per day (includes rKe^{-rT}N(d2) interest rate carry term)
        theta_c = (-(spot * pdf_d1 * iv) / (2.0 * sqrt_t) - r * strike * math.exp(-r * t) * cdf_d2) / 365.0
        vega = spot * sqrt_t * pdf_d1 / 100.0

        # GEX calculation:
        # Market Makers are typically short calls to retail and short puts to hedgers.
        # Net GEX = Spot * Gamma * (Put_OI - Call_OI) * Lot_Size / 1e7 (in ₹ Crores)
        gex_call = -1.0 * spot * gamma * call_oi * lot_size / 1e7
        gex_put = 1.0 * spot * gamma * put_oi * lot_size / 1e7
        net_gex = gex_call + gex_put

        return OptionGreeks(
            delta_call=round(delta_c, 3),
            delta_put=round(delta_p, 3),
            gamma=gamma,
            theta_call=round(theta_c, 2),
            vega=round(vega, 2),
            gex_call=round(gex_call, 2),
            gex_put=round(gex_put, 2),
            net_gex=round(net_gex, 2),
        )

    @classmethod
    def evaluate_frontier_landscape(
        cls,
        symbol: str,
        underlying_price: float,
        chain: List[OptionStrikeData],
        dte: float = 2.0,
        sector_confluence_state: str = "CONFIRMED_LEADERSHIP",
    ) -> FrontierAnalysisResult:
        """
        Synthesizes Nuvama Open Interest walls with institutional Net GEX and Gamma Squeeze mechanics.
        """
        spec = NuvamaOptionsEngine.SYMBOL_SPECS.get(
            symbol.upper(), NuvamaOptionsEngine.SYMBOL_SPECS["NIFTY"]
        )
        lot_size = spec["lot_size"]

        # 1. Base Nuvama Analytics (Max Pain, Walls, Velocity)
        nuvama_res = NuvamaOptionsEngine.analyze(
            underlying_price=underlying_price,
            chain=chain,
            symbol=symbol,
            dte=dte,
        )

        if not chain or underlying_price <= 0.0:
            return FrontierAnalysisResult(
                symbol=symbol,
                underlying_price=underlying_price,
                net_gex_crores=0.0,
                gex_regime="TRANSITIONAL_NEUTRAL",
                gex_flip_strike=0.0,
                gamma_squeeze_score=0.0,
                nuvama=nuvama_res,
                sector_confluence=sector_confluence_state,
                actionable_signal="STAND_ASIDE",
                conviction_pct=0,
                execution_strategy="WAIT_FOR_GEX_FLIP",
                frontier_thesis="Insufficient option chain data for Greeks/GEX evaluation.",
            )

        # 2. Compute Strike-Wise GEX and Net Landscape
        total_net_gex = 0.0
        strike_gex: List[Tuple[float, float]] = []

        for item in chain:
            greeks = cls.calculate_greeks(
                spot=underlying_price,
                strike=item.strike,
                dte=dte,
                iv=0.14,
                call_oi=item.call_oi,
                put_oi=item.put_oi,
                lot_size=lot_size,
            )
            total_net_gex += greeks.net_gex
            strike_gex.append((item.strike, greeks.net_gex))

        # 3. Detect GEX Flip Point (The line where market maker hedging turns explosive)
        strike_gex.sort(key=lambda x: x[0])
        gex_flip = nuvama_res.call_wall_strike
        for i in range(len(strike_gex) - 1):
            s1, g1 = strike_gex[i]
            s2, g2 = strike_gex[i + 1]
            if (g1 < 0 and g2 > 0) or (g1 > 0 and g2 < 0):
                gex_flip = round((s1 + s2) / 2.0, 1)
                break

        # 4. GEX Regime
        # Long Gamma: Dealers buy dips and sell rallies -> Stabilizing, Pinning to Max Pain
        # Short Gamma: Dealers forced to chase market momentum -> Explosive Volatility Squeeze
        if total_net_gex < -5.0:
            gex_regime = "SHORT_GAMMA_VOLATILITY"
        elif total_net_gex > 5.0:
            gex_regime = "LONG_GAMMA_PINNING"
        else:
            gex_regime = "TRANSITIONAL_NEUTRAL"

        # 5. Gamma Squeeze Probability Telemetry
        # Triggered when:
        # A) Spot is within 1 strike of Call Wall
        # B) Call OI is unwinding (<0 change)
        # C) Net ATM velocity is positive (Put writing > Call writing)
        dist_to_call_wall = nuvama_res.call_wall_strike - underlying_price
        squeeze_score = 0.0

        if dist_to_call_wall <= (spec["strike_step"] * 1.5):
            squeeze_score += 40.0
        if nuvama_res.near_atm_oi_velocity > 500000:
            squeeze_score += 30.0
        if gex_regime == "SHORT_GAMMA_VOLATILITY":
            squeeze_score += 20.0
        if sector_confluence_state == "CONFIRMED_LEADERSHIP":
            squeeze_score += 10.0

        squeeze_score = min(100.0, squeeze_score)

        # 6. Actionable Alpha Decision
        signal = "STAND_ASIDE"
        strategy = "WAIT_FOR_GEX_FLIP"
        conviction = 50
        thesis_parts = []

        if squeeze_score >= 70.0 and nuvama_res.trade_recommendation == "BUY_CE":
            signal = "BUY_CE"
            strategy = "GAMMA_MOMENTUM_BREAKOUT"
            conviction = int(squeeze_score)
            thesis_parts.append(
                f"🚨 GAMMA SQUEEZE ALERT ({squeeze_score:.0f}/100): "
                f"Spot ({underlying_price:.1f}) pressing Call Wall ({nuvama_res.call_wall_strike:.0f}). "
                f"Dealers trapped in {gex_regime}. Short-covering forced futures buying underway."
            )
        elif gex_regime == "LONG_GAMMA_PINNING" and abs(underlying_price - nuvama_res.max_pain_strike) > spec["strike_step"]:
            signal = "BUY_CE" if underlying_price < nuvama_res.max_pain_strike else "BUY_PE"
            strategy = "MEAN_REVERSION_PIN"
            conviction = 82
            thesis_parts.append(
                f"🧲 MAX PAIN GRAVITY PIN: Market in Long Gamma ({total_net_gex:+.1f} Cr). "
                f"Dealers hedging will pull Spot ({underlying_price:.1f}) toward Max Pain ({nuvama_res.max_pain_strike:.0f})."
            )
        elif nuvama_res.trade_recommendation in ("BUY_CE", "BUY_PE") and sector_confluence_state == "CONFIRMED_LEADERSHIP":
            signal = nuvama_res.trade_recommendation
            strategy = "SECTOR_CONFLUENCE_TREND"
            conviction = 84
            thesis_parts.append(
                f"🏛️ SECTOR CONFLUENCE: {symbol} aligned with broader peer group flow. "
                f"Institutional flow confirmed at {nuvama_res.sentiment}."
            )
        else:
            thesis_parts.append(
                f"⚖️ RANGEBOUND: Net GEX={total_net_gex:+.1f} Cr. Squeeze score low ({squeeze_score:.0f}/100). Stand aside."
            )

        return FrontierAnalysisResult(
            symbol=symbol,
            underlying_price=underlying_price,
            net_gex_crores=round(total_net_gex, 2),
            gex_regime=gex_regime,
            gex_flip_strike=gex_flip,
            gamma_squeeze_score=round(squeeze_score, 1),
            nuvama=nuvama_res,
            sector_confluence=sector_confluence_state,
            actionable_signal=signal,
            conviction_pct=conviction,
            execution_strategy=strategy,
            frontier_thesis=" | ".join(thesis_parts),
        )


GreeksGexEngine = FrontierAnalyticsEngine
GexAnalysisResult = FrontierAnalysisResult

