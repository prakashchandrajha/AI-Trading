"""
Nuvama Wealth-Style Options Analytics Engine
Calculates institutional Open Interest (OI) metrics:
1. Max Pain (the strike where option writers make maximum payout / expiry magnet)
2. Put-Call Ratio (PCR) - Total and Strike-wise
3. Call Wall (Major Resistance) & Put Wall (Major Support)
4. Institutional Flow & Regimes (Unwinding vs Writing)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class OptionStrikeData:
    strike: float
    call_oi: int = 0
    put_oi: int = 0
    call_ltp: float = 0.0
    put_ltp: float = 0.0
    call_change_oi: int = 0
    put_change_oi: int = 0
    call_volume: int = 0
    put_volume: int = 0
    call_bid: float = 0.0
    call_ask: float = 0.0
    put_bid: float = 0.0
    put_ask: float = 0.0


@dataclass
class NuvamaAnalyticsResult:
    underlying_price: float
    max_pain_strike: float
    effective_max_pain_weight: float   # DTE-scaled weight (0.15 on 4-DTE to 0.95 on 0-DTE)
    pcr: float
    pcr_divergence: str                # "BULLISH_DIVERGENCE", "BEARISH_DIVERGENCE", "CONFIRMING", "NONE"
    call_wall_strike: float            # Major Resistance
    call_wall_oi: int
    put_wall_strike: float             # Major Support
    put_wall_oi: int
    total_call_oi: int
    total_put_oi: int
    near_atm_oi_velocity: float        # Net 15-min OI acceleration (Put addition - Call addition)
    sentiment: str                     # "BULLISH_OVERSOLD", "BEARISH_OVERBOUGHT", "BULLISH_FLOW", "BEARISH_FLOW", "NEUTRAL"
    trade_recommendation: str          # "BUY_CE", "BUY_PE", "STAND_ASIDE"
    confidence: float
    structural_stop_index: float       # Exact institutional invalidation level in Spot points
    structural_target_index: float     # Target level (opposite wall or Max Pain)
    atm_straddle_premium: float = 0.0  # Combined ATM Call + Put price (Opstra Breakeven measure)
    expected_breakeven_low: float = 0.0 # Lower Market Maker breakeven boundary
    expected_breakeven_high: float = 0.0 # Upper Market Maker breakeven boundary
    analysis_thesis: str = ""
    key_strikes: List[Dict] = field(default_factory=list)


class NuvamaOptionsEngine:
    """
    Independent analytics engine inspired by Nuvama Wealth's institutional derivatives dashboard.
    Supports NIFTY, BANKNIFTY, and MIDCPNIFTY (Nifty Midcap Select).
    """

    SYMBOL_SPECS = {
        "NIFTY": {
            "lot_size": 25,
            "strike_step": 50.0,
            "near_range": 300.0,
            "stop_buffer": 25.0,
            "wall_proximity": 60.0,
            "expiry_day": "Thursday / Monthly",
        },
        "BANKNIFTY": {
            "lot_size": 15,
            "strike_step": 100.0,
            "near_range": 600.0,
            "stop_buffer": 60.0,
            "wall_proximity": 120.0,
            "expiry_day": "Wednesday/Monthly",
        },
        "MIDCPNIFTY": {
            "lot_size": 120,
            "strike_step": 25.0,
            "near_range": 150.0,
            "stop_buffer": 15.0,
            "wall_proximity": 30.0,
            "expiry_day": "Monday",
        },
        "HDFCBANK": {
            "lot_size": 550,
            "strike_step": 10.0,
            "near_range": 50.0,
            "stop_buffer": 6.0,
            "wall_proximity": 10.0,
            "expiry_day": "Monthly (Last Thursday)",
        },
        "RELIANCE": {
            "lot_size": 250,
            "strike_step": 20.0,
            "near_range": 80.0,
            "stop_buffer": 10.0,
            "wall_proximity": 15.0,
            "expiry_day": "Monthly (Last Thursday)",
        },
        "ICICIBANK": {
            "lot_size": 700,
            "strike_step": 10.0,
            "near_range": 50.0,
            "stop_buffer": 5.0,
            "wall_proximity": 10.0,
            "expiry_day": "Monthly (Last Thursday)",
        },
        "SBIN": {
            "lot_size": 750,
            "strike_step": 10.0,
            "near_range": 40.0,
            "stop_buffer": 5.0,
            "wall_proximity": 8.0,
            "expiry_day": "Monthly (Last Thursday)",
        },
        "TATAMOTORS": {
            "lot_size": 700,
            "strike_step": 10.0,
            "near_range": 40.0,
            "stop_buffer": 6.0,
            "wall_proximity": 8.0,
            "expiry_day": "Monthly (Last Thursday)",
        },
        "INFY": {
            "lot_size": 400,
            "strike_step": 20.0,
            "near_range": 80.0,
            "stop_buffer": 10.0,
            "wall_proximity": 15.0,
            "expiry_day": "Monthly (Last Thursday)",
        },
        "TCS": {
            "lot_size": 175,
            "strike_step": 20.0,
            "near_range": 80.0,
            "stop_buffer": 15.0,
            "wall_proximity": 20.0,
            "expiry_day": "Monthly (Last Thursday)",
        },
        "TATASTEEL": {
            "lot_size": 5500,
            "strike_step": 2.5,
            "near_range": 10.0,
            "stop_buffer": 1.5,
            "wall_proximity": 2.5,
            "expiry_day": "Monthly (Last Thursday)",
        },
        "HINDALCO": {
            "lot_size": 1400,
            "strike_step": 10.0,
            "near_range": 30.0,
            "stop_buffer": 4.0,
            "wall_proximity": 8.0,
            "expiry_day": "Monthly (Last Thursday)",
        },
        "M&M": {
            "lot_size": 350,
            "strike_step": 20.0,
            "near_range": 80.0,
            "stop_buffer": 15.0,
            "wall_proximity": 20.0,
            "expiry_day": "Monthly (Last Thursday)",
        },
        "MARUTI": {
            "lot_size": 50,
            "strike_step": 100.0,
            "near_range": 400.0,
            "stop_buffer": 80.0,
            "wall_proximity": 100.0,
            "expiry_day": "Monthly (Last Thursday)",
        },
        "HAL": {
            "lot_size": 150,
            "strike_step": 50.0,
            "near_range": 200.0,
            "stop_buffer": 35.0,
            "wall_proximity": 50.0,
            "expiry_day": "Monthly (Last Thursday)",
        },
        "BEL": {
            "lot_size": 2850,
            "strike_step": 5.0,
            "near_range": 20.0,
            "stop_buffer": 2.5,
            "wall_proximity": 5.0,
            "expiry_day": "Monthly (Last Thursday)",
        },
        "LT": {
            "lot_size": 150,
            "strike_step": 20.0,
            "near_range": 100.0,
            "stop_buffer": 20.0,
            "wall_proximity": 25.0,
            "expiry_day": "Monthly (Last Thursday)",
        },
        "SUNPHARMA": {
            "lot_size": 350,
            "strike_step": 20.0,
            "near_range": 80.0,
            "stop_buffer": 12.0,
            "wall_proximity": 15.0,
            "expiry_day": "Monthly (Last Thursday)",
        },
        "CIPLA": {
            "lot_size": 650,
            "strike_step": 10.0,
            "near_range": 50.0,
            "stop_buffer": 8.0,
            "wall_proximity": 10.0,
            "expiry_day": "Monthly (Last Thursday)",
        },
        "NTPC": {
            "lot_size": 1500,
            "strike_step": 5.0,
            "near_range": 20.0,
            "stop_buffer": 3.0,
            "wall_proximity": 5.0,
            "expiry_day": "Monthly (Last Thursday)",
        },
        "ONGC": {
            "lot_size": 3850,
            "strike_step": 2.5,
            "near_range": 12.0,
            "stop_buffer": 2.0,
            "wall_proximity": 3.0,
            "expiry_day": "Monthly (Last Thursday)",
        },
        "ITC": {
            "lot_size": 1600,
            "strike_step": 5.0,
            "near_range": 25.0,
            "stop_buffer": 3.5,
            "wall_proximity": 5.0,
            "expiry_day": "Monthly (Last Thursday)",
        },
        "BHARTIARTL": {
            "lot_size": 475,
            "strike_step": 20.0,
            "near_range": 80.0,
            "stop_buffer": 12.0,
            "wall_proximity": 15.0,
            "expiry_day": "Monthly (Last Thursday)",
        },
    }

    SECTOR_MAP = {
        "METALS": ["TATASTEEL", "HINDALCO"],
        "AUTO": ["TATAMOTORS", "M&M", "MARUTI"],
        "DEFENSE_CAPGOODS": ["HAL", "BEL", "LT"],
        "PHARMA": ["SUNPHARMA", "CIPLA"],
        "IT": ["TCS", "INFY"],
        "BANKING": ["HDFCBANK", "ICICIBANK", "SBIN"],
        "ENERGY": ["RELIANCE", "NTPC", "ONGC"],
        "FMCG": ["ITC"],
        "TELECOM": ["BHARTIARTL"],
    }

    @staticmethod
    def classify_buildup(price_change: float, oi_change: int) -> str:
        """
        Nuvama 4-Quadrant Derivative Buildup Classifier:
        1. Long Buildup   : Price UP,   OI UP    (Aggressive institutional buying)
        2. Short Buildup  : Price DOWN, OI UP    (Aggressive option writing / shorting)
        3. Short Covering : Price UP,   OI DOWN  (Writers / bears covering in panic)
        4. Long Unwinding : Price DOWN, OI DOWN  (Bulls liquidating / closing positions)
        """
        if oi_change > 0:
            return "Long Buildup" if price_change >= 0 else "Short Buildup"
        elif oi_change < 0:
            return "Short Covering" if price_change >= 0 else "Long Unwinding"
        return "Neutral"

    @staticmethod
    def calculate_max_pain(strikes_data: List[OptionStrikeData]) -> float:
        """
        Calculates the Max Pain strike:
        Total Payout at price S = sum(max(0, S - K) * Call_OI) + sum(max(0, K - S) * Put_OI)
        The strike that minimizes total payout is the Max Pain level.
        """
        if not strikes_data:
            return 0.0

        all_strikes = sorted([s.strike for s in strikes_data])
        min_payout = float("inf")
        best_strike = all_strikes[0]

        for test_price in all_strikes:
            total_payout = 0.0
            for item in strikes_data:
                # Call payout if expiry is at test_price: max(0, test_price - strike) * Call_OI
                if test_price > item.strike:
                    total_payout += (test_price - item.strike) * item.call_oi
                # Put payout if expiry is at test_price: max(0, strike - test_price) * Put_OI
                elif test_price < item.strike:
                    total_payout += (item.strike - test_price) * item.put_oi

            if total_payout < min_payout:
                min_payout = total_payout
                best_strike = test_price

        return best_strike

    @classmethod
    def analyze(
        cls,
        underlying_price: float,
        chain: List[OptionStrikeData],
        symbol: str = "NIFTY",
        dte: float = 3.0,
        prev_underlying: Optional[float] = None,
        prev_pcr: Optional[float] = None,
    ) -> NuvamaAnalyticsResult:
        if not chain or underlying_price <= 0.0:
            return NuvamaAnalyticsResult(
                underlying_price=underlying_price,
                max_pain_strike=0.0,
                effective_max_pain_weight=0.0,
                pcr=0.0,
                pcr_divergence="NONE",
                call_wall_strike=0.0,
                call_wall_oi=0,
                put_wall_strike=0.0,
                put_wall_oi=0,
                total_call_oi=0,
                total_put_oi=0,
                near_atm_oi_velocity=0.0,
                sentiment="NEUTRAL",
                trade_recommendation="STAND_ASIDE",
                confidence=0.0,
                structural_stop_index=0.0,
                structural_target_index=0.0,
                analysis_thesis="Insufficient option chain data.",
            )

        # 1. Total OI and PCR
        total_call_oi = sum(s.call_oi for s in chain)
        total_put_oi = sum(s.put_oi for s in chain)
        pcr = round(total_put_oi / total_call_oi, 2) if total_call_oi > 0 else 1.0

        # 2. Max Pain & DTE Gravitational Weight
        max_pain = cls.calculate_max_pain(chain)
        # On Monday (3-DTE), weight is ~13%; on Expiry afternoon (0-DTE), weight is 95%+
        effective_max_pain_weight = round(math.exp(-max(0.0, dte) / 1.5), 2)

        # 3. Call Wall (Resistance) and Put Wall (Support)
        call_wall = max(chain, key=lambda s: s.call_oi)
        put_wall = max(chain, key=lambda s: s.put_oi)

        # 4. Opstra-Style ATM Straddle & Market Maker Breakeven Cone
        atm_strike_obj = min(chain, key=lambda s: abs(s.strike - underlying_price))
        atm_straddle = round(atm_strike_obj.call_ltp + atm_strike_obj.put_ltp, 2)
        expected_low = round(atm_strike_obj.strike - atm_straddle, 1)
        expected_high = round(atm_strike_obj.strike + atm_straddle, 1)

        # 5. Institutional Flow & Change in OI near ATM (+/- near_range)
        spec = cls.SYMBOL_SPECS.get(symbol.upper(), cls.SYMBOL_SPECS["NIFTY"])
        stop_buffer = spec["stop_buffer"]
        wall_proximity = spec["wall_proximity"]

        atm_strike = min(chain, key=lambda s: abs(s.strike - underlying_price)).strike
        near_strikes = [s for s in chain if abs(s.strike - atm_strike) <= spec["near_range"]]

        net_call_change = sum(s.call_change_oi for s in near_strikes)
        net_put_change = sum(s.put_change_oi for s in near_strikes)
        near_atm_oi_velocity = float(net_put_change - net_call_change)

        # 5. PCR Divergence (Leading Reversal Signal)
        pcr_divergence = "CONFIRMING"
        if prev_underlying is not None and prev_pcr is not None:
            if underlying_price < prev_underlying and pcr > prev_pcr + 0.03:
                pcr_divergence = "BULLISH_DIVERGENCE"
            elif underlying_price > prev_underlying and pcr < prev_pcr - 0.03:
                pcr_divergence = "BEARISH_DIVERGENCE"

        # 6. Smart-Money Regime Synthesis & Invalidation Anchors
        dist_to_put_wall = underlying_price - put_wall.strike
        dist_to_call_wall = call_wall.strike - underlying_price

        sentiment = "NEUTRAL"
        recommendation = "STAND_ASIDE"
        confidence = 0.50
        structural_stop = 0.0
        structural_target = 0.0
        thesis_lines = []

        # Case A: Oversold Bounce at Put Wall (Confirmed by PCR or Divergence)
        if (dist_to_put_wall >= 0 and dist_to_put_wall <= wall_proximity and pcr <= 0.75) or pcr_divergence == "BULLISH_DIVERGENCE":
            sentiment = "BULLISH_OVERSOLD"
            recommendation = "BUY_CE"
            confidence = 0.88 if pcr_divergence == "BULLISH_DIVERGENCE" else 0.82
            structural_stop = put_wall.strike - stop_buffer
            structural_target = call_wall.strike if effective_max_pain_weight < 0.4 else max_pain
            thesis_lines.append(
                f"{symbol.upper()} ({underlying_price:.1f}) defending Put Wall at {put_wall.strike:.0f}. "
                f"PCR={pcr:.2f} ({pcr_divergence}). Structural Stop: {structural_stop:.0f}, Target: {structural_target:.0f}."
            )
        # Case B: Overbought Rejection at Call Wall
        elif (dist_to_call_wall >= 0 and dist_to_call_wall <= wall_proximity and pcr >= 1.30) or pcr_divergence == "BEARISH_DIVERGENCE":
            sentiment = "BEARISH_OVERBOUGHT"
            recommendation = "BUY_PE"
            confidence = 0.88 if pcr_divergence == "BEARISH_DIVERGENCE" else 0.82
            structural_stop = call_wall.strike + stop_buffer
            structural_target = put_wall.strike if effective_max_pain_weight < 0.4 else max_pain
            thesis_lines.append(
                f"{symbol.upper()} ({underlying_price:.1f}) hitting Call Wall at {call_wall.strike:.0f}. "
                f"PCR={pcr:.2f} ({pcr_divergence}). Structural Stop: {structural_stop:.0f}, Target: {structural_target:.0f}."
            )
        # Case C: Institutional Call Unwinding Breakout
        elif net_call_change < 0 and net_put_change > 0:
            sentiment = "BULLISH_FLOW"
            recommendation = "BUY_CE"
            confidence = 0.80
            structural_stop = put_wall.strike - stop_buffer
            structural_target = call_wall.strike
            thesis_lines.append(
                f"[{symbol.upper()}] Call writers covering (-{abs(net_call_change):,} OI) while Put writers add (+{net_put_change:,} OI). "
                f"OI Velocity: +{near_atm_oi_velocity:,.0f} net put bias."
            )
        # Case D: Institutional Put Panicking Breakdown
        elif net_put_change < 0 and net_call_change > 0:
            sentiment = "BEARISH_FLOW"
            recommendation = "BUY_PE"
            confidence = 0.80
            structural_stop = call_wall.strike + stop_buffer
            structural_target = put_wall.strike
            thesis_lines.append(
                f"[{symbol.upper()}] Put writers panicking (-{abs(net_put_change):,} OI) while Call writers add (+{net_call_change:,} OI). "
                f"OI Velocity: {near_atm_oi_velocity:,.0f} net call bias."
            )
        else:
            thesis_lines.append(
                f"[{symbol.upper()}] Market rangebound between Put Wall {put_wall.strike:.0f} and Call Wall {call_wall.strike:.0f}. "
                f"PCR={pcr:.2f}, Max Pain={max_pain:.0f} (Weight: {effective_max_pain_weight*100:.0f}%). Stand aside."
            )

        thesis = " | ".join(thesis_lines)

        # Snapshot of key strikes for display with Nuvama buildup
        key_strikes = [
            {
                "strike": s.strike,
                "call_oi": s.call_oi,
                "put_oi": s.put_oi,
                "call_ltp": s.call_ltp,
                "put_ltp": s.put_ltp,
                "call_change_oi": s.call_change_oi,
                "put_change_oi": s.put_change_oi,
                "call_buildup": cls.classify_buildup(1.0 if s.call_change_oi > 0 else -1.0, s.call_change_oi),
                "put_buildup": cls.classify_buildup(1.0 if s.put_change_oi > 0 else -1.0, s.put_change_oi),
            }
            for s in sorted(chain, key=lambda s: abs(s.strike - underlying_price))[:7]
        ]

        return NuvamaAnalyticsResult(
            underlying_price=underlying_price,
            max_pain_strike=max_pain,
            effective_max_pain_weight=effective_max_pain_weight,
            pcr=pcr,
            pcr_divergence=pcr_divergence,
            call_wall_strike=call_wall.strike,
            call_wall_oi=call_wall.call_oi,
            put_wall_strike=put_wall.strike,
            put_wall_oi=put_wall.put_oi,
            total_call_oi=total_call_oi,
            total_put_oi=total_put_oi,
            near_atm_oi_velocity=near_atm_oi_velocity,
            sentiment=sentiment,
            trade_recommendation=recommendation,
            confidence=confidence,
            structural_stop_index=structural_stop,
            structural_target_index=structural_target,
            atm_straddle_premium=atm_straddle,
            expected_breakeven_low=expected_low,
            expected_breakeven_high=expected_high,
            analysis_thesis=thesis,
            key_strikes=key_strikes,
        )


OptionsAnalyticsEngine = NuvamaOptionsEngine
OptionsAnalyticsResult = NuvamaAnalyticsResult


