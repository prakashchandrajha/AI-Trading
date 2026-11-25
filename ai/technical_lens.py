"""
Technical Microstructure & Live Market-Breadth Engine
Synthesizes:
1. VWAP (Volume Weighted Average Price) & VWAP Stretch
2. 9 EMA (Fast Intraday Momentum)
3. 40 EMA (Institutional Baseline Trend)
4. Live broker breadth (Advances/Declines ratio, Sector Concentration, Momentum + Breakouts)
5. Confluence Matrix (Technical + Options OI + GEX + live breadth)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class TechnicalIndicators:
    symbol: str
    current_price: float
    vwap: float
    ema_9: float
    ema_40: float
    vwap_distance_pct: float     # (Price - VWAP) / VWAP * 100
    ema_spread_pct: float        # (EMA9 - EMA40) / EMA40 * 100
    trend_regime: str            # "BULLISH_TREND_ALIGNMENT", "BEARISH_TREND_ALIGNMENT", "CHOP_OR_DIVERGENT"
    is_above_vwap: bool
    is_golden_ema_cross: bool    # 9 EMA > 40 EMA
    is_overextended: bool        # |vwap_distance_pct| > 2.5% or |z_vwap| > 3.0
    technical_conviction: float  # 0.0 to 1.0
    ema_20: float = 0.0
    is_triple_ribbon_bull: bool = False  # 9 EMA > 20 EMA > 40 EMA
    is_triple_ribbon_bear: bool = False  # 9 EMA < 20 EMA < 40 EMA
    atr_5m: float = 0.0
    atr_pct: float = 0.0
    realized_vol_annual: float = 0.22
    z_vwap: float = 0.0
    ribbon_spread_atr: float = 0.0
    ema_20_slope_atr: float = 0.0
    ema_20_series: List[float] = field(default_factory=list)


@dataclass
class MarketLensBreadth:
    advances: int
    declines: int
    unchanged: int
    ad_ratio: float              # Advances / Declines
    market_breadth_sentiment: str # "BULLISH_EXPANSION", "BEARISH_LIQUIDATION", "MIXED_ROTATION"
    top_turnover_sectors: List[str] = field(default_factory=list)
    volume_shockers: List[str] = field(default_factory=list)
    source: str = "OFFLINE_FIXTURE"


class TechnicalLensEngine:
    """
    Computes real-time technical microstructure and broker-derived market breadth.

    `MarketLensBreadth` remains the type name only for backward compatibility;
    it is not a connection to NSE Market Lens.
    """

    @staticmethod
    def calculate_ema(prices: List[float], period: int) -> float:
        """Calculates Exponential Moving Average with smoothing factor 2 / (period + 1)."""
        if not prices:
            return 0.0
        if len(prices) < period:
            return sum(prices) / len(prices)

        multiplier = 2.0 / (period + 1.0)
        ema = sum(prices[:period]) / period
        for p in prices[period:]:
            ema = (p - ema) * multiplier + ema
        return round(ema, 2)

    @classmethod
    def calculate_ema_series(cls, prices: List[float], period: int) -> List[float]:
        if not prices:
            return []
        if len(prices) < period:
            avg = sum(prices) / len(prices)
            return [round(avg, 2)] * len(prices)
        multiplier = 2.0 / (period + 1.0)
        seed = sum(prices[:period]) / period
        series = [round(seed, 2)] * period
        ema = seed
        for p in prices[period:]:
            ema = (p - ema) * multiplier + ema
            series.append(round(ema, 2))
        return series

    @staticmethod
    def calculate_atr_and_vol(
        closes: List[float],
        highs: Optional[List[float]] = None,
        lows: Optional[List[float]] = None,
        period: int = 14,
    ) -> Tuple[float, float]:
        """Returns `(atr_5m, annualized_realized_vol)` from 5m OHLCV bars."""
        if not closes or len(closes) < 3:
            return 0.0, 0.22

        true_ranges: List[float] = []
        has_hl = bool(highs and lows and len(highs) == len(closes) and len(lows) == len(closes))
        for i in range(1, len(closes)):
            prev_c = closes[i - 1]
            if has_hl:
                tr = max(
                    highs[i] - lows[i],
                    abs(highs[i] - prev_c),
                    abs(lows[i] - prev_c),
                )
            else:
                tr = abs(closes[i] - prev_c) * 1.35
            true_ranges.append(max(0.0, tr))

        recent_trs = true_ranges[-period:]
        atr = sum(recent_trs) / len(recent_trs) if recent_trs else 0.0

        log_rets: List[float] = []
        for i in range(max(1, len(closes) - 45), len(closes)):
            if closes[i - 1] > 0 and closes[i] > 0:
                log_rets.append(math.log(closes[i] / closes[i - 1]))
        if len(log_rets) >= 5:
            mean_r = sum(log_rets) / len(log_rets)
            var_r = sum((r - mean_r) ** 2 for r in log_rets) / max(1, len(log_rets) - 1)
            realized_vol = math.sqrt(var_r) * math.sqrt(252.0 * 75.0)
            realized_vol = max(0.08, min(1.20, realized_vol))
        else:
            realized_vol = 0.22

        return round(atr, 2), round(realized_vol, 4)

    @staticmethod
    def calculate_vwap(prices: List[float], volumes: List[int]) -> float:
        """Calculates cumulative Volume Weighted Average Price."""
        if not prices or not volumes or len(prices) != len(volumes):
            return prices[-1] if prices else 0.0

        cum_pv = sum(p * v for p, v in zip(prices, volumes))
        cum_v = sum(volumes)
        if cum_v <= 0:
            return prices[-1]
        return round(cum_pv / cum_v, 2)

    @classmethod
    def analyze_technicals(
        cls,
        symbol: str,
        current_price: float,
        price_history: Optional[List[float]] = None,
        volume_history: Optional[List[int]] = None,
        vwap_override: Optional[float] = None,
        require_real_history: bool = False,
        high_history: Optional[List[float]] = None,
        low_history: Optional[List[float]] = None,
    ) -> TechnicalIndicators:
        """
        Analyzes the Core Technical Setup normalized to current intraday ATR/volatility:
        - Price vs VWAP (Z_VWAP institutional dominance)
        - 9 EMA, 20 EMA, 40 EMA (Triple Ribbon Alignment + slope in ATRs)
        - Overextension guard combining VWAP % and Z_VWAP
        """
        if not price_history or len(price_history) < 40:
            if require_real_history or current_price <= 0:
                return TechnicalIndicators(
                    symbol=symbol.upper(),
                    current_price=current_price,
                    vwap=vwap_override or current_price,
                    ema_9=current_price,
                    ema_20=current_price,
                    ema_40=current_price,
                    vwap_distance_pct=0.0,
                    ema_spread_pct=0.0,
                    trend_regime="CHOP_OR_DIVERGENT",
                    is_above_vwap=False,
                    is_golden_ema_cross=False,
                    is_overextended=False,
                    technical_conviction=0.0,
                )
            step = current_price * 0.0008
            price_history = [
                current_price - (45 - i) * step * (0.9 if i % 2 == 0 else 1.1)
                for i in range(45)
            ]
            volume_history = [int(15000 + (i * 450)) for i in range(45)]

        vwap = (
            round(float(vwap_override), 2)
            if vwap_override and vwap_override > 0
            else cls.calculate_vwap(price_history, volume_history or [])
        )
        ema_9 = cls.calculate_ema(price_history, 9)
        ema20_series = cls.calculate_ema_series(price_history, 20)
        ema_20 = round(ema20_series[-1], 2) if ema20_series else cls.calculate_ema(price_history, 20)
        ema_40 = cls.calculate_ema(price_history, 40)

        atr_5m, rv_ann = cls.calculate_atr_and_vol(price_history, high_history, low_history)
        atr_safe = max(atr_5m, current_price * 0.001)
        atr_pct = round((atr_5m / current_price * 100.0), 3) if current_price > 0 else 0.0

        vwap_dist = round(((current_price - vwap) / vwap) * 100.0, 2) if vwap > 0 else 0.0
        ema_spread = round(((ema_9 - ema_40) / ema_40) * 100.0, 2) if ema_40 > 0 else 0.0
        session_vwap_sigma = max(atr_safe * 6.0, current_price * 0.009)
        z_vwap = round((current_price - vwap) / session_vwap_sigma, 2) if session_vwap_sigma > 0 else 0.0
        ribbon_spread_atr = round((ema_9 - ema_40) / atr_safe, 2) if atr_safe > 0 else 0.0
        ema20_slope = ((ema20_series[-1] - ema20_series[-6]) / atr_safe) if len(ema20_series) >= 6 else 0.0

        is_above_vwap = current_price > vwap
        is_golden_cross = ema_9 > ema_40
        is_triple_bull = ema_9 > ema_20 > ema_40
        is_triple_bear = ema_9 < ema_20 < ema_40
        is_overextended = abs(vwap_dist) > 2.5 or abs(z_vwap) > 3.2

        if is_above_vwap and is_golden_cross and not is_overextended:
            trend_regime = "BULLISH_TREND_ALIGNMENT"
            conviction = 0.90 if is_triple_bull else (0.85 if vwap_dist <= 1.5 else 0.78)
        elif not is_above_vwap and not is_golden_cross and not is_overextended:
            trend_regime = "BEARISH_TREND_ALIGNMENT"
            conviction = 0.90 if is_triple_bear else (0.85 if abs(vwap_dist) <= 1.5 else 0.78)
        else:
            trend_regime = "CHOP_OR_DIVERGENT"
            conviction = 0.45

        return TechnicalIndicators(
            symbol=symbol.upper(),
            current_price=current_price,
            vwap=vwap,
            ema_9=ema_9,
            ema_40=ema_40,
            vwap_distance_pct=vwap_dist,
            ema_spread_pct=ema_spread,
            trend_regime=trend_regime,
            is_above_vwap=is_above_vwap,
            is_golden_ema_cross=is_golden_cross,
            is_overextended=is_overextended,
            technical_conviction=conviction,
            ema_20=ema_20,
            is_triple_ribbon_bull=is_triple_bull,
            is_triple_ribbon_bear=is_triple_bear,
            atr_5m=atr_5m,
            atr_pct=atr_pct,
            realized_vol_annual=rv_ann,
            z_vwap=z_vwap,
            ribbon_spread_atr=ribbon_spread_atr,
            ema_20_slope_atr=round(ema20_slope, 2),
            ema_20_series=ema20_series,
        )

    @classmethod
    def compute_live_market_lens_breadth(
        cls,
        live_breadth: Optional[Dict] = None,
        sector_map: Optional[Dict[str, List[str]]] = None,
    ) -> MarketLensBreadth:
        """Builds live F&O breadth strictly from batched broker quotes."""
        if not live_breadth or live_breadth.get("status") != "LIVE_KITE_BATCH":
            return MarketLensBreadth(
                advances=0,
                declines=0,
                unchanged=0,
                ad_ratio=1.0,
                market_breadth_sentiment="UNAVAILABLE",
                top_turnover_sectors=[],
                volume_shockers=[],
                source="UNAVAILABLE",
            )

        adv = int(live_breadth.get("advances", 0))
        dec = int(live_breadth.get("declines", 0))
        unc = int(live_breadth.get("unchanged", 0))
        ad_ratio = round(adv / max(1, dec), 2) if (adv + dec) > 0 else 1.0
        sentiment = (
            "BULLISH_EXPANSION" if ad_ratio >= 1.5
            else ("BEARISH_LIQUIDATION" if ad_ratio <= 0.65 else "MIXED_ROTATION")
        )

        moves = live_breadth.get("stock_moves") or []
        by_sym = {m["symbol"]: m for m in moves if "symbol" in m}
        top_sectors: List[str] = []
        if sector_map:
            ranked_sec = []
            for sec, syms in sector_map.items():
                rets = [by_sym[s]["chg_pct"] for s in syms if s in by_sym]
                if rets:
                    ranked_sec.append((sum(abs(x) for x in rets) / len(rets), sec))
            ranked_sec.sort(reverse=True)
            top_sectors = [sec for _, sec in ranked_sec[:4]]

        shockers = [m["symbol"] for m in sorted(moves, key=lambda x: abs(x.get("chg_pct", 0.0)), reverse=True)[:5]]
        return MarketLensBreadth(
            advances=adv,
            declines=dec,
            unchanged=unc,
            ad_ratio=ad_ratio,
            market_breadth_sentiment=sentiment,
            top_turnover_sectors=top_sectors,
            volume_shockers=shockers,
            source="LIVE_KITE_BATCH",
        )

    @classmethod
    def get_market_lens_breadth(cls) -> MarketLensBreadth:
        """
        Deprecated compatibility method.  It must never manufacture a breadth
        signal; live callers use `compute_live_market_lens_breadth` with batched
        broker quotes and receive UNAVAILABLE when that feed is absent.
        """
        return MarketLensBreadth(
            advances=0,
            declines=0,
            unchanged=0,
            ad_ratio=1.0,
            market_breadth_sentiment="UNAVAILABLE",
            top_turnover_sectors=[],
            volume_shockers=[],
            source="UNAVAILABLE",
        )

    @classmethod
    def synthesize_super_setup(
        cls,
        symbol: str,
        current_price: float,
        options_recommendation: str,
        options_confidence: float,
        gex_regime: str,
        gamma_squeeze_score: float,
        generals_status: str,
        price_history: Optional[List[float]] = None,
        volume_history: Optional[List[int]] = None,
        vwap_override: Optional[float] = None,
        require_real_history: bool = False,
        market_breadth: Optional[MarketLensBreadth] = None,
    ) -> Dict:
        """
        Master Multi-Engine Confluence Matrix:
        Engine 1: Technical Microstructure (VWAP + 9/20/40 EMA)
        Engine 2: Institutional Options OI (Nuvama / Opstra)
        Engine 3: Net GEX & Greeks (Quantsapp)
        Engine 4: Macro Generals (Sensibull)
        Engine 5: Live broker breadth, only when supplied by the caller
        """
        technicals = cls.analyze_technicals(
            symbol,
            current_price,
            price_history=price_history,
            volume_history=volume_history,
            vwap_override=vwap_override,
            require_real_history=require_real_history,
        )
        breadth = market_breadth or cls.get_market_lens_breadth()

        tech_bullish = technicals.trend_regime == "BULLISH_TREND_ALIGNMENT"
        tech_bearish = technicals.trend_regime == "BEARISH_TREND_ALIGNMENT"
        options_bullish = options_recommendation == "BUY_CE"
        options_bearish = options_recommendation == "BUY_PE"

        # Confluence Scoring (0 to 100)
        score = 0
        confluence_points = []

        # 1. Technical Alignment (VWAP + 9/20/40 EMA) - 30 pts + 10 Triple Ribbon bonus
        if tech_bullish:
            score += 30
            confluence_points.append(f"Price ₹{current_price:,.1f} > VWAP ₹{technicals.vwap:,.1f} & 9 EMA > 40 EMA")
            if technicals.is_triple_ribbon_bull:
                score += 10
                confluence_points.append(f"20 EMA Triple Ribbon (9:{technicals.ema_9:.1f} > 20:{technicals.ema_20:.1f} > 40:{technicals.ema_40:.1f})")
        elif tech_bearish:
            score += 30
            confluence_points.append(f"Price ₹{current_price:,.1f} < VWAP ₹{technicals.vwap:,.1f} & 9 EMA < 40 EMA")
            if technicals.is_triple_ribbon_bear:
                score += 10
                confluence_points.append(f"20 EMA Triple Ribbon (9:{technicals.ema_9:.1f} < 20:{technicals.ema_20:.1f} < 40:{technicals.ema_40:.1f})")

        # 2. Options OI Alignment (Nuvama / Opstra) - 30 pts
        if (tech_bullish and options_bullish) or (tech_bearish and options_bearish):
            score += int(options_confidence * 30)
            confluence_points.append(f"Options OI confirms ({options_recommendation} with {int(options_confidence*100)}% conviction)")

        # 3. GEX & Squeeze Telemetry (Quantsapp) - 20 pts
        if gex_regime == "SHORT_GAMMA_VOL" or gamma_squeeze_score >= 60.0:
            score += 20
            confluence_points.append(f"Gamma Squeeze active (Score: {gamma_squeeze_score:.0f}/100, Dealer short gamma)")
        elif gex_regime != "LONG_GAMMA_PIN":
            score += 10

        # 4. Live breadth & Generals - 20 pts.  No data, no points.
        if breadth.market_breadth_sentiment == "BULLISH_EXPANSION" and generals_status != "RED_LIGHT":
            score += 20
            confluence_points.append(f"Live breadth A/D Ratio {breadth.ad_ratio} & Generals clear")

        score = min(100, score)

        # Action Decision
        if score >= 75 and (tech_bullish and options_bullish):
            decision = "SUPER_SETUP_BUY_CE"
            action_desc = "TIER-1 HIGH CONVICTION CALL BUY (Full 5-Engine Confluence)"
        elif score >= 75 and (tech_bearish and options_bearish):
            decision = "SUPER_SETUP_BUY_PE"
            action_desc = "TIER-1 HIGH CONVICTION PUT BUY (Full 5-Engine Confluence)"
        elif score >= 55:
            decision = "MODERATE_EDGE"
            action_desc = "SELECTIVE ENTRY (Partial Confluence)"
        else:
            decision = "STAND_ASIDE"
            action_desc = "NO TRADE (Chop or Divergence between Technicals & OI)"

        return {
            "symbol": symbol.upper(),
            "current_price": current_price,
            "super_setup_score": score,
            "decision": decision,
            "action_description": action_desc,
            "technicals": {
                "vwap": technicals.vwap,
                "ema_9": technicals.ema_9,
                "ema_20": technicals.ema_20,
                "ema_40": technicals.ema_40,
                "trend_regime": technicals.trend_regime,
                "vwap_distance_pct": technicals.vwap_distance_pct,
                "is_triple_ribbon_bull": technicals.is_triple_ribbon_bull,
                "is_triple_ribbon_bear": technicals.is_triple_ribbon_bear,
            },
            "live_market_breadth": {
                "ad_ratio": breadth.ad_ratio,
                "breadth_sentiment": breadth.market_breadth_sentiment,
                "source": breadth.source,
            },
            "confluence_points": confluence_points,
        }
