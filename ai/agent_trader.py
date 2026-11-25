"""
AI Autonomous Trading Agent (Nuvama Open Interest & Institutional Flow)
Acts as the Chief Investment Officer:
1. Evaluates live/mock option chain via NuvamaOptionsEngine.
2. Manages open paper positions (Target, Stop-Loss).
3. Opens high-conviction institutional swing trades (15-min / hourly horizon).
4. Records all results in ai/ai_paper_trades.db (100% separate from desk/).
"""
from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

AI_DIR = Path(__file__).resolve().parent
ROOT_DIR = AI_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))

from cockpit import CockpitBuilder, evaluate_session_clock, format_briefing_text
from connector.smart_trading_bridge import SmartTradingBridge
from greeks_gex import GreeksGexEngine, GexAnalysisResult
from live_kite_feed import LiveKiteFeed, LiveMarketSnapshot
from logic_tester import format_logic_report, run_logic_validation
from options_analytics import OptionsAnalyticsEngine, OptionsAnalyticsResult, OptionStrikeData
from paper_ledger import AIPaperLedger
from technical_lens import TechnicalLensEngine, TechnicalIndicators, MarketLensBreadth
from quantitative_alpha import QuantitativeAlphaEngine, QuantitativeAlphaState
from gpu_engine import GPUQuantitativeEngine


