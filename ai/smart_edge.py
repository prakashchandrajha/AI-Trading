"""
Smart Algo Edge Module — Machine-Level Computations Beyond Retail

Retail traders on Nuvama and Sensibull see:
    - Static OI walls (highest OI strike)
    - PCR as a single number
    - Greeks as numbers on a table
    - RSI / EMA crossovers on price charts

This module computes what NO retail dashboard shows:
    1. Wall Fragility Score — Is the Call/Put Wall crumbling or reinforcing in real-time?
    2. OI Concentration (Herfindahl Index) — Is institutional positioning spread or concentrated?
    3. IV Skew Analysis — Are institutions bidding up downside protection (Put skew)?
    4. Straddle Premium Compression — Is a volatility breakout imminent?
    5. Relative Volume Surge Detection — Is this move backed by institutional volume or retail noise?
    6. EMA Derivative Slope (Acceleration) — Is the trend accelerating or decelerating?
    7. Composite Algo Edge Score — Single 0-100 number synthesizing all algo-only signals.

These are strictly Helping-Hand computations. Antigravity (the AI CIO) makes the final decision.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class WallFragility:
    """Measures whether the OI Wall is crumbling (covering) or reinforcing (defending)."""
    wall_strike: float
    wall_oi: int
    wall_oi_change: int              # Positive = reinforcing, Negative = covering/crumbling
    fragility_score: float            # 0.0 (fortress) to 1.0 (about to break)
    status: str                       # "FORTRESS", "DEFENDING", "WEAKENING", "CRUMBLING"
    breakout_probability: float       # 0.0 to 1.0: probability that price punches through this wall


@dataclass
class IVSkewSignal:
    """Institutional IV skew — are smart money players hedging downside or upside?"""
    atm_iv: float
    otm_put_iv: float                 # 1-strike OTM Put IV
    otm_call_iv: float                # 1-strike OTM Call IV
    put_call_iv_skew: float           # (OTM_Put_IV - OTM_Call_IV) / ATM_IV — positive = fear
    skew_regime: str                  # "FEAR_HEDGE", "COMPLACENT", "CALL_DEMAND", "NEUTRAL"


@dataclass
class SmartEdgeResult:
    """Complete algo-edge synthesis for a single candidate."""
    symbol: str
    spot: float

    # Wall Dynamics
    call_wall_fragility: WallFragility
    put_wall_fragility: WallFragility

    # OI Concentration
    oi_herfindahl: float              # 0.0 = perfectly distributed, 1.0 = all OI at single strike
    oi_concentration_regime: str      # "CONCENTRATED_PIN", "DISPERSED_TREND", "MODERATE"

    # IV Skew
    iv_skew: IVSkewSignal

    # Straddle Compression
    straddle_premium_pct: float       # ATM straddle as % of spot — lower = compression = breakout imminent
    straddle_regime: str              # "COMPRESSED_BREAKOUT", "ELEVATED_FEAR", "NORMAL"

    # Volume Intelligence
    relative_volume: float            # Today's volume / 20-day avg. >2.0 = institutional surge
    volume_regime: str                # "INSTITUTIONAL_SURGE", "NORMAL", "DEAD_TAPE"

    # EMA Acceleration (derivative of slope)
    ema_20_acceleration: float        # Rate of change of EMA 20 slope — positive = trend accelerating
    acceleration_regime: str          # "ACCELERATING", "STEADY", "DECELERATING", "REVERSING"

    # Composite Edge
    composite_edge_score: float       # 0.0 to 100.0 — algo-only conviction
    edge_verdict: str                 # "STRONG_EDGE", "MODERATE_EDGE", "NO_EDGE", "ADVERSE"
    edge_factors: List[str] = field(default_factory=list)  # Human-readable reasons


def compute_wall_fragility(
    wall_strike: float,
    wall_oi: int,
    wall_oi_change: int,
    spot: float,
    atr: float,
    is_call_wall: bool,
) -> WallFragility:
    """
    Algo-only Wall Fragility: Combines OI change velocity with spot proximity.
    
    Key insight retail misses: A wall with 50 lakh OI that just LOST 8 lakh OI in the
    last hour is about to break. A wall with 20 lakh OI that GAINED 5 lakh is a fortress.
    Retail just sees "50 lakh OI" and thinks it's stronger.
    """
    if wall_oi <= 0:
        return WallFragility(wall_strike, 0, 0, 1.0, "CRUMBLING", 0.5)

    # OI erosion rate: what fraction of the wall has been removed?
    erosion_rate = max(-1.0, min(1.0, -wall_oi_change / max(wall_oi, 1)))

    # Distance factor: closer = more pressure
    dist_pts = abs(wall_strike - spot)
    dist_atr = dist_pts / max(atr, spot * 0.001) if atr > 0 else 10.0

    # Proximity pressure: exponential increase as spot approaches
    proximity_pressure = math.exp(-0.3 * max(0.0, dist_atr)) if dist_atr < 10 else 0.0

    # Fragility = weighted combination of erosion + proximity
    fragility = max(0.0, min(1.0, 0.55 * erosion_rate + 0.45 * proximity_pressure))

    # Breakout probability (logistic function of fragility)
    breakout_p = 1.0 / (1.0 + math.exp(-6.0 * (fragility - 0.5)))

    if fragility >= 0.65 or erosion_rate >= 0.50:
        status = "CRUMBLING"
    elif fragility >= 0.40 or erosion_rate >= 0.20:
        status = "WEAKENING"
    elif erosion_rate <= -0.08:
        status = "DEFENDING"
    else:
        status = "FORTRESS"

    return WallFragility(
        wall_strike=wall_strike,
        wall_oi=wall_oi,
        wall_oi_change=wall_oi_change,
        fragility_score=round(fragility, 3),
        status=status,
        breakout_probability=round(breakout_p, 3),
    )


def compute_oi_herfindahl(chain_data: List[Dict]) -> Tuple[float, str]:
    """
    OI Concentration Index (Herfindahl-Hirschman) across the option chain.
    
    Why retail can't see this: Nuvama shows individual strike OI.
    We compute the DISTRIBUTION. If OI is concentrated at 1-2 strikes,
    Max Pain pinning is very strong. If OI is spread across 10 strikes,
    trending breakouts are more likely.
    
    H = sum(s_i^2) where s_i = OI_i / Total_OI
    H = 1/N means perfectly uniform; H = 1.0 means all at one strike.
    """
    if not chain_data:
        return 0.0, "MODERATE"

    total_oi = sum(d.get("call_oi", 0) + d.get("put_oi", 0) for d in chain_data)
    if total_oi <= 0:
        return 0.0, "MODERATE"

    hhi = 0.0
    for d in chain_data:
        strike_oi = d.get("call_oi", 0) + d.get("put_oi", 0)
        share = strike_oi / total_oi
        hhi += share * share

    # Normalize: for N strikes, min HHI = 1/N; we scale relative to that
    n = max(len(chain_data), 1)
    if n <= 2:
        # 1-2 strike chain is by definition maximally concentrated
        normalized = 1.0 if n == 1 else hhi
    else:
        min_hhi = 1.0 / n
        normalized = (hhi - min_hhi) / (1.0 - min_hhi) if (1.0 - min_hhi) > 0 else 0.0

    if normalized > 0.35:
        regime = "CONCENTRATED_PIN"
    elif normalized < 0.10:
        regime = "DISPERSED_TREND"
    else:
        regime = "MODERATE"

    return round(normalized, 4), regime


def compute_iv_skew(
    chain_data: List[Dict],
    spot: float,
    strike_step: float,
    dte_days: float = 3.0,
) -> IVSkewSignal:
    """
    IV Skew: Compares implied volatility of OTM Puts vs OTM Calls.
    
    Uses Brenner-Subrahmanyam approximation scaled by sqrt(T) so the IV proxy
    and skew maintain high mathematical resolution across 0-DTE, weekly, and monthly options.
    """
    atm_iv = 0.15
    otm_put_iv = 0.15
    otm_call_iv = 0.15

    if not chain_data or spot <= 0:
        return IVSkewSignal(atm_iv, otm_put_iv, otm_call_iv, 0.0, "NEUTRAL")

    # Time to expiry in years
    t = max(0.15 / 365.0, float(dte_days) / 365.0)
    bs_denom = max(1.0, spot * math.sqrt(t) * 0.7979)

    # Find ATM, 1-OTM-Put, 1-OTM-Call strikes
    sorted_chain = sorted(chain_data, key=lambda s: abs(s.get("strike", 0) - spot))
    atm = sorted_chain[0] if sorted_chain else None

    p_prem = 0.0
    c_prem = 0.0
    atm_avg_prem = spot * 0.015

    if atm:
        c_ltp = float(atm.get("call_ltp", 0.0) or 0.0)
        p_ltp = float(atm.get("put_ltp", 0.0) or 0.0)
        if (c_ltp + p_ltp) > 0:
            atm_avg_prem = (c_ltp + p_ltp) / 2.0
        atm_iv = max(0.04, min(1.50, atm_avg_prem / bs_denom))

    # OTM Put: one strike below ATM
    otm_put_strike = (atm.get("strike", spot) if atm else spot) - strike_step
    otm_put = next((s for s in chain_data if abs(s.get("strike", 0) - otm_put_strike) < 0.01), None)
    if otm_put:
        p_prem = float(otm_put.get("put_ltp", 0.0) or 0.0)
        if p_prem > 0:
            otm_put_iv = max(0.04, min(1.50, p_prem / bs_denom))

    # OTM Call: one strike above ATM
    otm_call_strike = (atm.get("strike", spot) if atm else spot) + strike_step
    otm_call = next((s for s in chain_data if abs(s.get("strike", 0) - otm_call_strike) < 0.01), None)
    if otm_call:
        c_prem = float(otm_call.get("call_ltp", 0.0) or 0.0)
        if c_prem > 0:
            otm_call_iv = max(0.04, min(1.50, c_prem / bs_denom))

    # Direct premium skew preserves full resolution even on low-premium/0-DTE options
    if atm_avg_prem > 0 and (p_prem > 0 or c_prem > 0):
        skew = round((p_prem - c_prem) / max(0.5, atm_avg_prem), 4)
    else:
        skew = round((otm_put_iv - otm_call_iv) / max(0.05, atm_iv), 4)

    if skew > 0.15:
        regime = "FEAR_HEDGE"       # Institutions buying puts aggressively
    elif skew < -0.10:
        regime = "CALL_DEMAND"      # Aggressive call buying (breakout positioning)
    elif abs(skew) < 0.05:
        regime = "COMPLACENT"       # No directional hedging
    else:
        regime = "NEUTRAL"

    return IVSkewSignal(
        atm_iv=round(atm_iv, 4),
        otm_put_iv=round(otm_put_iv, 4),
        otm_call_iv=round(otm_call_iv, 4),
        put_call_iv_skew=skew,
        skew_regime=regime,
    )


def compute_straddle_compression(
    atm_straddle_premium: float,
    spot: float,
    realized_vol_annual: float,
    dte_days: float = 4.0,
) -> Tuple[float, str]:
    """
    Straddle Premium as % of Spot vs Expected Move — detects volatility compression.
    
    Dynamically scales expected straddle by sqrt(DTE/365) so 0-DTE, weekly,
    and 30-DTE monthly options are evaluated fairly without false ELEVATED_FEAR alerts.
    """
    if spot <= 0:
        return 0.0, "NORMAL"

    straddle_pct = round((atm_straddle_premium / spot) * 100.0, 3)
    t = max(0.15 / 365.0, float(dte_days) / 365.0)
    # Expected ATM straddle % from realized vol over time T: E[S_atm / Spot] ≈ σ * sqrt(T) * sqrt(2/π)
    expected_straddle_pct = max(0.08, realized_vol_annual) * math.sqrt(t) * 0.7979 * 100.0

    ratio = straddle_pct / max(0.1, expected_straddle_pct)

    if ratio < 0.72:
        regime = "COMPRESSED_BREAKOUT"    # Market underpricing volatility (breakout alert)
    elif ratio > 1.45:
        regime = "ELEVATED_FEAR"          # Expensive options, IV crush risk
    else:
        regime = "NORMAL"

    return straddle_pct, regime


def compute_relative_volume(
    current_volume: int,
    avg_volume_20d: int,
) -> Tuple[float, str]:
    """
    Relative Volume: Today's traded volume vs 20-day average.
    
    Why: A +2% stock move on 0.5x relative volume is retail noise.
    A +2% stock move on 3.5x relative volume is institutional accumulation.
    Retail sees "volume bars" on charts; we compute the statistical ratio.
    """
    if avg_volume_20d <= 0:
        return 1.0, "NORMAL"

    rvol = round(current_volume / avg_volume_20d, 2)

    if rvol >= 2.5:
        regime = "INSTITUTIONAL_SURGE"
    elif rvol >= 1.5:
        regime = "ELEVATED"
    elif rvol < 0.6:
        regime = "DEAD_TAPE"
    else:
        regime = "NORMAL"

    return rvol, regime


def compute_ema_acceleration(
    ema_20_series: List[float],
    atr: float,
) -> Tuple[float, str]:
    """
    EMA-20 Second Derivative (Acceleration).
    
    Why retail can't compute this: They look at EMA slope (1st derivative).
    We compute the CHANGE in slope (2nd derivative = acceleration).
    
    - Positive acceleration: Trend is SPEEDING UP (best entry window)
    - Zero acceleration: Trend at constant speed (hold/trail)  
    - Negative acceleration: Trend SLOWING DOWN (prepare for exit)
    
    This is literally calculus applied to market data at machine speed.
    """
    if len(ema_20_series) < 12 or atr <= 0:
        return 0.0, "STEADY"

    # Slope at t (using last 5 bars)
    slope_now = (ema_20_series[-1] - ema_20_series[-5]) / atr
    # Slope at t-5 (using bars -10 to -6)
    slope_prev = (ema_20_series[-6] - ema_20_series[-10]) / atr

    accel = round(slope_now - slope_prev, 3)

    if accel > 0.15:
        regime = "ACCELERATING"
    elif accel < -0.35:
        regime = "REVERSING"       # Must check before -0.15 so it's reachable
    elif accel < -0.15:
        regime = "DECELERATING"
    else:
        regime = "STEADY"

    return accel, regime


def compute_composite_edge(
    call_wall_frag: WallFragility,
    put_wall_frag: WallFragility,
    oi_hhi: float,
    oi_regime: str,
    iv_skew: IVSkewSignal,
    straddle_pct: float,
    straddle_regime: str,
    rvol: float,
    vol_regime: str,
    accel: float,
    accel_regime: str,
    is_ce: bool,
    z_ofi: float,
    kobp: float,
    mc_pot_pct: float,
) -> Tuple[float, str, List[str]]:
    """
    Synthesizes all algo-only signals into a single Composite Edge Score (0-100).
    
    This is the CORE algo advantage: no retail trader can mentally compute
    7 mathematical dimensions simultaneously and weight them in real-time.
    We do it in microseconds across 218 stocks.
    """
    score = 50.0  # Neutral baseline
    factors: List[str] = []

    # 1. Wall Fragility (is our target wall about to break?)
    target_wall = call_wall_frag if is_ce else put_wall_frag
    if target_wall.status == "CRUMBLING":
        score += 15.0
        factors.append(f"Target wall CRUMBLING (frag={target_wall.fragility_score:.2f}, breakout_p={target_wall.breakout_probability:.0%})")
    elif target_wall.status == "WEAKENING":
        score += 8.0
        factors.append(f"Target wall weakening (frag={target_wall.fragility_score:.2f})")
    elif target_wall.status == "FORTRESS":
        score -= 10.0
        factors.append(f"Target wall FORTRESS — limited upside runway")

    # Backstop wall strength (is our support holding?)
    support_wall = put_wall_frag if is_ce else call_wall_frag
    if support_wall.status in ("FORTRESS", "DEFENDING"):
        score += 5.0
        factors.append(f"Support wall solid ({support_wall.status})")
    elif support_wall.status == "CRUMBLING":
        score -= 8.0
        factors.append(f"⚠ Support wall CRUMBLING — stop-loss vulnerable")

    # 2. OI Concentration
    if oi_regime == "DISPERSED_TREND":
        score += 6.0
        factors.append("OI dispersed — trend continuation likely")
    elif oi_regime == "CONCENTRATED_PIN":
        score -= 5.0
        factors.append("OI concentrated — Max Pain pinning risk")

    # 3. IV Skew
    if is_ce and iv_skew.skew_regime == "COMPLACENT":
        score += 5.0
        factors.append("IV complacent — cheap calls, surprise upside potential")
    elif is_ce and iv_skew.skew_regime == "FEAR_HEDGE":
        score -= 4.0
        factors.append("IV skewed to puts — institutional fear hedging")
    elif not is_ce and iv_skew.skew_regime == "FEAR_HEDGE":
        score += 7.0
        factors.append("IV fear skew CONFIRMS put direction")

    # 4. Straddle Compression
    if straddle_regime == "COMPRESSED_BREAKOUT":
        score += 8.0
        factors.append(f"Straddle compressed ({straddle_pct:.2f}% of spot) — breakout imminent")
    elif straddle_regime == "ELEVATED_FEAR":
        score -= 6.0
        factors.append(f"Straddle elevated ({straddle_pct:.2f}%) — IV crush risk on entry")

    # 5. Relative Volume
    if vol_regime == "INSTITUTIONAL_SURGE":
        score += 10.0
        factors.append(f"Relative volume {rvol:.1f}x — institutional accumulation")
    elif vol_regime == "DEAD_TAPE":
        score -= 12.0
        factors.append(f"Relative volume {rvol:.1f}x — DEAD TAPE, no institutional backing")

    # 6. EMA Acceleration
    if accel_regime == "ACCELERATING":
        score += 7.0
        factors.append(f"Trend ACCELERATING (a={accel:+.3f} ATR/bar²)")
    elif accel_regime in ("DECELERATING", "REVERSING"):
        score -= 7.0
        factors.append(f"Trend {accel_regime} (a={accel:+.3f}) — momentum exhaustion")

    # 7. Order Flow confirmation
    if abs(z_ofi) >= 2.0:
        ofi_dir = "bullish" if z_ofi > 0 else "bearish"
        correct_dir = (z_ofi > 0 and is_ce) or (z_ofi < 0 and not is_ce)
        if correct_dir:
            score += 8.0
            factors.append(f"Strong {ofi_dir} order flow Z_OFI={z_ofi:+.1f}")
        else:
            score -= 10.0
            factors.append(f"⚠ OPPOSING order flow Z_OFI={z_ofi:+.1f} against trade direction")

    # 8. KOBP confirmation
    if abs(kobp) > 0.25:
        correct_kobp = (kobp > 0 and is_ce) or (kobp < 0 and not is_ce)
        if correct_kobp:
            score += 4.0
        else:
            score -= 5.0
            factors.append(f"KOBP opposing: {kobp:+.2f}")

    # 9. Monte Carlo Probability
    if mc_pot_pct >= 55.0:
        score += 5.0
        factors.append(f"MC simulation {mc_pot_pct:.0f}% PoT — positive EV")
    elif mc_pot_pct < 35.0:
        score -= 5.0
        factors.append(f"MC simulation only {mc_pot_pct:.0f}% PoT — marginal EV")

    # Clamp and classify
    score = max(0.0, min(100.0, score))

    if score >= 72.0:
        verdict = "STRONG_EDGE"
    elif score >= 55.0:
        verdict = "MODERATE_EDGE"
    elif score >= 40.0:
        verdict = "NO_EDGE"
    else:
        verdict = "ADVERSE"

    return round(score, 1), verdict, factors


def evaluate_smart_edge(
    symbol: str,
    spot: float,
    chain_data: List[Dict],
    strike_step: float,
    tech_indicators: Dict,
    quant_alpha: Dict,
    opt_analytics: Dict,
    mc_result: Dict,
    is_ce: bool,
    current_volume: int = 0,
    avg_volume_20d: int = 0,
    dte_days: float = 4.0,
) -> SmartEdgeResult:
    """
    Master entry point: Computes the full Smart Algo Edge for a single stock candidate.
    
    This is the mathematical layer that makes our Nuvama/Sensibull replicas 'smart' —
    not just copying what retail sees, but computing the DERIVED signals that only
    an algorithmic system running 218 stocks in parallel can produce.
    """
    atr = tech_indicators.get("atr_5m", spot * 0.001)
    rv = tech_indicators.get("realized_vol_annual", 0.22)

    # 1. Wall Fragility
    call_wall_frag = compute_wall_fragility(
        wall_strike=opt_analytics.get("call_wall_strike", 0.0),
        wall_oi=opt_analytics.get("call_wall_oi", 0),
        wall_oi_change=opt_analytics.get("call_wall_oi_change", 0),
        spot=spot, atr=atr, is_call_wall=True,
    )
    put_wall_frag = compute_wall_fragility(
        wall_strike=opt_analytics.get("put_wall_strike", 0.0),
        wall_oi=opt_analytics.get("put_wall_oi", 0),
        wall_oi_change=opt_analytics.get("put_wall_oi_change", 0),
        spot=spot, atr=atr, is_call_wall=False,
    )

    # 2. OI Concentration
    oi_hhi, oi_regime = compute_oi_herfindahl(chain_data)

    # 3. IV Skew
    iv_skew = compute_iv_skew(chain_data, spot, strike_step, dte_days=dte_days)

    # 4. Straddle Compression
    straddle_prem = opt_analytics.get("atm_straddle_premium", 0.0)
    straddle_pct, straddle_regime = compute_straddle_compression(straddle_prem, spot, rv, dte_days=dte_days)

    # 5. Relative Volume
    rvol, vol_regime = compute_relative_volume(current_volume, avg_volume_20d)

    # 6. EMA Acceleration
    ema_20_series = tech_indicators.get("ema_20_series", [])
    accel, accel_regime = compute_ema_acceleration(ema_20_series, atr)

    # 7. Composite Edge
    z_ofi = quant_alpha.get("z_ofi", 0.0)
    kobp = quant_alpha.get("kobp", 0.0)
    mc_pot = mc_result.get("p_win_target", 0.5) * 100.0

    edge_score, edge_verdict, edge_factors = compute_composite_edge(
        call_wall_frag=call_wall_frag,
        put_wall_frag=put_wall_frag,
        oi_hhi=oi_hhi,
        oi_regime=oi_regime,
        iv_skew=iv_skew,
        straddle_pct=straddle_pct,
        straddle_regime=straddle_regime,
        rvol=rvol,
        vol_regime=vol_regime,
        accel=accel,
        accel_regime=accel_regime,
        is_ce=is_ce,
        z_ofi=z_ofi,
        kobp=kobp,
        mc_pot_pct=mc_pot,
    )

    return SmartEdgeResult(
        symbol=symbol,
        spot=spot,
        call_wall_fragility=call_wall_frag,
        put_wall_fragility=put_wall_frag,
        oi_herfindahl=oi_hhi,
        oi_concentration_regime=oi_regime,
        iv_skew=iv_skew,
        straddle_premium_pct=straddle_pct,
        straddle_regime=straddle_regime,
        relative_volume=rvol,
        volume_regime=vol_regime,
        ema_20_acceleration=accel,
        acceleration_regime=accel_regime,
        composite_edge_score=edge_score,
        edge_verdict=edge_verdict,
        edge_factors=edge_factors,
    )