class AIAgentTrader:
    def __init__(
        self,
        symbol: str = "NIFTY",
        live_mode: bool = False,
        feed: Optional[LiveKiteFeed] = None,
    ):
        self.symbol = symbol.upper()
        self.live_mode = live_mode
        self.feed = feed if feed is not None else (LiveKiteFeed() if live_mode else None)
        self.ledger = AIPaperLedger()
        self.gpu = GPUQuantitativeEngine()
        base_spec = OptionsAnalyticsEngine.SYMBOL_SPECS.get(
            self.symbol, OptionsAnalyticsEngine.SYMBOL_SPECS["NIFTY"]
        )
        self.spec = dict(base_spec)
        if self.live_mode and self.feed is not None:
            meta = self.feed.get_symbol_meta(self.symbol)
            if meta:
                self.spec["lot_size"] = meta["lot_size"]
                self.spec["strike_step"] = meta["strike_step"]
        self.lot_size = self.spec["lot_size"]

    def _fetch_live_snapshot(self) -> Optional[LiveMarketSnapshot]:
        """Fetches genuine live spot + option chain + 5m bars via LiveKiteFeed."""
        if self.feed is None:
            self.feed = LiveKiteFeed()
        snap = self.feed.fetch_spot_and_option_chain(self.symbol)
        self._last_snapshot = snap
        return snap

    def load_option_chain_from_tape(
        self,
        tape_db_path: Optional[str] = None,
    ) -> Tuple[float, List[OptionStrikeData]]:
        """
        Loads live option chain from Zerodha Kite API in live_mode (strict fail-closed),
        recorded ticks from a tick history db tape if provided,
        or offline synthetic test distribution only when live_mode=False.
        """
        if self.live_mode:
            snap = self._fetch_live_snapshot()
            if snap is None:
                return 0.0, []
            self.lot_size = snap.lot_size
            self.spec["lot_size"] = snap.lot_size
            self.spec["strike_step"] = snap.strike_step
            return snap.spot_price, snap.chain

        if tape_db_path and Path(tape_db_path).exists():
            try:
                conn = sqlite3.connect(tape_db_path)
                cur = conn.cursor()
                # Fetch recent option ticks matching current symbol
                cur.execute(
                    "SELECT payload FROM recorded_ticks WHERE payload LIKE '%option_type%' AND payload LIKE ? ORDER BY id DESC LIMIT 500",
                    (f"%{self.symbol}%",),
                )
                rows = cur.fetchall()
                strikes_map: Dict[float, OptionStrikeData] = {}
                underlying_px = 25420.0 if self.symbol == "NIFTY" else 13125.0

                for r in rows:
                    try:
                        p = json.loads(r[0])
                        strike = float(p.get("strike", 0.0))
                        opt_type = p.get("option_type", "")
                        oi = int(p.get("oi", 0))
                        ltp = float(p.get("last_price", 0.0))
                        u_px = float(p.get("underlying_price") or p.get("underlying_value") or 0.0)
                        if u_px > 0:
                            underlying_px = u_px

                        if strike <= 0:
                            continue

                        if strike not in strikes_map:
                            strikes_map[strike] = OptionStrikeData(strike=strike)

                        if opt_type == "CE":
                            strikes_map[strike].call_oi = max(strikes_map[strike].call_oi, oi)
                            strikes_map[strike].call_ltp = ltp
                        elif opt_type == "PE":
                            strikes_map[strike].put_oi = max(strikes_map[strike].put_oi, oi)
                            strikes_map[strike].put_ltp = ltp
                    except Exception:
                        continue

                if strikes_map:
                    return underlying_px, list(strikes_map.values())
            except Exception:
                pass

        # Offline unit-test fallback (never reached when live_mode=True)
        if self.symbol == "MIDCPNIFTY":
            underlying_px = 13125.0
            synthetic_chain = [
                OptionStrikeData(strike=13000, call_oi=180000, put_oi=3400000, call_ltp=135.0, put_ltp=8.0, put_change_oi=450000),
                OptionStrikeData(strike=13050, call_oi=320000, put_oi=4200000, call_ltp=92.0, put_ltp=15.0, put_change_oi=680000),
                OptionStrikeData(strike=13100, call_oi=890000, put_oi=5100000, call_ltp=54.0, put_ltp=28.0, put_change_oi=550000),  # Put Wall
                OptionStrikeData(strike=13125, call_oi=1400000, put_oi=2100000, call_ltp=36.0, put_ltp=41.0, call_change_oi=-180000), # ATM
                OptionStrikeData(strike=13150, call_oi=3100000, put_oi=1200000, call_ltp=22.0, put_ltp=58.0, call_change_oi=-220000), # Unwinding
                OptionStrikeData(strike=13200, call_oi=5600000, put_oi=650000, call_ltp=10.0, put_ltp=95.0, call_change_oi=250000),   # Call Wall
                OptionStrikeData(strike=13250, call_oi=4100000, put_oi=220000, call_ltp=4.5, put_ltp=140.0, call_change_oi=110000),
            ]
            return underlying_px, synthetic_chain

        if self.symbol == "HDFCBANK":
            underlying_px = 1750.0
            synthetic_chain = [
                OptionStrikeData(strike=1700, call_oi=120000, put_oi=1200000, call_ltp=58.0, put_ltp=2.0, put_change_oi=150000),
                OptionStrikeData(strike=1720, call_oi=280000, put_oi=1800000, call_ltp=39.0, put_ltp=4.5, put_change_oi=320000),
                OptionStrikeData(strike=1740, call_oi=750000, put_oi=2600000, call_ltp=22.0, put_ltp=8.0, put_change_oi=580000),  # Put Wall
                OptionStrikeData(strike=1750, call_oi=1450000, put_oi=1600000, call_ltp=15.0, put_ltp=14.0, call_change_oi=-120000), # ATM
                OptionStrikeData(strike=1760, call_oi=2400000, put_oi=850000, call_ltp=9.5, put_ltp=22.0, call_change_oi=-210000),  # Unwinding
                OptionStrikeData(strike=1780, call_oi=3800000, put_oi=350000, call_ltp=3.5, put_ltp=45.0, call_change_oi=180000),   # Call Wall
                OptionStrikeData(strike=1800, call_oi=2900000, put_oi=120000, call_ltp=1.2, put_ltp=62.0, call_change_oi=80000),
            ]
            return underlying_px, synthetic_chain

        if self.symbol == "RELIANCE":
            underlying_px = 1480.0
            synthetic_chain = [
                OptionStrikeData(strike=1420, call_oi=90000, put_oi=850000, call_ltp=68.0, put_ltp=3.0, put_change_oi=110000),
                OptionStrikeData(strike=1440, call_oi=180000, put_oi=1250000, call_ltp=51.0, put_ltp=6.5, put_change_oi=220000),
                OptionStrikeData(strike=1460, call_oi=420000, put_oi=1950000, call_ltp=34.0, put_ltp=12.0, put_change_oi=450000),  # Put Wall
                OptionStrikeData(strike=1480, call_oi=980000, put_oi=1100000, call_ltp=21.0, put_ltp=20.0, call_change_oi=-95000),  # ATM
                OptionStrikeData(strike=1500, call_oi=2350000, put_oi=520000, call_ltp=11.0, put_ltp=36.0, call_change_oi=140000),  # Call Wall
                OptionStrikeData(strike=1520, call_oi=2100000, put_oi=210000, call_ltp=4.5, put_ltp=58.0, call_change_oi=90000),
            ]
            return underlying_px, synthetic_chain

        if self.symbol == "SBIN":
            underlying_px = 840.0
            synthetic_chain = [
                OptionStrikeData(strike=820, call_oi=450000, put_oi=3200000, call_ltp=25.0, put_ltp=1.5, put_change_oi=350000),
                OptionStrikeData(strike=830, call_oi=820000, put_oi=4100000, call_ltp=16.0, put_ltp=3.5, put_change_oi=620000),  # Put Wall
                OptionStrikeData(strike=840, call_oi=1900000, put_oi=2500000, call_ltp=8.5, put_ltp=8.0, call_change_oi=-310000), # ATM Unwinding
                OptionStrikeData(strike=850, call_oi=4800000, put_oi=1100000, call_ltp=4.0, put_ltp=15.0, call_change_oi=150000), # Call Wall
                OptionStrikeData(strike=860, call_oi=3900000, put_oi=450000, call_ltp=1.8, put_ltp=23.0, call_change_oi=95000),
            ]
            return underlying_px, synthetic_chain

        if self.symbol == "TATAMOTORS":
            underlying_px = 980.0
            synthetic_chain = [
                OptionStrikeData(strike=960, call_oi=210000, put_oi=1800000, call_ltp=32.0, put_ltp=3.0, put_change_oi=220000),
                OptionStrikeData(strike=970, call_oi=450000, put_oi=2400000, call_ltp=23.0, put_ltp=6.0, put_change_oi=450000),  # Put Wall
                OptionStrikeData(strike=980, call_oi=1200000, put_oi=1600000, call_ltp=14.0, put_ltp=13.0, call_change_oi=-180000), # ATM
                OptionStrikeData(strike=990, call_oi=2800000, put_oi=850000, call_ltp=8.5, put_ltp=21.0, call_change_oi=-120000),
                OptionStrikeData(strike=1000, call_oi=4200000, put_oi=420000, call_ltp=4.0, put_ltp=32.0, call_change_oi=220000),  # Call Wall
            ]
            return underlying_px, synthetic_chain

        if self.symbol == "INFY":
            underlying_px = 1880.0
            synthetic_chain = [
                OptionStrikeData(strike=1840, call_oi=180000, put_oi=1400000, call_ltp=55.0, put_ltp=4.0, put_change_oi=110000),
                OptionStrikeData(strike=1860, call_oi=350000, put_oi=2100000, call_ltp=38.0, put_ltp=9.0, put_change_oi=310000),  # Put Wall
                OptionStrikeData(strike=1880, call_oi=1250000, put_oi=1450000, call_ltp=24.0, put_ltp=21.0, call_change_oi=220000), # Short Buildup
                OptionStrikeData(strike=1900, call_oi=3600000, put_oi=720000, call_ltp=13.0, put_ltp=38.0, call_change_oi=420000),  # Call Wall
                OptionStrikeData(strike=1920, call_oi=2900000, put_oi=280000, call_ltp=6.5, put_ltp=56.0, call_change_oi=180000),
            ]
            return underlying_px, synthetic_chain

        # Dynamic multi-sector universe stock dictionary
        STOCK_REGIMES = {
            "HAL": {"spot": 4250.0, "step": 50.0, "bias": "BULLISH_BREAKOUT", "atm_call": 65.0, "atm_put": 42.0},
            "BEL": {"spot": 310.0, "step": 5.0, "bias": "BULLISH_FLOW", "atm_call": 8.5, "atm_put": 6.0},
            "TATASTEEL": {"spot": 155.0, "step": 2.5, "bias": "BULLISH_BREAKOUT", "atm_call": 4.5, "atm_put": 2.2},
            "HINDALCO": {"spot": 680.0, "step": 10.0, "bias": "BULLISH_FLOW", "atm_call": 16.0, "atm_put": 11.0},
            "M&M": {"spot": 2950.0, "step": 20.0, "bias": "BULLISH_BREAKOUT", "atm_call": 55.0, "atm_put": 34.0},
            "MARUTI": {"spot": 12400.0, "step": 100.0, "bias": "NEUTRAL", "atm_call": 180.0, "atm_put": 175.0},
            "SUNPHARMA": {"spot": 1850.0, "step": 20.0, "bias": "BULLISH_FLOW", "atm_call": 28.0, "atm_put": 19.0},
            "CIPLA": {"spot": 1520.0, "step": 10.0, "bias": "NEUTRAL", "atm_call": 22.0, "atm_put": 21.0},
            "TCS": {"spot": 4150.0, "step": 20.0, "bias": "NEUTRAL", "atm_call": 58.0, "atm_put": 55.0},
            "LT": {"spot": 3650.0, "step": 20.0, "bias": "BULLISH_FLOW", "atm_call": 52.0, "atm_put": 38.0},
            "NTPC": {"spot": 380.0, "step": 5.0, "bias": "NEUTRAL", "atm_call": 7.0, "atm_put": 6.8},
            "ONGC": {"spot": 260.0, "step": 2.5, "bias": "NEUTRAL", "atm_call": 4.5, "atm_put": 4.2},
            "ITC": {"spot": 470.0, "step": 5.0, "bias": "NEUTRAL", "atm_call": 7.5, "atm_put": 7.2},
            "BHARTIARTL": {"spot": 1620.0, "step": 20.0, "bias": "BULLISH_FLOW", "atm_call": 24.0, "atm_put": 17.0},
            "ICICIBANK": {"spot": 1280.0, "step": 10.0, "bias": "BULLISH_FLOW", "atm_call": 18.0, "atm_put": 12.0},
        }

        if self.symbol in STOCK_REGIMES:
            info = STOCK_REGIMES[self.symbol]
            u_px = info["spot"]
            step = info["step"]
            atm_k = round(u_px / step) * step
            is_bull = "BULLISH" in info["bias"]

            synthetic_chain = []
            for offset in range(-3, 4):
                strike = atm_k + offset * step
                dist = offset * step
                if offset == -2:
                    p_oi, c_oi = 2500000, 300000
                elif offset == 2:
                    p_oi, c_oi = 400000, 3200000
                elif offset == 0:
                    p_oi = 1800000 if is_bull else 1200000
                    c_oi = 850000 if is_bull else 1400000
                elif offset < 0:
                    p_oi, c_oi = 1500000, 500000
                else:
                    p_oi, c_oi = 600000, 2000000

                c_ltp = max(0.5, round(info["atm_call"] - dist * 0.45, 2))
                p_ltp = max(0.5, round(info["atm_put"] + dist * 0.45, 2))
                c_chg = -220000 if (is_bull and offset >= 0) else (60000 if is_bull else 150000)
                p_chg = 380000 if (is_bull and offset <= 0) else (50000 if is_bull else 40000)

                synthetic_chain.append(
                    OptionStrikeData(
                        strike=strike,
                        call_oi=c_oi,
                        put_oi=p_oi,
                        call_ltp=c_ltp,
                        put_ltp=p_ltp,
                        call_change_oi=c_chg,
                        put_change_oi=p_chg,
                    )
                )
            return u_px, synthetic_chain

        underlying_px = 25450.0
        synthetic_chain = [
            OptionStrikeData(strike=25200, call_oi=250000, put_oi=3200000, call_ltp=285.0, put_ltp=15.0, put_change_oi=450000),
            OptionStrikeData(strike=25300, call_oi=600000, put_oi=4800000, call_ltp=205.0, put_ltp=28.0, put_change_oi=820000),
            OptionStrikeData(strike=25400, call_oi=1450000, put_oi=5200000, call_ltp=138.0, put_ltp=52.0, put_change_oi=650000),  # Put Wall
            OptionStrikeData(strike=25450, call_oi=2100000, put_oi=2800000, call_ltp=105.0, put_ltp=72.0, call_change_oi=-120000), # ATM
            OptionStrikeData(strike=25500, call_oi=4900000, put_oi=1800000, call_ltp=76.0, put_ltp=98.0, call_change_oi=-350000),  # Unwinding
            OptionStrikeData(strike=25600, call_oi=6800000, put_oi=950000, call_ltp=36.0, put_ltp=155.0, call_change_oi=210000),   # Call Wall
            OptionStrikeData(strike=25700, call_oi=5100000, put_oi=400000, call_ltp=16.0, put_ltp=230.0, call_change_oi=150000),
        ]
        return underlying_px, synthetic_chain

    def inspect_market(
        self,
        tape_db: Optional[str] = None,
    ) -> OptionsAnalyticsResult:
        underlying_px, chain = self.load_option_chain_from_tape(tape_db)
        return OptionsAnalyticsEngine.analyze(underlying_px, chain, symbol=self.symbol)

    def print_dashboard(self, result: OptionsAnalyticsResult) -> None:
        print("\n" + "=" * 65)
        print(f"     NUVAMA WEALTH INSTITUTIONAL REPORT ({self.symbol})")
        print("=" * 65)
        print(f" {self.symbol} Spot Price   : ₹{result.underlying_price:,.2f}")
        print(f" Expiry Max Pain      : ₹{result.max_pain_strike:,.0f}  (Pinning Target)")
        print(f" Put-Call Ratio (PCR) : {result.pcr:.2f}")
        print(f" Major Put Wall (SUP) : ₹{result.put_wall_strike:,.0f}  ({result.put_wall_oi:,} contracts)")
        print(f" Major Call Wall (RES): ₹{result.call_wall_strike:,.0f}  ({result.call_wall_oi:,} contracts)")
        print(f" ATM Straddle (Opstra): ₹{result.atm_straddle_premium:,.2f}  (Breakeven Cone: ₹{result.expected_breakeven_low:,.0f} – ₹{result.expected_breakeven_high:,.0f})")
        print(f" Market Sentiment     : {result.sentiment}")
        print(f" AI Recommendation    : {result.trade_recommendation}  (Confidence: {result.confidence*100:.0f}%)")
        print("-" * 65)
        print(f" Thesis: {result.analysis_thesis}")
        print("-" * 65)
        print(" Strike-Wise Institutional Distribution:")
        print(f" {'Strike':^8} | {'Call OI':^12} | {'Call LTP':^9} | {'Put LTP':^9} | {'Put OI':^12}")
        print("-" * 65)
        for s in result.key_strikes:
            print(
                f" {s['strike']:>8.0f} | {s['call_oi']:>12,} | {s['call_ltp']:>9.2f} | "
                f"{s['put_ltp']:>9.2f} | {s['put_oi']:>12,}"
            )
        print("=" * 65 + "\n")

    def inspect_live_setup(self) -> Optional[Dict]:
        """
        Evaluates genuine real-time Kite market data (spot, VWAP, 5m 9/20/40 EMAs,
        live NFO option chain, GEX) without synthetic fallback. Returns None if unavailable.
        """
        if self.feed is None:
            self.feed = LiveKiteFeed()
        snap = self.feed.fetch_spot_and_option_chain(self.symbol)
        if snap is None or not snap.chain or snap.spot_price <= 0:
            return None

        self.lot_size = snap.lot_size
        self.spec["lot_size"] = snap.lot_size
        self.spec["strike_step"] = snap.strike_step

        analysis = OptionsAnalyticsEngine.analyze(snap.spot_price, snap.chain, symbol=self.symbol)
        tech = TechnicalLensEngine.analyze_technicals(
            symbol=self.symbol,
            current_price=snap.spot_price,
            price_history=snap.price_history,
            volume_history=snap.volume_history,
            vwap_override=snap.vwap_live,
            require_real_history=True,
        )
        gex_res = GreeksGexEngine.evaluate_frontier_landscape(
            symbol=self.symbol,
            underlying_price=snap.spot_price,
            chain=snap.chain,
        )

        # Synthesize live directional edge from real technicals + intraday price + OI walls/PCR
        signal = analysis.trade_recommendation
        confidence = analysis.confidence
        thesis = analysis.analysis_thesis

        if signal not in ("BUY_CE", "BUY_PE") or confidence < 0.75:
            day_ret_pct = ((snap.spot_price - snap.prev_close) / snap.prev_close * 100.0) if snap.prev_close > 0 else 0.0
            if tech.trend_regime == "BULLISH_TREND_ALIGNMENT" and day_ret_pct > 0.15 and analysis.pcr >= 0.65:
                signal = "BUY_CE"
                confidence = 0.86 if tech.is_triple_ribbon_bull else 0.78
                ribbon = f"9>{tech.ema_20:.1f}>40" if tech.is_triple_ribbon_bull else f"9>{tech.ema_40:.1f}"
                thesis = (
                    f"[LIVE KITE] {self.symbol} @ ₹{snap.spot_price:,.2f} (+{day_ret_pct:.2f}% vs prev close, "
                    f"VWAP ₹{tech.vwap:,.2f}, EMA {ribbon}), PCR={analysis.pcr:.2f}, Walls {analysis.put_wall_strike:.0f}-{analysis.call_wall_strike:.0f}"
                )
            elif tech.trend_regime == "BEARISH_TREND_ALIGNMENT" and day_ret_pct < -0.15 and analysis.pcr <= 1.35:
                signal = "BUY_PE"
                confidence = 0.86 if tech.is_triple_ribbon_bear else 0.78
                ribbon = f"9<{tech.ema_20:.1f}<40" if tech.is_triple_ribbon_bear else f"9<{tech.ema_40:.1f}"
                thesis = (
                    f"[LIVE KITE] {self.symbol} @ ₹{snap.spot_price:,.2f} ({day_ret_pct:.2f}% vs prev close, "
                    f"VWAP ₹{tech.vwap:,.2f}, EMA {ribbon}), PCR={analysis.pcr:.2f}, Walls {analysis.put_wall_strike:.0f}-{analysis.call_wall_strike:.0f}"
                )
        else:
            # Confirm Nuvama OI signal does not violently fight live 5m trend
            if signal == "BUY_CE" and tech.trend_regime == "BEARISH_TREND_ALIGNMENT":
                signal = "STAND_ASIDE"
            elif signal == "BUY_PE" and tech.trend_regime == "BULLISH_TREND_ALIGNMENT":
                signal = "STAND_ASIDE"

        score = 0.0
        if signal in ("BUY_CE", "BUY_PE"):
            score = confidence * 80.0 + (10.0 if (tech.is_triple_ribbon_bull or tech.is_triple_ribbon_bear) else 0.0)
            if gex_res.gex_regime == "SHORT_GAMMA_VOL":
                score += 10.0

        return {
            "symbol": self.symbol,
            "snap": snap,
            "analysis": analysis,
            "tech": tech,
            "gex": gex_res,
            "signal": signal,
            "confidence": confidence,
            "score": round(score, 2),
            "thesis": thesis,
        }

    def _compute_generals_score(self) -> float:
        """Evaluates weighted directional alignment across the 6 Generals Composite."""
        total_w = sum(w for _, w in GENERALS_COMPOSITE)
        w_score = 0.0
        for g_sym, wt in GENERALS_COMPOSITE:
            if self.live_mode:
                sub = AIAgentTrader(g_sym, live_mode=True, feed=self.feed).inspect_live_setup()
                sig = sub["signal"] if sub else "STAND_ASIDE"
            else:
                sig = AIAgentTrader(g_sym).inspect_market().trade_recommendation
            mult = 1.0 if "BUY_CE" in sig else (-1.0 if "BUY_PE" in sig else 0.0)
            w_score += mult * wt
        return w_score / total_w if total_w > 0 else 0.0

    def run_cycle(
        self,
        tape_db: Optional[str] = None,
        generals_norm: Optional[float] = None,
        precomputed_live: Optional[Dict] = None,
    ) -> Dict:
        """
        Autonomous Agent Decision Cycle:
        1. Manages open trades (using live NFO quotes in live_mode; strictly fail-closed).
        2. If flat, evaluates analytics + technicals + liquidity and opens paper trade.
        """
        all_open_trades = self.ledger.get_open_trades()
        symbol_open_trades = [t for t in all_open_trades if t.get("underlying") == self.symbol]

        # 1. Manage active open trades for this underlying symbol
        if symbol_open_trades:
            time_stopped: List[str] = []
            live_quotes: Dict[str, Dict] = {}
            live_tech: Optional[TechnicalIndicators] = None

            if self.live_mode:
                if self.feed is None:
                    self.feed = LiveKiteFeed()
                live_quotes = self.feed.fetch_contract_quotes([t["symbol"] for t in symbol_open_trades])
                meta = self.feed.get_symbol_meta(self.symbol)
                if meta and meta.get("spot_token"):
                    closes, vols = self.feed._fetch_spot_5m_bars(meta["spot_symbol"], meta["spot_token"])
                    if closes:
                        live_tech = TechnicalLensEngine.analyze_technicals(
                            self.symbol, closes[-1], price_history=closes, volume_history=vols, require_real_history=True
                        )
            else:
                underlying_px, chain = self.load_option_chain_from_tape(tape_db)

            for t in symbol_open_trades:
                tid = t["trade_id"]
                sym = t["symbol"]
                entry_p = t["entry_price"]
                sl = t["stop_loss"]
                direction = t["direction"]

                curr_price = None
                if self.live_mode:
                    q = live_quotes.get(sym)
                    if q and q.get("mark", 0.0) > 0.0:
                        curr_price = float(q["mark"])
                    else:
                        print(f"⚠️ [LIVE FAIL-CLOSED] Missing live quote for #{tid} ({sym}). Holding position safely without synthetic mark.")
                        continue
                else:
                    curr_price = entry_p
                    matched_strike = None
                    for part in sym.split("_"):
                        digits = "".join(c for c in part if c.isdigit())
                        if digits:
                            matched_strike = float(digits)
                            break
                    if matched_strike:
                        chain_match = next((s for s in chain if s.strike == matched_strike), None)
                        if chain_match:
                            ltp = chain_match.call_ltp if "CE" in sym else chain_match.put_ltp
                            if ltp > 0:
                                curr_price = ltp

                # Dynamic Trailing Stop-Loss & 45-Minute Momentum Time-Stop
                gain_ratio = (curr_price - entry_p) / entry_p if entry_p > 0 else 0.0

                is_stagnant = False
                try:
                    entry_dt = datetime.datetime.fromisoformat(t["entry_timestamp"])
                    if entry_dt.tzinfo is None:
                        entry_dt = entry_dt.replace(tzinfo=datetime.timezone.utc)
                    now_dt = datetime.datetime.now(datetime.timezone.utc)
                    elapsed_min = (now_dt - entry_dt).total_seconds() / 60.0
                    if elapsed_min >= 45.0 and gain_ratio < 0.15:
                        is_stagnant = True
                except Exception:
                    pass

                if is_stagnant:
                    res = self.ledger.close_trade(tid, curr_price, "MOMENTUM_TIME_STOP_45MIN")
                    time_stopped.append(self.symbol)
                    print(f"⏱️ [AI Agent] 45-Minute Momentum Time-Stop for Trade #{tid} ({sym}) at ₹{curr_price:.2f}! Net PnL: ₹{res.get('net_pnl', 0.0):,.2f}")
                    continue

                if gain_ratio >= 0.35:
                    rally_trail_sl = max(round(entry_p * 1.02, 2), round(curr_price * 0.82, 2))
                    if rally_trail_sl > sl:
                        self.ledger.update_stop_loss(tid, rally_trail_sl)
                        sl = rally_trail_sl
                        print(f"🚀 [AI Agent RALLY RUNNER] Trade #{tid} ({sym}) ripping (+{gain_ratio*100:.1f}%)! Trailing SL raised to ₹{sl:.2f}")
                elif gain_ratio >= 0.25:
                    new_sl = round(entry_p * 0.95, 2)
                    if new_sl > sl:
                        self.ledger.update_stop_loss(tid, new_sl)
                        sl = new_sl
                        print(f"💰 [AI Agent] +25% Tailwind for Trade #{tid} ({sym})! Tightened SL to ₹{new_sl:.2f}")
                elif live_tech and live_tech.ema_20 > 0 and gain_ratio >= 0.15:
                    ema20_broken = (
                        (direction == "BUY_CE" and live_tech.current_price < live_tech.ema_20)
                        or (direction == "BUY_PE" and live_tech.current_price > live_tech.ema_20)
                    )
                    if ema20_broken:
                        lock_sl = round(curr_price * 0.95, 2)
                        if lock_sl > sl and lock_sl > entry_p:
                            self.ledger.update_stop_loss(tid, lock_sl)
                            sl = lock_sl
                            print(f"🛡️ [20-EMA DEFENSE] Spot crossed 20-EMA (₹{live_tech.ema_20:.2f}) on #{tid} ({sym}) — SL locked at ₹{sl:.2f}")

                if curr_price <= sl:
                    exit_reason = "TRAILING_STOP_LOCK" if sl > entry_p else "STOP_LOSS_WALL_BREACHED"
                    res = self.ledger.close_trade(tid, curr_price, exit_reason)
                    npnl = float(res.get("net_pnl", 0.0) or 0.0)
                    pnl_s = f"-₹{abs(npnl):,.2f}" if npnl < 0 else f"₹{npnl:,.2f}"
                    print(f"🛑 [AI Agent] {exit_reason} Hit for Trade #{tid} ({sym}) at ₹{curr_price:.2f}! Net PnL: {pnl_s}")
                else:
                    pnl_unrealized = (curr_price - entry_p) * t["quantity"]
                    pnl_pct_disp = gain_ratio * 100
                    u_str = f"-₹{abs(pnl_unrealized):,.2f}" if pnl_unrealized < 0 else f"+₹{pnl_unrealized:,.2f}"
                    pct_str = f"{pnl_pct_disp:+.1f}%"
                    print(f"📊 [AI Agent] Active #{tid} ({sym}) @ ₹{curr_price:.2f} | Unrealized: {u_str} ({pct_str}) | SL: ₹{sl:.2f}")

            return {"action": "MANAGED_ACTIVE_TRADES", "open_count": len(symbol_open_trades), "time_stopped": time_stopped}

        # 2. Portfolio Risk Gate: Maximum concurrent positions across all symbols
        if len(all_open_trades) >= 3:
            return {"action": "STAND_ASIDE", "reason": "MAX_CONCURRENT_POSITIONS_REACHED"}

        # 3. Evaluate entry signal
        snap: Optional[LiveMarketSnapshot] = None
        tech_info: Optional[TechnicalIndicators] = None
        if self.live_mode:
            live_eval = precomputed_live or self.inspect_live_setup()
            if live_eval is None:
                print(f"⚠️ [LIVE FAIL-CLOSED] Standing aside on {self.symbol}: live Zerodha quote/chain unavailable.")
                return {"action": "STAND_ASIDE", "reason": "NO_LIVE_MARKET_FEED"}
            snap = live_eval["snap"]
            underlying_px = snap.spot_price
            chain = snap.chain
            analysis = live_eval["analysis"]
            tech_info = live_eval["tech"]
            gex_res = live_eval["gex"]
            signal = live_eval["signal"]
            confidence = live_eval["confidence"]
            thesis = live_eval["thesis"]
        else:
            underlying_px, chain = self.load_option_chain_from_tape(tape_db)
            if not chain or underlying_px <= 0:
                return {"action": "STAND_ASIDE", "reason": "NO_MARKET_DATA"}
            analysis = OptionsAnalyticsEngine.analyze(underlying_px, chain, symbol=self.symbol)
            gex_res = GreeksGexEngine.evaluate_frontier_landscape(self.symbol, underlying_px, chain)
            signal = analysis.trade_recommendation
            confidence = analysis.confidence
            thesis = analysis.analysis_thesis

        if signal not in ("BUY_CE", "BUY_PE") or confidence < 0.75:
            return {"action": "STAND_ASIDE", "thesis": thesis}

        # Macro Gate: The 6 Generals Composite (40.44% of NIFTY)
        try:
            norm_w = generals_norm if generals_norm is not None else self._compute_generals_score()
            if signal == "BUY_CE" and norm_w <= -0.25:
                print(f"🛑 [AI Agent VETO] Blocked BUY_CE on {self.symbol}! The 6 Generals are liquidating ({norm_w*100:.1f}% score).")
                return {"action": "VETOED_BY_GENERALS", "reason": "6 Generals bearish liquidation"}
            if signal == "BUY_PE" and norm_w >= 0.25:
                print(f"🛑 [AI Agent VETO] Blocked BUY_PE on {self.symbol}! The 6 Generals are bullish (+{norm_w*100:.1f}% score).")
                return {"action": "VETOED_BY_GENERALS", "reason": "6 Generals bullish breakout"}
        except Exception:
            pass

        # GEX Volatility Regime Gate: Prevent Theta bleed in pinned chop
        if gex_res and gex_res.gex_regime == "LONG_GAMMA_PIN" and gex_res.gamma_squeeze_score < 60.0:
            print(f"⚖️ [AI Agent Fiduciary] STAND ASIDE on {self.symbol}: LONG_GAMMA_PIN ({gex_res.net_gex_crores:+.1f} Cr).")
            return {"action": "STAND_ASIDE", "reason": "LONG_GAMMA_PIN_THETA_DECAY", "gex": gex_res.net_gex_crores}

        # Institutional Wall Proximity Gate
        strike_step = self.spec["strike_step"]
        is_call = signal == "BUY_CE"
        if is_call and analysis.call_wall_strike > 0:
            dist_to_call_wall = analysis.call_wall_strike - underlying_px
            if 0 < dist_to_call_wall < (self.spec["wall_proximity"] * 0.4):
                print(f"🛑 [AI Agent VETO] Blocked BUY_CE on {self.symbol}: Distance to Call Wall ({analysis.call_wall_strike:.0f}) is only {dist_to_call_wall:.1f} pts!")
                return {"action": "VETOED_BY_WALL_PROXIMITY", "reason": f"Only {dist_to_call_wall:.1f} pts to Call Wall"}
        elif not is_call and analysis.put_wall_strike > 0:
            dist_to_put_wall = underlying_px - analysis.put_wall_strike
            if 0 < dist_to_put_wall < (self.spec["wall_proximity"] * 0.4):
                print(f"🛑 [AI Agent VETO] Blocked BUY_PE on {self.symbol}: Distance to Put Wall ({analysis.put_wall_strike:.0f}) is only {dist_to_put_wall:.1f} pts!")
                return {"action": "VETOED_BY_WALL_PROXIMITY", "reason": f"Only {dist_to_put_wall:.1f} pts to Put Wall"}

        # Demat Budget calculation (₹50,000 Total Capital)
        DEMAT_BUDGET_TOTAL = 50000.0
        current_deployed = sum(t["entry_price"] * t["quantity"] for t in all_open_trades)
        available_cash = max(0.0, DEMAT_BUDGET_TOTAL - current_deployed)

        opt_tag = "CE" if is_call else "PE"
        contract_symbol = ""
        entry_px = 0.0

        if self.live_mode and snap is not None:
            # Candidate strikes: ATM first, then 1 step OTM only if ATM lot exceeds cash budget
            atm_obj = min(chain, key=lambda s: abs(s.strike - underlying_px))
            otm_target = atm_obj.strike + (strike_step if is_call else -strike_step)
            otm_obj = next((s for s in chain if s.strike == otm_target), None)
            candidates = [atm_obj] + ([otm_obj] if otm_obj else [])

            for cand in candidates:
                tsym = snap.contract_map.get((cand.strike, opt_tag))
                if not tsym:
                    continue
                ltp = cand.call_ltp if is_call else cand.put_ltp
                bid = cand.call_bid if is_call else cand.put_bid
                ask = cand.call_ask if is_call else cand.put_ask
                oi = cand.call_oi if is_call else cand.put_oi
                if bid > 0 and ask > 0:
                    mid = (bid + ask) / 2.0
                    spread_pct = (ask - bid) / mid
                    if spread_pct > 0.025:
                        print(f"🛑 [AI Agent VETO] {tsym}: Wide spread {spread_pct*100:.1f}% > 2.5% (Bid ₹{bid:.2f} / Ask ₹{ask:.2f}).")
                        continue
                    exec_px = ask
                else:
                    exec_px = ltp

                if exec_px <= 0 or oi <= 0:
                    continue
                if exec_px * self.lot_size <= available_cash:
                    contract_symbol = tsym
                    entry_px = exec_px
                    break

            if not contract_symbol or entry_px <= 0.0:
                print(f"⚖️ [AI Agent Fiduciary] STAND ASIDE on {self.symbol}: No liquid contract fits ₹{available_cash:,.2f} cash budget.")
                return {"action": "STAND_ASIDE", "reason": "NO_LIQUID_AFFORDABLE_CONTRACT", "available_cash": available_cash}
        else:
            strike_target = int(round(underlying_px / strike_step) * strike_step)
            contract_symbol = f"{self.symbol}_CURRENT_{strike_target}{opt_tag}"
            atm_match = next((s for s in chain if s.strike == strike_target), None)
            if atm_match:
                entry_px = atm_match.call_ltp if is_call else atm_match.put_ltp
                bid = getattr(atm_match, "call_bid", 0.0) if is_call else getattr(atm_match, "put_bid", 0.0)
                ask = getattr(atm_match, "call_ask", 0.0) if is_call else getattr(atm_match, "put_ask", 0.0)
                if bid > 0 and ask > 0:
                    spread_pct = (ask - bid) / ((bid + ask) / 2.0)
                    if spread_pct > 0.015:
                        return {"action": "VETOED_BY_SPREAD", "reason": f"Spread {spread_pct*100:.1f}% > 1.5%"}
            if entry_px <= 0.0:
                return {"action": "STAND_ASIDE", "reason": "ZERO_PREMIUM"}

        single_lot_cost = entry_px * self.lot_size
        # Risk-budgeted SL: keep single-trade stop risk <= ₹3,000 so one stop never triggers the -₹3,500 daily breaker
        sl_risk_pct = min(0.18, max(0.08, 3000.0 / single_lot_cost)) if single_lot_cost > 0 else 0.18
        stop_px = round(entry_px * (1.0 - sl_risk_pct), 2)
        target_px = round(entry_px * 1.35, 2)

        target_allocation = min(available_cash, 28000.0 if confidence >= 0.85 else 22000.0)
        raw_lots = int(target_allocation / single_lot_cost) if single_lot_cost > 0 else 1
        calc_lots = 1 if (raw_lots < 1 and available_cash >= single_lot_cost) else max(1, raw_lots)
        ticket_value = entry_px * self.lot_size * calc_lots
        if ticket_value > available_cash:
            calc_lots = int(available_cash / single_lot_cost)
            ticket_value = entry_px * self.lot_size * calc_lots

        if calc_lots < 1 or ticket_value > available_cash:
            print(f"⚖️ [AI Agent Fiduciary] STAND ASIDE on {self.symbol}: 1 lot needs ₹{single_lot_cost:,.2f}, cash left ₹{available_cash:,.2f}.")
            return {"action": "STAND_ASIDE", "reason": "INSUFFICIENT_DEMAT_BUDGET", "available_cash": available_cash}

        # GPU Monte Carlo 10,000-Path Probability of Touch (PoT) Evaluation
        target_distance_pts = max(1.0, abs(analysis.structural_target_index - underlying_px))
        stop_distance_pts = max(1.0, abs(underlying_px - analysis.structural_stop_index))
        mc_sim = self.gpu.simulate_monte_carlo_po_t(
            spot=underlying_px,
            annualized_vol=0.28,
            target_pts=target_distance_pts,
            stop_pts=stop_distance_pts,
            drift=0.35 if is_call else -0.35,
        )

        trade_id = self.ledger.open_trade(
            underlying=self.symbol,
            symbol=contract_symbol,
            direction=signal,
            entry_price=entry_px,
            stop_loss=stop_px,
            target=target_px,
            lots=calc_lots,
            lot_size=self.lot_size,
            thesis=thesis,
            source_tag="LIVE_KITE" if self.live_mode else "SHADOW",
        )

        vwap_disp = tech_info.vwap if tech_info else underlying_px
        ema_disp = f"9:{tech_info.ema_9:.1f} / 20:{tech_info.ema_20:.1f} / 40:{tech_info.ema_40:.1f}" if tech_info else "9 > 40 EMA"
        print("\n" + "═" * 78)
        print(f" 🎯 {'LIVE KITE' if self.live_mode else 'PAPER'} TRADE EXECUTION: {signal} #{trade_id} ({contract_symbol})")
        print("═" * 78)
        print(f" 👤 TECHNICALS & FLOW     : Spot ₹{underlying_px:,.2f} | VWAP ₹{vwap_disp:,.2f} | EMAs ({ema_disp})")
        print(f" 🤖 OPTIONS TELEMETRY     : PCR {analysis.pcr:.2f} | Max Pain ₹{analysis.max_pain_strike:.0f} | Call Wall ₹{analysis.call_wall_strike:.0f}")
        print(f" ⚡ GPU MONTE CARLO (RTX) : 10,000 Paths in {mc_sim['gpu_execution_ms']:.2f}ms | PoT Target: {mc_sim['p_win_target']*100:.1f}% ({mc_sim['device']})")
        print(f" 📦 DEMAT BUDGET (₹50k)   : {calc_lots} Lot(s) ({calc_lots * self.lot_size} Units) | Ticket: ₹{ticket_value:,.2f} | Buffer: ₹{available_cash - ticket_value:,.2f}")
        print(f" 🛡️ ACTIVE DEFENSE PLAN   : Entry ₹{entry_px:.2f} | SL ₹{stop_px:.2f} (-{sl_risk_pct*100:.1f}%) | Target ₹{target_px:.2f} (+35%)")
        print("═" * 78 + "\n")

        return {
            "action": "OPENED_TRADE",
            "trade_id": trade_id,
            "symbol": contract_symbol,
            "direction": signal,
            "entry_price": entry_px,
            "lots": calc_lots,
            "ticket_value": ticket_value,
        }


def square_off_all_positions(
    reason: str = "HARD_EOD_SQUAREOFF_0315PM",
    live_mode: bool = True,
    feed: Optional[LiveKiteFeed] = None,
):
    """
    Executes mandatory 03:15 PM EOD square-off across all open positions.
    Uses genuine live Zerodha NFO quotes for LIVE_KITE trades; voids orphaned synthetic rows cleanly.
    """
    ledger = AIPaperLedger()
    ledger.void_synthetic_open_trades()
    open_trades = ledger.get_open_trades()
    if not open_trades:
        print("\n⚖️ [AI Agent CIO] No open live positions to square off. Portfolio is 100% in cash.")
        return

    print("\n" + "=" * 80)
    print("      MANDATORY 03:15 PM EOD SQUARE-OFF (FIDUCIARY ZERO-OVERNIGHT RULE)")
    print("=" * 80)
    print(f" Squaring off {len(open_trades)} active positions at genuine live market mark...")
    print("-" * 80)

    active_feed = feed or LiveKiteFeed()
    live_quotes = active_feed.fetch_contract_quotes([t["symbol"] for t in open_trades]) if live_mode else {}

    total_realized_net = 0.0
    total_friction = 0.0

    for t in open_trades:
        tid = t["trade_id"]
        sym = t["symbol"]
        entry_p = t["entry_price"]

        q = live_quotes.get(sym)
        if q and q.get("mark", 0.0) > 0:
            mark = float(q["mark"])
        else:
            print(f" ⚠️ [FAIL-CLOSED] Could not fetch live quote for #{tid} ({sym}); skipping synthetic fallback close.")
            continue

        res = ledger.close_trade(tid, mark, reason)
        total_friction += res.get("friction", 0.0)
        total_realized_net += res.get("net_pnl", 0.0)

        sign = "+" if res.get("net_pnl", 0.0) >= 0 else ""
        print(f" 🔒 SQUARED OFF #{tid} ({sym}): Entry ₹{entry_p:.2f} ──► Live Exit ₹{mark:.2f} | Net: {sign}₹{res.get('net_pnl', 0.0):,.2f} (Friction: ₹{res.get('friction', 0.0):.2f})")

    print("-" * 80)
    net_sign = "+" if total_realized_net >= 0 else ""
    print(f" 🏁 EOD Live Net Realized PnL: {net_sign}₹{total_realized_net:,.2f} | Statutory Friction: ₹{total_friction:,.2f}")
    print("=" * 80 + "\n")


GENERALS_COMPOSITE = [
    ("HDFCBANK", 0.0995),
    ("ICICIBANK", 0.0939),
    ("RELIANCE", 0.0730),
    ("BHARTIARTL", 0.0563),
    ("LT", 0.0425),
    ("INFY", 0.0392),
]


def inspect_the_generals():
    """
    Evaluates institutional Open Interest on 'The 6 Generals' Composite:
    HDFCBANK (9.95%), ICICIBANK (9.39%), RELIANCE (7.30%), BHARTIARTL (5.63%),
    LT (4.25%), INFY (3.92%) = 40.44% of NIFTY 50.
    Calculates index-weighted directional institutional conviction.
    """
    print("\n" + "=" * 76)
    print("      THE 6 GENERALS REPORT: HEAVYWEIGHT INSTITUTIONAL COMPOSITE (40.44% NIFTY)")
    print("=" * 76)

    total_weight = sum(w for _, w in GENERALS_COMPOSITE)
    weighted_score = 0.0
    generals_data = []

    for sym, wt in GENERALS_COMPOSITE:
        res = AIAgentTrader(sym).inspect_market()
        dir_mult = 1.0 if "BUY_CE" in res.trade_recommendation else (-1.0 if "BUY_PE" in res.trade_recommendation else 0.0)
        weighted_score += dir_mult * wt
        generals_data.append((sym, wt, res))
        icon = "🟢" if dir_mult > 0 else ("🔴" if dir_mult < 0 else "⚪")
        print(f" {icon} {sym:<10} ({wt*100:>4.2f}% Wt): ₹{res.underlying_price:>7.1f} | Pain: ₹{res.max_pain_strike:<5.0f} | Wall: ₹{res.call_wall_strike:<5.0f} | {res.trade_recommendation}")

    norm_score = weighted_score / total_weight  # Normalized [-1.0, +1.0]
    nifty_res = AIAgentTrader("NIFTY").inspect_market()

    print("-" * 76)
    print(f" Benchmark NIFTY 50: ₹{nifty_res.underlying_price:.1f} -> Recommendation: {nifty_res.trade_recommendation}")

    if norm_score >= 0.25:
        status = f"🟢 GREEN LIGHT: Confluent institutional accumulation (+{norm_score*100:.1f}% weighted score)"
    elif norm_score <= -0.25:
        status = f"🔴 RED LIGHT: Confluent institutional liquidation/writing ({norm_score*100:.1f}% weighted score)"
    else:
        status = f"🟡 CAUTION: Divergent/neutral flow ({norm_score*100:+.1f}% weighted score) - Selective stock trades only"

    print(f" 6 Generals Composite Macro Alignment: {status}")
    print("=" * 76 + "\n")
    return status


def scan_sector_heatmap():
    """
    Ranks sectors by institutional money flow, Net OI velocity, and price action.
    Identifies the single highest-momentum sector and its top breakout stock.
    """
    sectors = OptionsAnalyticsEngine.SECTOR_MAP
    sector_scores = []

    for sec_name, symbols in sectors.items():
        conf_sum = 0.0
        bull_count = 0
        sym_results = []
        for sym in symbols:
            agent = AIAgentTrader(sym)
            res = agent.inspect_market()
            conf_sum += res.confidence
            if "BUY" in res.trade_recommendation:
                bull_count += 1
            sym_results.append((sym, agent, res))

        avg_conf = conf_sum / len(symbols) if symbols else 0.0
        sector_scores.append((sec_name, avg_conf, bull_count, len(symbols), sym_results))

    # Sort sectors by bullish conviction descending
    sector_scores.sort(key=lambda x: (x[1], x[2]), reverse=True)

    print("\n" + "=" * 82)
    print("      NUVAMA / OPSTRA INSTITUTIONAL SECTOR ROTATION HEATMAP")
    print("=" * 82)
    print(f" {'Rank':<4} | {'Sector':<18} | {'Leader Status':<20} | {'Conviction':<12} | {'Top Stock Alpha'}")
    print("-" * 82)

    for i, (sec, avg_conf, bull_cnt, total, syms) in enumerate(sector_scores, 1):
        top_sym, top_ag, top_res = max(syms, key=lambda x: x[2].confidence)
        icon = "🔥 TOP LEADER" if i <= 2 else ("🚀 OUTPERFORM" if bull_cnt > 0 else "⚖️ NEUTRAL/CHOP")
        conf_pct = f"{int(avg_conf * 100)}%"
        print(f"  {i:<3} | {sec:<18} | {icon:<20} | {conf_pct:<12} | {top_sym} ({top_res.trade_recommendation})")

    print("-" * 82)
    best_sec, best_conf, _, _, best_syms = sector_scores[0]
    star_sym, star_ag, star_res = max(best_syms, key=lambda x: x[2].confidence)
    atm_k = int(round(star_res.underlying_price / star_ag.spec["strike_step"]) * star_ag.spec["strike_step"])
    opt_t = "CE" if star_res.trade_recommendation == "BUY_CE" else "PE"
    print(f" 🎯 SECTOR ROTATION ALPHA: Capital is heavily rotating into [{best_sec}]!")
    print(f"    Top Actionable Intraday Trade: {star_res.trade_recommendation} {star_sym} (Strike {atm_k}{opt_t} @ Spot ₹{star_res.underlying_price:,.1f})")
    print(f"    Thesis: {star_res.analysis_thesis}")
    print("=" * 82 + "\n")


def scan_fo_universe():
    """
    Scans the top liquid F&O stocks across all sectors to identify high-probability
    intraday option breakout and short-covering setups.
    """
    universe = [
        "HAL", "TATASTEEL", "M&M", "TATAMOTORS", "HDFCBANK",
        "SBIN", "SUNPHARMA", "BHARTIARTL", "LT", "ICICIBANK",
        "RELIANCE", "INFY", "TCS", "ITC"
    ]
    results = []

    for sym in universe:
        agent = AIAgentTrader(sym)
        res = agent.inspect_market()
        results.append((sym, agent, res))

    # Sort by confidence descending
    results.sort(key=lambda x: x[2].confidence, reverse=True)

    print("\n" + "=" * 82)
    print("      NUVAMA INSTITUTIONAL F&O MULTI-SECTOR SCANNER (TOP BREAKOUTS)")
    print("=" * 82)
    print(f" {'Rank':<4} | {'Symbol':<11} | {'Spot Px':<9} | {'Sentiment':<18} | {'Signal':<10} | {'Conviction':<10}")
    print("-" * 82)

    for i, (sym, agent, res) in enumerate(results, 1):
        spot_str = f"₹{res.underlying_price:,.0f}"
        icon = "🚀" if "BUY" in res.trade_recommendation else "⚖️"
        conf_str = f"{int(res.confidence * 100)}% {icon}"
        print(f"  {i:<3} | {sym:<11} | {spot_str:<9} | {res.sentiment:<18} | {res.trade_recommendation:<10} | {conf_str:<10}")

    print("-" * 82)
    top_sym, top_agent, top_res = results[0]
    atm_strike = int(round(top_res.underlying_price / top_agent.spec["strike_step"]) * top_agent.spec["strike_step"])
    opt_type = "CE" if top_res.trade_recommendation == "BUY_CE" else "PE"
    print(f" 🔥 TOP ACTIONABLE INTRADAY CALL: {top_res.trade_recommendation} {top_sym} (Strike {atm_strike}{opt_type})")
    print(f"    Thesis: {top_res.analysis_thesis}")
    print("=" * 82 + "\n")


def scan_machine_radar():
    """
    Machine-Level Autonomous Universe Radar:
    Unlike a human trader who can only look at 1 stock at a time,
    the machine evaluates all liquid stocks across all sectors simultaneously in parallel.
    Filters out noise, pinpoints exact institutional flow, and selects the #1 mathematical edge trade.
    """
    universe = [
        "HAL", "TATASTEEL", "M&M", "TATAMOTORS", "HDFCBANK",
        "SBIN", "SUNPHARMA", "BHARTIARTL", "LT", "ICICIBANK",
        "RELIANCE", "INFY", "TCS", "ITC"
    ]
    bullish_breakouts = []
    bearish_breakdowns = []
    neutral_chop = []

    for sym in universe:
        agent = AIAgentTrader(sym)
        res = agent.inspect_market()
        data = {
            "symbol": sym,
            "spot": res.underlying_price,
            "signal": res.trade_recommendation,
            "confidence": res.confidence,
            "sentiment": res.sentiment,
            "velocity": res.near_atm_oi_velocity,
            "pcr": res.pcr,
            "thesis": res.analysis_thesis,
            "strike_step": agent.spec["strike_step"],
            "lot_size": agent.spec["lot_size"],
        }
        if res.trade_recommendation == "BUY_CE":
            bullish_breakouts.append(data)
        elif res.trade_recommendation == "BUY_PE":
            bearish_breakdowns.append(data)
        else:
            neutral_chop.append(data)

    bullish_breakouts.sort(key=lambda x: (x["confidence"], x["velocity"]), reverse=True)
    bearish_breakdowns.sort(key=lambda x: (x["confidence"], abs(x["velocity"])), reverse=True)

    print("\n" + "=" * 84)
    print("      FRONTIER AI PARALLEL MARKET RADAR (FULL MACHINE SWEEP)")
    print("=" * 84)
    print(f" Total Symbols Evaluated Simultaneously: {len(universe)} stocks in parallel")
    print(f" Market Regimes Detected: {len(bullish_breakouts)} Long Breakouts | {len(bearish_breakdowns)} Short Breakdowns | {len(neutral_chop)} Rangebound Chop")
    print("-" * 84)

    print(" 🚀 TOP INSTITUTIONAL LONG MOMENTUM (CALL BUY SETUPS):")
    for i, b in enumerate(bullish_breakouts[:4], 1):
        atm_k = int(round(b["spot"] / b["strike_step"]) * b["strike_step"])
        print(f"   [{i}] {b['symbol']:<11} | Spot: ₹{b['spot']:>7,.1f} | Strike: {atm_k}CE | Conviction: {int(b['confidence']*100)}% | Flow: {b['sentiment']}")

    print("-" * 84)
    print(" ⚖️ CHOP / LOW-VOLATILITY ZONE (MACHINE SITTING FLAT / PREVENTING THETA BLEED):")
    chop_syms = ", ".join(c["symbol"] for c in neutral_chop)
    print(f"   Ignored Stocks: {chop_syms} (Dealers pinning price; option buyers lose to theta)")

    print("-" * 84)
    best = bullish_breakouts[0]
    best_k = int(round(best["spot"] / best["strike_step"]) * best["strike_step"])
    print(f" 🏆 THE MACHINE'S #1 EXECUTION CHOICE (INSTITUTIONAL HIGHEST EDGE):")
    print(f"    Action: {best['signal']} {best['symbol']} (Strike {best_k}CE @ Spot ₹{best['spot']:,.1f})")
    print(f"    Why: {best['thesis']}")
    print("=" * 84 + "\n")


def scan_super_setups():
    """
    Evaluates the Senior's Full Institutional Playbook:
    1. Technicals: VWAP, 9 EMA, 40 EMA
    2. Nuvama Derivatives: OI velocity, Walls, PCR
    3. Quantsapp: Net GEX, Squeeze Telemetry
    4. Sensibull: Generals Macro Alignment
    5. NSE Market Lens: Advances/Declines Breadth, Sector Turnover
    """
    universe = [
        "HAL", "TATASTEEL", "M&M", "TATAMOTORS", "HDFCBANK",
        "SBIN", "SUNPHARMA", "BHARTIARTL", "LT", "ICICIBANK",
        "RELIANCE", "INFY", "TCS", "ITC"
    ]
    breadth = TechnicalLensEngine.get_market_lens_breadth()
    total_w = sum(w for _, w in GENERALS_COMPOSITE)
    w_score = 0.0
    for sym, wt in GENERALS_COMPOSITE:
        r = AIAgentTrader(sym).inspect_market()
        mult = 1.0 if "BUY_CE" in r.trade_recommendation else (-1.0 if "BUY_PE" in r.trade_recommendation else 0.0)
        w_score += mult * wt
    norm_w = w_score / total_w
    generals_status = "GREEN_LIGHT" if norm_w >= 0.25 else ("RED_LIGHT" if norm_w <= -0.25 else "CAUTION")

    evaluations = []
    for sym in universe:
        agent = AIAgentTrader(sym)
        underlying_px, chain = agent.load_option_chain_from_tape()
        opt_res = OptionsAnalyticsEngine.analyze(underlying_px, chain, symbol=sym)
        gex_res = GreeksGexEngine.evaluate_frontier_landscape(sym, underlying_px, chain)

        setup = TechnicalLensEngine.synthesize_super_setup(
            symbol=sym,
            current_price=underlying_px,
            options_recommendation=opt_res.trade_recommendation,
            options_confidence=opt_res.confidence,
            gex_regime=gex_res.gex_regime,
            gamma_squeeze_score=gex_res.gamma_squeeze_score,
            generals_status=generals_status,
        )
        evaluations.append((sym, agent, opt_res, gex_res, setup))

    evaluations.sort(key=lambda x: x[4]["super_setup_score"], reverse=True)

    print("\n" + "=" * 90)
    print("      5-ENGINE INSTITUTIONAL SUPER-SETUP MATRIX (SENIOR + MACHINE SYNTHESIS)")
    print("=" * 90)
    print(f" NSE Market Lens Breadth: {breadth.advances} Adv / {breadth.declines} Dec (A/D Ratio: {breadth.ad_ratio} -> {breadth.market_breadth_sentiment})")
    print(f" Macro Generals (6 Generals Composite: 40.44% NIFTY): {generals_status} (Score: {norm_w*100:+.1f}%)")
    print("-" * 90)
    print(f" {'Rank':<4} | {'Symbol':<11} | {'Spot Px':<9} | {'VWAP':<9} | {'9/40 EMA':<12} | {'OI Flow':<10} | {'Score':<8} | {'Super Decision'}")
    print("-" * 90)

    for i, (sym, ag, opt, gex, s) in enumerate(evaluations, 1):
        t = s["technicals"]
        ema_str = "9 > 40 ✅" if t["trend_regime"] == "BULLISH_TREND_ALIGNMENT" else ("9 < 40 🔻" if t["trend_regime"] == "BEARISH_TREND_ALIGNMENT" else "Tangled ⚠️")
        icon = "🌟 TIER-1" if s["super_setup_score"] >= 75 else ("🔹 WATCH" if s["super_setup_score"] >= 55 else "▫️ FLAT")
        print(f"  {i:<3} | {sym:<11} | ₹{s['current_price']:>7,.1f} | ₹{t['vwap']:>7,.1f} | {ema_str:<12} | {opt.sentiment:<10} | {s['super_setup_score']:>3}/100 | {icon} {s['decision']}")

    print("-" * 90)
    star_sym, star_ag, star_opt, star_gex, star_s = evaluations[0]
    atm_k = int(round(star_s["current_price"] / star_ag.spec["strike_step"]) * star_ag.spec["strike_step"])
    opt_t = "CE" if "BUY_CE" in star_s["decision"] else "PE"
    print(f" 🚀 HIGHEST MATHEMATICAL CONFLUENCE CALL: {star_s['decision']} {star_sym} (Strike {atm_k}{opt_t})")
    print(f"    Super Setup Score: {star_s['super_setup_score']}/100")
    print("    Confluence Checklist:")
    for pt in star_s["confluence_points"]:
        print(f"     • {pt}")
    print("=" * 90 + "\n")


def scan_quant_physics():
    """
    Sub-Second Machine-Level Quantitative Alpha Sweep:
    Leaves lagging retail indicators (VWAP, 9/40 EMA) behind.
    Evaluates order book micro-price skew, OFI kinematics, second-order Greeks (Vanna/Charm),
    and flow toxicity to detect institutional smart money before candles even form.
    """
    universe = [
        "HAL", "TATASTEEL", "M&M", "TATAMOTORS", "HDFCBANK",
        "SBIN", "SUNPHARMA", "BHARTIARTL", "LT", "ICICIBANK",
        "RELIANCE", "INFY", "TCS", "ITC"
    ]
    results = []

    for sym in universe:
        agent = AIAgentTrader(sym)
        underlying_px, chain = agent.load_option_chain_from_tape()
        opt_res = OptionsAnalyticsEngine.analyze(underlying_px, chain, symbol=sym)

        atm_k = round(underlying_px / agent.spec["strike_step"]) * agent.spec["strike_step"]
        atm_obj = next((s for s in chain if s.strike == atm_k), chain[0])
        is_bull = "BUY_CE" in opt_res.trade_recommendation

        state = QuantitativeAlphaEngine.evaluate_machine_alpha(
            symbol=sym,
            spot_price=underlying_px,
            strike_step=agent.spec["strike_step"],
            lot_size=agent.spec["lot_size"],
            call_oi_atm=atm_obj.call_oi,
            put_oi_atm=atm_obj.put_oi,
            net_oi_velocity_15m=opt_res.near_atm_oi_velocity,
            is_bullish_bias=is_bull,
        )
        results.append((sym, agent, opt_res, state))

    # Sort by toxicity and OFI acceleration
    results.sort(key=lambda x: (x[3].flow_toxicity_score, x[3].ofi_acceleration), reverse=True)

    print("\n" + "=" * 94)
    print("      FRONTIER MACHINE QUANTITATIVE PHYSICS ENGINE (TRANSCENDING RETAIL EMAs)")
    print("=" * 94)
    print(" Methodology: Micro-Price Skew + Kinematic OFI Acceleration + Vanna/Charm Dealer Flow")
    print("-" * 94)
    print(f" {'Rank':<4} | {'Symbol':<11} | {'Spot Px':<9} | {'Micro Lead':<11} | {'Queue Imb':<10} | {'OFI Accel':<11} | {'Toxicity':<10} | {'Quant Alpha Signal'}")
    print("-" * 94)

    for i, (sym, ag, opt, q) in enumerate(results, 1):
        lead_str = f"{q.micro_price_edge_pts:+.2f} pts"
        queue_str = f"{q.queue_imbalance_ratio:+.2f}"
        acc_str = f"{q.ofi_acceleration:+.0f}"
        tox_str = f"{q.flow_toxicity_score:.1f}%"
        icon = "⚡ SWEEP" if q.flow_toxicity_score >= 80 else ("🔹 FLOW" if q.flow_toxicity_score >= 60 else "▫️ CHOP")
        print(f"  {i:<3} | {sym:<11} | ₹{q.underlying_price:>7,.1f} | {lead_str:<11} | {queue_str:<10} | {acc_str:<11} | {tox_str:<10} | {icon} {q.machine_signal}")

    print("-" * 94)
    top_sym, top_ag, top_opt, top_q = results[0]
    atm_strike = int(round(top_q.underlying_price / top_ag.spec["strike_step"]) * top_ag.spec["strike_step"])
    print(f" 🚀 PURE MACHINE EDGE: {top_q.machine_signal} on {top_sym} (Strike {atm_strike}CE)")
    print(f"    Micro-Price Advantage: Spot is ₹{top_q.underlying_price:,.1f}, but Micro-Price is ₹{top_q.micro_price:,.2f} ({top_q.micro_price_edge_pts:+.2f} lead)")
    print(f"    Institutional Mechanics: {top_q.execution_advantage}")
    print(f"    Dealer Dynamic Hedging: Vanna flow ₹{top_q.vanna_dealer_flow:+.1f} Cr | Charm flow ₹{top_q.charm_dealer_flow:+.1f} Cr")
    print("=" * 94 + "\n")


class _TeeStream:
    """Mirrors stdout to both console and ai/logs/agent_trader.log line-buffered."""

    def __init__(self, primary, log_file):
        self.primary = primary
        self.log_file = log_file

    def write(self, data: str) -> None:
        self.primary.write(data)
        self.log_file.write(data)
        if "\n" in data:
            self.flush()

    def flush(self) -> None:
        self.primary.flush()
        self.log_file.flush()


def run_autonomous_loop(
    poll_interval_sec: float = 5.0,
    max_cycles: Optional[int] = None,
    live_mode: bool = True,
):
    """
    Autonomous Live Paper Trading Daemon for AI Agent CIO:
    Runs between 09:15 AM and 03:15 PM IST on genuine Zerodha Kite market prices.
    1. Monitors and manages active positions every cycle (live Bid/LTP, 20-EMA, Rally Runner, 45m time-stop).
    2. Scans active F&O universe dynamically ranked by Super Setup Score + Triple Ribbon alignment.
    3. Enforces 30-minute per-symbol cooldown after time-stops and ₹50,000 Demat budget.
    4. Automatically squares off 100% of open positions at 03:15 PM IST using live market marks.
    """
    log_dir = AI_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_dir / "agent_trader.log", "a", encoding="utf-8", buffering=1)
    orig_stdout = sys.stdout
    sys.stdout = _TeeStream(orig_stdout, log_fh)

    ledger = AIPaperLedger()
    shared_feed = LiveKiteFeed() if live_mode else None
    cooldown_until: Dict[str, float] = {}
    last_scan_ts = 0.0

    if live_mode:
        voided = ledger.void_synthetic_open_trades()
        if voided:
            print(f"🧹 [CLEANUP] Voided {voided} legacy synthetic open position(s) prior to starting genuine LIVE_KITE session.")

    print("\n" + "═" * 80)
    print(f" 🦅 STARTING ANTIGRAVITY AI CIO AUTONOMOUS {'LIVE KITE' if live_mode else 'MOCK'} DAEMON")
    print("    Demat Trading Budget: ₹50,000.00 | Mode: FAIL-CLOSED LIVE QUOTE PAPER")
    print("    Technicals: Real 5m 9/20/40 EMA Triple Ribbon + Exchange VWAP + GEX + Monte Carlo")
    print("    Poll Interval: 5.0s (Manage) / 15.0s (Scan) | Mandatory Square-Off: 03:15 PM IST")
    print("═" * 80 + "\n")

    cycle = 0
    try:
        while True:
            cycle += 1
            try:
                from zoneinfo import ZoneInfo
                now_ist = datetime.datetime.now(ZoneInfo("Asia/Kolkata"))

                # Pre-market / post-market guard in live mode
                if live_mode:
                    if now_ist.hour < 9 or (now_ist.hour == 9 and now_ist.minute < 15):
                        if cycle == 1 or cycle % 12 == 0:
                            print(f"⏳ [LIVE WAIT] Pre-market ({now_ist.strftime('%H:%M:%S IST')}). Waiting for 09:15 IST open...")
                        if max_cycles and cycle >= max_cycles:
                            break
                        time.sleep(poll_interval_sec)
                        continue

                # Check 03:15 PM EOD Square-Off
                if now_ist.hour > 15 or (now_ist.hour == 15 and now_ist.minute >= 15):
                    print("🏁 [AI Agent] 03:15 PM IST Reached! Triggering mandatory Live EOD Cash Square-Off...")
                    square_off_all_positions(reason="HARD_EOD_SQUAREOFF_0315PM", live_mode=live_mode, feed=shared_feed)
                    break

                # 1. Manage currently open positions across all symbols
                open_trades = ledger.get_open_trades()
                if open_trades:
                    seen_underlyings = []
                    for t in open_trades:
                        u = t["underlying"]
                        if u not in seen_underlyings:
                            seen_underlyings.append(u)
                    for u in seen_underlyings:
                        try:
                            res = AIAgentTrader(u, live_mode=live_mode, feed=shared_feed).run_cycle()
                            for stopped_sym in res.get("time_stopped", []):
                                cooldown_until[stopped_sym] = time.time() + 1800.0
                                print(f"❄️ [COOLDOWN] {stopped_sym} placed on 30-min cooldown after 45m time-stop.")
                        except Exception as p_err:
                            print(f"⚠️ [AI Agent] Position monitor error on {u}: {p_err}")

                # 2. Risk Circuit Breakers Check before considering new entries
                source_filter = "LIVE_KITE" if live_mode else None
                today_pnl = ledger.get_today_pnl(source_tag=source_filter)
                consec_losses = ledger.get_recent_consecutive_losses(source_tag=source_filter)

                if today_pnl <= -3500.0:
                    if cycle % 12 == 0:
                        print(f"🛑 [CIRCUIT BREAKER ACTIVE] Daily loss limit -₹3,500 hit (Current: -₹{abs(today_pnl):,.2f})! Halting new entries.")
                elif consec_losses >= 2:
                    if cycle % 12 == 0:
                        print(f"⚠️ [LOSS THROTTLE ACTIVE] {consec_losses} consecutive losses detected! Cooling down.")
                else:
                    # 3. If position slots available (< 3 concurrent), dynamically scan eligible universe (Cutoff 14:40 IST)
                    after_entry_cutoff = live_mode and (now_ist.hour > 14 or (now_ist.hour == 14 and now_ist.minute >= 40))
                    open_trades = ledger.get_open_trades()
                    now_ts = time.time()
                    if after_entry_cutoff:
                        if cycle % 12 == 1:
                            print(f"🛡️ [MANAGE ONLY >= 14:40 IST] Blocking new entries with {max(0, (15*60+15) - (now_ist.hour*60+now_ist.minute))}m to EOD square-off.")
                    elif len(open_trades) < 3 and (not live_mode or (now_ts - last_scan_ts) >= 15.0):
                        last_scan_ts = now_ts
                        base_universe = [
                            "HAL", "TATASTEEL", "M&M", "TMPV", "HDFCBANK", "SBIN",
                            "SUNPHARMA", "BHARTIARTL", "LT", "ICICIBANK", "RELIANCE",
                            "INFY", "TCS", "ITC", "BEL", "HINDALCO", "NIFTY",
                        ]
                        universe = shared_feed.get_available_universe(base_universe) if (live_mode and shared_feed) else base_universe

                        if live_mode:
                            generals_norm = AIAgentTrader("NIFTY", live_mode=True, feed=shared_feed)._compute_generals_score()
                            ranked = []
                            for sym in universe:
                                if any(t["underlying"] == sym for t in open_trades):
                                    continue
                                if cooldown_until.get(sym, 0.0) > now_ts:
                                    continue
                                try:
                                    ev = AIAgentTrader(sym, live_mode=True, feed=shared_feed).inspect_live_setup()
                                    if ev and ev["signal"] in ("BUY_CE", "BUY_PE") and ev["confidence"] >= 0.75:
                                        ranked.append((ev["score"], sym, ev))
                                except Exception:
                                    continue
                                time.sleep(0.15)

                            ranked.sort(key=lambda x: x[0], reverse=True)
                            if not ranked and cycle % 4 == 1:
                                print(f"🔎 [LIVE SCAN @ {now_ist.strftime('%H:%M:%S')}] Evaluated {len(universe)} real F&O symbols; standing aside (no high-conviction alignment).")
                            for _, sym, ev in ranked:
                                try:
                                    agent = AIAgentTrader(sym, live_mode=True, feed=shared_feed)
                                    res = agent.run_cycle(generals_norm=generals_norm, precomputed_live=ev)
                                    if res.get("action") == "OPENED_TRADE":
                                        break
                                except Exception:
                                    continue
                        else:
                            for sym in universe:
                                if any(t["underlying"] == sym for t in open_trades):
                                    continue
                                if cooldown_until.get(sym, 0.0) > now_ts:
                                    continue
                                try:
                                    res = AIAgentTrader(sym, live_mode=False).run_cycle()
                                    if res.get("action") == "OPENED_TRADE":
                                        break
                                except Exception:
                                    continue

            except Exception as loop_err:
                print(f"⚠️ [AI Agent Loop Error]: {loop_err}")

            if max_cycles and cycle >= max_cycles:
                break

            time.sleep(poll_interval_sec)
    except KeyboardInterrupt:
        print("\n🛑 [AI Agent] Keyboard Interrupt received. Daemon paused.")
    finally:
        sys.stdout = orig_stdout
        log_fh.close()


def _format_inr(val: float) -> str:
    return f"-₹{abs(val):,.2f}" if val < 0 else f"₹{val:,.2f}"


def cio_enter_trade(
    symbol: str,
    direction: str,
    contract: str,
    sl: float,
    target: float,
    lots: int = 1,
    thesis: str = "",
    entry_price: Optional[float] = None,
    live_mode: bool = True,
    ledger: Optional[AIPaperLedger] = None,
    feed: Optional[LiveKiteFeed] = None,
    bridge: Optional[SmartTradingBridge] = None,
    enforce_market_clock: bool = True,
) -> Dict:
    """
    Executes an explicit AI CIO trade decision (never auto-triggered by dumb loops).
    Enforces fail-closed live quotes, spread <= 2.5%, Desk Collision Mutex, F&O ban,
    ₹50,000 Demat budget, <= ₹3,500 risk cap, and logs thesis in `ai_cio_decisions`.
    """
    active_ledger = ledger or AIPaperLedger()
    active_bridge = bridge or SmartTradingBridge()
    active_feed = feed or (LiveKiteFeed(bridge=active_bridge) if live_mode else None)
    up_sym = symbol.upper()
    up_dir = direction.upper()
    up_contract = contract.upper()

    if up_dir not in ("BUY_CE", "BUY_PE"):
        return {"status": "REJECTED", "reason": "INVALID_DIRECTION"}
    if not thesis.strip():
        return {"status": "REJECTED", "reason": "MISSING_CIO_THESIS"}

    clock = evaluate_session_clock()
    if live_mode and enforce_market_clock and not clock["entries_permitted"]:
        active_ledger.log_cio_decision(
            action="STAND_ASIDE", underlying=up_sym, contract=up_contract,
            regime=clock["expiry_regime"],
            reasoning=f"Blocked CIO entry outside live entry window ({clock['phase']}, {clock['minutes_to_1515_eod']}m left): {thesis}",
        )
        return {"status": "REJECTED", "reason": f"CLOCK_BLOCKED:{clock['phase']}"}

    desk_ctx = active_bridge.get_unified_desk_context()
    if up_sym in set(desk_ctx.get("collision_blocked_underlyings") or []):
        return {"status": "REJECTED", "reason": f"DESK_COLLISION_MUTEX:{up_sym}"}
    if up_sym in set((desk_ctx.get("nse_premarket") or {}).get("fno_ban_list") or []):
        return {"status": "REJECTED", "reason": f"NSE_FNO_BAN:{up_sym}"}

    open_trades = active_ledger.get_open_trades()
    if len(open_trades) >= 3:
        return {"status": "REJECTED", "reason": "MAX_CONCURRENT_POSITIONS_3"}
    if any(t["underlying"] == up_sym for t in open_trades):
        return {"status": "REJECTED", "reason": f"SYMBOL_ALREADY_OPEN:{up_sym}"}

    today_pnl = active_ledger.get_today_pnl(source_tag="LIVE_KITE" if live_mode else None)
    if today_pnl <= -3500.0:
        return {"status": "REJECTED", "reason": f"DAILY_CIRCUIT_BREAKER_HIT:{today_pnl}"}

    lot_size = OptionsAnalyticsEngine.SYMBOL_SPECS.get(up_sym, {}).get("lot_size", 25)
    if active_feed is not None:
        meta = active_feed.get_symbol_meta(up_sym)
        if meta:
            lot_size = int(meta["lot_size"])

    exec_px = float(entry_price or 0.0)
    if live_mode:
        if active_feed is None:
            return {"status": "REJECTED", "reason": "NO_LIVE_FEED"}
        quotes = active_feed.fetch_contract_quotes([up_contract])
        q = quotes.get(up_contract)
        if not q:
            return {"status": "REJECTED", "reason": f"NO_LIVE_QUOTE:{up_contract}"}
        bid, ask, ltp = float(q.get("bid", 0.0)), float(q.get("ask", 0.0)), float(q.get("ltp", 0.0))
        if bid > 0 and ask > 0:
            spread_pct = ((ask - bid) / ((bid + ask) / 2.0)) * 100.0
            if spread_pct > 2.5:
                return {"status": "REJECTED", "reason": f"WIDE_SPREAD:{spread_pct:.2f}%"}
        exec_px = ask if ask > 0 else ltp

    if exec_px <= 0.0 or sl <= 0.0 or sl >= exec_px:
        return {"status": "REJECTED", "reason": f"INVALID_PRICE_OR_SL (entry={exec_px}, sl={sl})"}

    qty = lots * lot_size
    ticket = exec_px * qty
    deployed = sum(float(t["entry_price"]) * int(t["quantity"]) for t in open_trades)
    avail = max(0.0, 50000.0 - deployed)
    if ticket > avail:
        return {"status": "REJECTED", "reason": f"INSUFFICIENT_DEMAT_CASH: needs ₹{ticket:,.0f}, avail ₹{avail:,.0f}"}

    max_risk = (exec_px - sl) * qty
    if max_risk > 3500.0:
        return {"status": "REJECTED", "reason": f"SINGLE_TRADE_RISK_TOO_HIGH: ₹{max_risk:,.0f} > ₹3,500"}

    tag = "LIVE_KITE" if live_mode else "CIO_MOCK"
    tid = active_ledger.open_trade(
        underlying=up_sym,
        symbol=up_contract,
        direction=up_dir,
        entry_price=round(exec_px, 2),
        stop_loss=round(sl, 2),
        target=round(target, 2),
        lots=lots,
        lot_size=lot_size,
        thesis=thesis,
        source_tag=tag,
    )
    active_ledger.log_cio_decision(
        action="ENTER",
        trade_id=tid,
        underlying=up_sym,
        contract=up_contract,
        regime=clock["expiry_regime"],
        reasoning=thesis,
        context={"entry": exec_px, "sl": sl, "target": target, "lots": lots, "qty": qty, "risk_inr": round(max_risk, 2)},
    )
    return {
        "status": "OPENED_TRADE",
        "trade_id": tid,
        "contract": up_contract,
        "entry_price": round(exec_px, 2),
        "sl": round(sl, 2),
        "target": round(target, 2),
        "lots": lots,
        "quantity": qty,
        "ticket_inr": round(ticket, 2),
        "max_risk_inr": round(max_risk, 2),
    }


def cio_manage_trade(
    trade_id: int,
    action: str,
    reason: str,
    sl: Optional[float] = None,
    exit_lots: Optional[int] = None,
    exit_price: Optional[float] = None,
    live_mode: bool = True,
    ledger: Optional[AIPaperLedger] = None,
    feed: Optional[LiveKiteFeed] = None,
) -> Dict:
    """
    Executes explicit AI CIO management action on an active trade:
    - `TRAIL_SL`: Stay & Ride Rally by raising stop-loss to `sl`.
    - `PARTIAL_BOOK`: Book profit on `exit_lots` at live Bid mark while leaving runner open (and optionally raise SL).
    - `BOOK_PROFIT` / `EXIT`: Close full trade at live Bid mark with explicit reason.
    """
    active_ledger = ledger or AIPaperLedger()
    act = action.upper()
    open_trades = active_ledger.get_open_trades()
    trade = next((t for t in open_trades if int(t["trade_id"]) == int(trade_id)), None)
    if not trade:
        return {"status": "ERROR", "reason": f"OPEN_TRADE_NOT_FOUND:{trade_id}"}

    contract = trade["symbol"]
    clock = evaluate_session_clock()

    if act == "TRAIL_SL":
        if sl is None or sl <= 0:
            return {"status": "ERROR", "reason": "INVALID_NEW_SL"}
        ok = active_ledger.update_stop_loss(trade_id, round(float(sl), 2))
        if ok:
            active_ledger.log_cio_decision(
                action="TRAIL_SL", trade_id=trade_id, underlying=trade["underlying"],
                contract=contract, regime=clock["expiry_regime"], reasoning=reason,
                context={"old_sl": trade["stop_loss"], "new_sl": round(float(sl), 2)},
            )
            return {"status": "SL_UPDATED", "trade_id": trade_id, "new_sl": round(float(sl), 2)}
        return {"status": "ERROR", "reason": "SL_UPDATE_FAILED"}

    mark = float(exit_price or 0.0)
    if live_mode:
        active_feed = feed or LiveKiteFeed()
        quotes = active_feed.fetch_contract_quotes([contract])
        q = quotes.get(contract)
        if not q or float(q.get("mark", 0.0)) <= 0:
            return {"status": "FAIL_CLOSED", "reason": f"NO_LIVE_EXIT_MARK:{contract}"}
        mark = float(q["mark"])
    if mark <= 0:
        return {"status": "ERROR", "reason": "INVALID_EXIT_PRICE"}

    if act == "PARTIAL_BOOK":
        book_lots = int(exit_lots or max(1, int(trade["lots"]) // 2))
        res = active_ledger.partial_close_trade(trade_id, book_lots, mark, exit_reason=f"CIO_PARTIAL_BOOK:{reason[:40]}")
        if res and sl is not None and sl > float(trade["stop_loss"]):
            active_ledger.update_stop_loss(trade_id, round(float(sl), 2))
            res["runner_new_sl"] = round(float(sl), 2)
        if res:
            active_ledger.log_cio_decision(
                action="PARTIAL_BOOK", trade_id=trade_id, underlying=trade["underlying"],
                contract=contract, regime=clock["expiry_regime"], reasoning=reason, context=res,
            )
        return {"status": "PARTIAL_BOOKED", **res} if res else {"status": "ERROR", "reason": "PARTIAL_BOOK_FAILED"}

    if act in ("BOOK_PROFIT", "EXIT"):
        reason_tag = f"CIO_{act}"
        res = active_ledger.close_trade(trade_id, mark, exit_reason=reason_tag)
        if res:
            active_ledger.log_cio_decision(
                action=act, trade_id=trade_id, underlying=trade["underlying"],
                contract=contract, regime=clock["expiry_regime"], reasoning=reason, context=res,
            )
        return {"status": "CLOSED", **res} if res else {"status": "ERROR", "reason": "CLOSE_FAILED"}

    return {"status": "ERROR", "reason": f"UNSUPPORTED_ACTION:{act}"}


def run_guardian_loop(
    poll_interval_sec: float = 5.0,
    cockpit_refresh_sec: float = 15.0,
    max_cycles: Optional[int] = None,
    live_mode: bool = True,
):
    """
    Honest Reflex Guardian + Algo Cockpit Refresh Daemon (ZERO autonomous auto-buying).
    1. Continuously keeps `ai/runtime/cockpit_briefing.json` fresh every `cockpit_refresh_sec`
       so Antigravity always sees a sub-second lightweight briefing.
    2. Monitors active open trades every `poll_interval_sec` strictly to enforce:
       - Hard emergency Stop-Loss (`live Bid mark <= sl`)
       - Mandatory `15:15 IST` End-of-Day Cash Square-Off.
    3. Never opens trades or takes profit on its own — entry, SL trails, rally holds, and exits
       stay strictly in the hands of the AI CIO (`cio-enter` / `cio-manage`).
    """
    from zoneinfo import ZoneInfo
    log_dir = AI_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_dir / "agent_trader.log", "a", encoding="utf-8", buffering=1)
    orig_stdout = sys.stdout
    sys.stdout = _TeeStream(orig_stdout, log_fh)

    ledger = AIPaperLedger()
    bridge = SmartTradingBridge()
    shared_feed = LiveKiteFeed(bridge=bridge) if live_mode else None
    cockpit_builder = CockpitBuilder(feed=shared_feed, bridge=bridge, ledger=ledger)

    if live_mode:
        voided = ledger.void_synthetic_open_trades()
        if voided:
            print(f"🧹 [GUARDIAN] Voided {voided} legacy synthetic open position(s).")

    print("\n" + "═" * 80)
    print(" 🛡️ STARTING REFLEX GUARDIAN + ALGO COCKPIT ENGINE (AI-PILOT MODE)")
    print("    Authority Split: Machine refreshes Cockpit + guards Emergency SL / 15:15 EOD")
    print("    Entry / Trail / Profit-Book / Exit Decisions: STRICTLY ANTIGRAVITY CIO")
    print("═" * 80 + "\n")

    cycle = 0
    last_brief_ts = 0.0
    try:
        while True:
            cycle += 1
            now_ist = datetime.datetime.now(ZoneInfo("Asia/Kolkata"))
            try:
                if live_mode and (now_ist.hour > 15 or (now_ist.hour == 15 and now_ist.minute >= 15)):
                    print("🏁 [GUARDIAN] 15:15 IST Reached! Executing mandatory EOD Cash Square-Off...")
                    square_off_all_positions(reason="HARD_EOD_SQUAREOFF_0315PM", live_mode=live_mode, feed=shared_feed)
                    cockpit_builder.build_briefing(save_json=True)
                    break

                # 1. Hard Emergency Stop-Loss Guard ONLY on already-open positions
                open_trades = ledger.get_open_trades()
                if open_trades and live_mode and shared_feed:
                    quotes = shared_feed.fetch_contract_quotes([t["symbol"] for t in open_trades])
                    for t in open_trades:
                        tid = int(t["trade_id"])
                        sym = t["symbol"]
                        sl = float(t["stop_loss"])
                        entry_p = float(t["entry_price"])
                        q = quotes.get(sym)
                        if not q or float(q.get("mark", 0.0)) <= 0:
                            print(f"⚠️ [GUARDIAN FAIL-CLOSED] Missing live quote for #{tid} ({sym}); holding safely.")
                            continue
                        mark = float(q["mark"])
                        if mark <= sl:
                            reason = "GUARDIAN_TRAILING_SL_HIT" if sl > entry_p else "GUARDIAN_HARD_SL_HIT"
                            res = ledger.close_trade(tid, mark, reason)
                            ledger.log_cio_decision(
                                action="EXIT", trade_id=tid, underlying=t["underlying"], contract=sym,
                                reasoning=f"Automatic guardian stop-loss trigger at ₹{mark:.2f} <= SL ₹{sl:.2f}",
                                context=res,
                            )
                            print(f"🛑 [GUARDIAN SL] Closed #{tid} ({sym}) @ ₹{mark:.2f} (SL ₹{sl:.2f}) | Net {_format_inr(float(res.get('net_pnl', 0.0)))}")
                        else:
                            unr = (mark - entry_p) * int(t["quantity"])
                            pct = ((mark - entry_p) / entry_p * 100.0) if entry_p > 0 else 0.0
                            print(f"🛡️ [GUARDIAN] #{tid} {sym} @ ₹{mark:.2f} | Unr: {_format_inr(unr)} ({pct:+.1f}%) | SL ₹{sl:.2f}")

                # 2. Refresh lightweight AI Cockpit snapshot cleanly every `cockpit_refresh_sec`
                now_ts = time.time()
                if (now_ts - last_brief_ts) >= cockpit_refresh_sec:
                    last_brief_ts = now_ts
                    b = cockpit_builder.build_briefing(save_json=True)
                    top = b["top_candidates"][0]["symbol"] if b["top_candidates"] else "STAND_ASIDE"
                    if cycle == 1 or cycle % 3 == 0:
                        print(f"📡 [COCKPIT REFRESH @ {now_ist.strftime('%H:%M:%S')}] Phase={b['clock']['phase']} | Open={len(b['active_positions'])} | Top={top} | Desk={b['desk_connector']['status']}")

            except Exception as err:
                print(f"⚠️ [GUARDIAN ERROR]: {err}")

            if max_cycles and cycle >= max_cycles:
                break
            time.sleep(poll_interval_sec)
    except KeyboardInterrupt:
        print("\n🛑 [GUARDIAN] Interrupted cleanly.")
    finally:
        sys.stdout = orig_stdout
        log_fh.close()


def print_session_status():
    from zoneinfo import ZoneInfo
    ledger = AIPaperLedger()
    open_trades = ledger.get_open_trades()
    ist = ZoneInfo("Asia/Kolkata")
    today_ist = datetime.datetime.now(ist).strftime("%Y-%m-%d")

    live_closed_today = []
    synth_closed_today = []

    with sqlite3.connect(ledger.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM ai_paper_trades WHERE status = 'CLOSED' ORDER BY trade_id ASC").fetchall()
        for r in rows:
            d = dict(r)
            try:
                dt = datetime.datetime.fromisoformat(d["entry_timestamp"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.timezone.utc)
                row_date = dt.astimezone(ist).strftime("%Y-%m-%d")
            except Exception:
                row_date = ""

            if row_date == today_ist and d.get("source_tag") == "LIVE_KITE":
                live_closed_today.append(d)
            elif row_date == today_ist:
                synth_closed_today.append(d)

    print("\n" + "=" * 68)
    print("      ANTIGRAVITY AI CIO — SESSION SCORECARD (IST)")
    print("=" * 68)
    print(f" • Active Open Positions: {len(open_trades)}")
    for t in open_trades:
        tag = t.get("source_tag", "SHADOW")
        print(f"   - #{t['trade_id']} [{tag}] {t['symbol']}: Entry ₹{t['entry_price']} | SL ₹{t['stop_loss']} | Lots: {t['lots']}")

    live_wins = sum(1 for r in live_closed_today if (r.get("net_pnl") or 0) > 0)
    live_losses = len(live_closed_today) - live_wins
    live_net = sum(float(r.get("net_pnl") or 0.0) for r in live_closed_today)
    live_fric = sum(float(r.get("statutory_friction") or 0.0) for r in live_closed_today)

    print("-" * 68)
    print(f" • Today's LIVE_KITE Closed  : {len(live_closed_today)} ({live_wins} Wins / {live_losses} Losses)")
    print(f" • Today's LIVE Realized PnL : {_format_inr(live_net)} (Friction: -₹{live_fric:,.2f})")
    if synth_closed_today:
        s_net = sum(float(r.get("net_pnl") or 0.0) for r in synth_closed_today)
        print(f" • Legacy Synthetic Today    : {len(synth_closed_today)} closed | Mock PnL {_format_inr(s_net)} (Excluded from Live)")
    print("=" * 68 + "\n")


def main():
    parser = argparse.ArgumentParser(description="AI Autonomous Options Trading Cockpit & CIO")
    parser.add_argument(
        "command",
        choices=[
            "cockpit", "cio-enter", "cio-manage", "guardian", "logic-test",
            "scan", "run-once", "loop", "summary", "status", "generals",
            "scan-fo", "sectors", "radar", "super-setup", "quant", "square-off",
        ],
        help="Command to execute",
    )
    parser.add_argument("--symbol", type=str, default="NIFTY", help="Underlying symbol (e.g. NIFTY, HAL)")
    parser.add_argument("--tape", type=str, default=None, help="Path to tick tape database")
    parser.add_argument("--mock", action="store_true", help="Use offline synthetic test fixtures instead of live Zerodha Kite API")
    parser.add_argument("--json", action="store_true", help="Print JSON output for cockpit/CIO commands")
    parser.add_argument("--top", type=int, default=5, help="Max top Helping-Hand candidates in cockpit")
    parser.add_argument("--days", type=int, default=25, help="Historical days for logic-test validation")
    # CIO entry / management args
    parser.add_argument("--contract", type=str, default="", help="Exact NFO option contract symbol")
    parser.add_argument("--direction", type=str, default="BUY_CE", help="BUY_CE or BUY_PE")
    parser.add_argument("--lots", type=int, default=1, help="Number of lots")
    parser.add_argument("--sl", type=float, default=None, help="Stop-loss option price")
    parser.add_argument("--target", type=float, default=0.0, help="Target option price")
    parser.add_argument("--entry-price", type=float, default=None, help="Explicit mock entry price (ignored in live mode)")
    parser.add_argument("--exit-price", type=float, default=None, help="Explicit mock exit price (ignored in live mode)")
    parser.add_argument("--thesis", type=str, default="", help="Written AI CIO thesis")
    parser.add_argument("--trade-id", type=int, default=0, help="Target open trade ID for cio-manage")
    parser.add_argument("--action", type=str, default="", help="TRAIL_SL | PARTIAL_BOOK | BOOK_PROFIT | EXIT")
    parser.add_argument("--exit-lots", type=int, default=None, help="Lots to book for PARTIAL_BOOK")
    parser.add_argument("--reason", type=str, default="", help="AI CIO reason for cio-manage")
    args = parser.parse_args()

    live_mode = not args.mock

    if args.command == "cockpit":
        briefing = CockpitBuilder().build_briefing(max_candidates=args.top)
        print(json.dumps(briefing, indent=2) if args.json else format_briefing_text(briefing))
        return

    if args.command == "logic-test":
        rep = run_logic_validation(days=args.days)
        print(json.dumps(rep, indent=2) if args.json else format_logic_report(rep))
        return

    if args.command == "cio-enter":
        res = cio_enter_trade(
            symbol=args.symbol,
            direction=args.direction,
            contract=args.contract,
            sl=float(args.sl or 0.0),
            target=float(args.target or 0.0),
            lots=args.lots,
            thesis=args.thesis,
            entry_price=args.entry_price,
            live_mode=live_mode,
        )
        print(json.dumps(res, indent=2))
        return

    if args.command == "cio-manage":
        res = cio_manage_trade(
            trade_id=args.trade_id,
            action=args.action,
            reason=args.reason or "CIO decision",
            sl=args.sl,
            exit_lots=args.exit_lots,
            exit_price=args.exit_price,
            live_mode=live_mode,
        )
        print(json.dumps(res, indent=2))
        return

    if args.command == "guardian":
        run_guardian_loop(live_mode=live_mode)
        return

    if args.command == "status":
        print_session_status()
        return

    if args.command == "loop":
        run_autonomous_loop(live_mode=live_mode)
        return

    if args.command == "square-off":
        square_off_all_positions(live_mode=live_mode)
        return

    if args.command == "quant":
        scan_quant_physics()
        return

    if args.command == "super-setup":
        scan_super_setups()
        return

    if args.command == "radar":
        scan_machine_radar()
        return

    if args.command == "generals":
        inspect_the_generals()
        return

    if args.command == "sectors":
        scan_sector_heatmap()
        return

    if args.command == "scan-fo":
        scan_fo_universe()
        return

    agent = AIAgentTrader(symbol=args.symbol, live_mode=live_mode)

    if args.command == "scan":
        result = agent.inspect_market(args.tape)
        agent.print_dashboard(result)
    elif args.command == "run-once":
        agent.run_cycle(args.tape)
    elif args.command == "summary":
        summary = agent.ledger.get_summary()
        print("\n" + "=" * 50)
        print("          AI PAPER TRADING PERFORMANCE")
        print("=" * 50)
        print(f" Total Closed Trades : {summary.get('total_trades', 0)}")
        print(f" Win Rate            : {summary.get('win_rate', 0.0)}%")
        print(f" Wins / Losses       : {summary.get('wins', 0)} / {summary.get('losses', 0)}")
        print(f" Gross Profit / Loss : ₹{summary.get('gross_pnl', 0.0):,.2f}")
        print(f" STT & Statutory Fees: ₹{summary.get('total_friction', 0.0):,.2f}")
        print(f" Net Realized Profit : ₹{summary.get('net_pnl', 0.0):,.2f}")
        print("=" * 50 + "\n")


if __name__ == "__main__":
    main()
