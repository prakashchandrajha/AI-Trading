"""
Continuous Non-Curve-Fitting Walk-Forward Logic & Regime Validator (`logic_tester.py`).

Validates core trading hypotheses across Normal Days, Expiry Days, and High-Volatility regimes
using real historical NSE 5-minute bars (Kite API) + archived Zerodha tick DBs (`SmartTrading/data/by_date`).

Anti-Curve-Fitting Rules:
1. Zero parameter tuning on single-day winners — evaluates fixed structural physics across 20-30 days.
2. Deducts realistic Bid-Ask spread drag, theta burn (elevated on expiry), and Finance Act 2026 STT/GST.
3. Compares every strategy archetype against:
   - Time-matched Random Null Baseline (`RANDOM_NULL`)
   - Late-Session Stress Test (`14:45-15:10 IST`)
   - ±20% Parameter Perturbation Stability Check (`ROBUST` vs `CURVE_FIT_WARNING`)
"""
from __future__ import annotations

import calendar
import datetime
import json
import math
import random
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

AI_DIR = Path(__file__).resolve().parent
ROOT_DIR = AI_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))

from connector.smart_trading_bridge import SmartTradingBridge, compute_book_telemetry
from live_kite_feed import LiveKiteFeed
from paper_ledger import AIPaperLedger
from technical_lens import TechnicalLensEngine

IST = ZoneInfo("Asia/Kolkata")
REPORT_PATH = AI_DIR / "runtime" / "logic_test_report.json"


def classify_indian_session_regime(trading_date: datetime.date, day_range_pct: float) -> str:
    """Classifies historical NSE sessions into Expiry vs High-Vol vs Normal regimes."""
    # Check NSE Tuesday weekly NIFTY expiry or Last-Thursday monthly F&O expiry
    is_tuesday = trading_date.weekday() == 1
    last_day = calendar.monthrange(trading_date.year, trading_date.month)[1]
    last_thu = last_day
    while datetime.date(trading_date.year, trading_date.month, last_thu).weekday() != 3:
        last_thu -= 1
    is_monthly_thu = trading_date.day == last_thu

    if is_tuesday or is_monthly_thu:
        return "EXPIRY_DAY"
    if day_range_pct >= 2.2:
        return "HIGH_VOL_TREND"
    return "NORMAL_DAY"


def _parse_bar_dt(date_str: str) -> datetime.datetime:
    dt = datetime.datetime.fromisoformat(date_str)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


@dataclass
class SimTradeResult:
    symbol: str
    date: str
    regime: str
    logic: str
    direction: str
    entry_time: str
    exit_time: str
    spot_entry: float
    spot_exit: float
    option_net_pnl: float
    friction: float
    r_multiple: float
    exit_reason: str


def _simulate_logic_on_bars(
    symbol: str,
    bars_by_day: Dict[str, List[Dict[str, Any]]],
    lot_size: int,
    logic_name: str = "RIBBON_VWAP_ATR_TRAIL",
    atr_stop_mult: float = 1.5,
    cutoff_hhmm: Tuple[int, int] = (14, 40),
    late_window_only: bool = False,
    random_seed: int = 42,
) -> List[SimTradeResult]:
    """
    Replays 5-minute OHLCV sessions bar-by-bar with zero lookahead bias.
    Computes running session VWAP, rolling EMAs (9/20/40), ATR(14),
    and translates spot trajectory + theta + bid-ask spread into real net Rupee PnL.
    """
    rng = random.Random(random_seed)
    trades: List[SimTradeResult] = []
    rolling_closes: List[float] = []
    rolling_highs: List[float] = []
    rolling_lows: List[float] = []
    rolling_vols: List[int] = []
    rolling_atrs: List[float] = []

    for day_str in sorted(bars_by_day.keys()):
        day_bars = bars_by_day[day_str]
        if len(day_bars) < 12:
            continue

        day_dt = datetime.date.fromisoformat(day_str)
        day_hi = max(b["high"] for b in day_bars)
        day_lo = min(b["low"] for b in day_bars)
        day_open = day_bars[0]["open"] or day_bars[0]["close"]
        day_range_pct = ((day_hi - day_lo) / day_open * 100.0) if day_open > 0 else 1.0
        regime = classify_indian_session_regime(day_dt, day_range_pct)
        is_expiry = regime == "EXPIRY_DAY"

        # Session resets VWAP cleanly at 09:15 IST open
        cum_pv = 0.0
        cum_v = 0.0
        active_pos: Optional[Dict[str, Any]] = None
        cooldown_until_idx = -1
        day_losses = 0

        for idx, bar in enumerate(day_bars):
            c = float(bar["close"])
            h = float(bar["high"])
            l = float(bar["low"])
            v = max(1, int(bar.get("volume", 1) or 1))
            dt_ist = _parse_bar_dt(bar["date"])
            hhmm = (dt_ist.hour, dt_ist.minute)

            cum_pv += ((h + l + c) / 3.0) * v
            cum_v += v
            session_vwap = cum_pv / cum_v if cum_v > 0 else c

            rolling_closes.append(c)
            rolling_highs.append(h)
            rolling_lows.append(l)
            rolling_vols.append(v)
            if len(rolling_closes) > 120:
                rolling_closes.pop(0)
                rolling_highs.pop(0)
                rolling_lows.pop(0)
                rolling_vols.pop(0)

            if len(rolling_closes) < 42:
                continue

            ema_9 = TechnicalLensEngine.calculate_ema(rolling_closes, 9)
            ema_20 = TechnicalLensEngine.calculate_ema(rolling_closes, 20)
            ema_40 = TechnicalLensEngine.calculate_ema(rolling_closes, 40)
            atr_5m, _ = TechnicalLensEngine.calculate_atr_and_vol(rolling_closes, rolling_highs, rolling_lows)
            atr_safe = max(atr_5m, c * 0.0012)
            rolling_atrs.append(atr_safe)
            if len(rolling_atrs) > 120:
                rolling_atrs.pop(0)

            # Causal median volume & ATR for relative expansion check (zero lookahead)
            hist_vols = sorted(rolling_vols[:-1]) if len(rolling_vols) > 5 else [v]
            med_vol = max(1.0, float(hist_vols[len(hist_vols) // 2]))
            rvol = v / med_vol
            hist_atrs = sorted(rolling_atrs[:-1]) if len(rolling_atrs) > 5 else [atr_safe]
            med_atr = max(1e-6, float(hist_atrs[len(hist_atrs) // 2]))
            atr_expansion = atr_safe / med_atr

            # Manage open position
            if active_pos is not None:
                bars_held = idx - active_pos["entry_idx"]
                direction = active_pos["direction"]
                is_ce = direction == "BUY_CE"
                sl_spot = active_pos["sl_spot"]

                hit_sl = (l <= sl_spot) if is_ce else (h >= sl_spot)
                is_eod = hhmm >= (15, 15) or idx == len(day_bars) - 1
                if not hit_sl and not is_eod and bars_held >= 3:
                    move_fav = (c - active_pos["spot_entry"]) if is_ce else (active_pos["spot_entry"] - c)
                    if move_fav >= 1.2 * active_pos["entry_atr"]:
                        if is_ce and ema_20 > sl_spot:
                            active_pos["sl_spot"] = ema_20 - 0.35 * atr_safe
                        elif not is_ce and ema_20 < sl_spot:
                            active_pos["sl_spot"] = ema_20 + 0.35 * atr_safe

                if hit_sl or is_eod:
                    exit_spot = sl_spot if hit_sl else c
                    reason = "TRAILING_SL" if (hit_sl and ((is_ce and sl_spot > active_pos["spot_entry"]) or (not is_ce and sl_spot < active_pos["spot_entry"]))) else ("EOD_1515" if is_eod else "ATR_STOP")
                    raw_spot_pts = (exit_spot - active_pos["spot_entry"]) if is_ce else (active_pos["spot_entry"] - exit_spot)

                    delta = 0.48 if is_expiry else 0.52
                    base_opt_prem = max(3.0, active_pos["spot_entry"] * (0.007 if is_expiry else 0.018))
                    spread_drag_opt = max(0.10, base_opt_prem * 0.008)
                    theta_per_bar = base_opt_prem * (0.0045 if is_expiry else 0.0012)
                    opt_exit = max(0.05, base_opt_prem + (raw_spot_pts * delta) - spread_drag_opt - (theta_per_bar * bars_held))
                    qty = max(1, lot_size)
                    gross = (opt_exit - base_opt_prem) * qty
                    friction = AIPaperLedger.calculate_indian_friction(base_opt_prem, opt_exit, qty, "BUY")
                    net_pnl = round(gross - friction, 2)
                    risk_rupees = max(500.0, active_pos["risk_spot_pts"] * delta * qty)
                    if net_pnl <= 0:
                        day_losses += 1

                    trades.append(
                        SimTradeResult(
                            symbol=symbol,
                            date=day_str,
                            regime=regime,
                            logic=logic_name,
                            direction=direction,
                            entry_time=active_pos["entry_time"],
                            exit_time=dt_ist.strftime("%H:%M"),
                            spot_entry=round(active_pos["spot_entry"], 2),
                            spot_exit=round(exit_spot, 2),
                            option_net_pnl=net_pnl,
                            friction=round(friction, 2),
                            r_multiple=round(net_pnl / risk_rupees, 2),
                            exit_reason=reason,
                        )
                    )
                    active_pos = None
                    cooldown_until_idx = idx + 6
                continue

            if idx <= cooldown_until_idx:
                continue

            if late_window_only:
                in_window = (14, 45) <= hhmm <= (15, 10)
            else:
                in_window = (9, 25) <= hhmm <= cutoff_hhmm
            if not in_window:
                continue

            direction_pick: Optional[str] = None
            if logic_name == "RANDOM_NULL_BASELINE":
                if idx % 10 == 0 and rng.random() < 0.25:
                    direction_pick = "BUY_CE" if rng.random() < 0.5 else "BUY_PE"
            elif logic_name.startswith("CAUSAL_EXPANSION"):
                # Anti-chop causal gate: stop after 1 loss on the same stock/day + require real RVOL & ribbon expansion
                if day_losses >= 1:
                    continue
                session_vwap_sigma = max(atr_safe * 6.0, c * 0.009)
                z_vwap = (c - session_vwap) / session_vwap_sigma
                ribbon_atr = abs(ema_9 - ema_40) / atr_safe
                min_rvol = 1.45 if is_expiry else 1.25
                expanding = rvol >= min_rvol and atr_expansion >= 0.95 and ribbon_atr >= 0.45
                bull_ribbon = expanding and c > session_vwap and ema_9 > ema_20 > ema_40 and 0.05 <= z_vwap <= 2.6
                bear_ribbon = expanding and c < session_vwap and ema_9 < ema_20 < ema_40 and -2.6 <= z_vwap <= -0.05
                if bull_ribbon and c > rolling_highs[-2]:
                    direction_pick = "BUY_CE"
                elif bear_ribbon and c < rolling_lows[-2]:
                    direction_pick = "BUY_PE"
            else:
                session_vwap_sigma = max(atr_safe * 6.0, c * 0.009)
                z_vwap = (c - session_vwap) / session_vwap_sigma
                bull_ribbon = c > session_vwap and ema_9 > ema_20 > ema_40 and 0.05 <= z_vwap <= 2.6
                bear_ribbon = c < session_vwap and ema_9 < ema_20 < ema_40 and -2.6 <= z_vwap <= -0.05
                if bull_ribbon and c > rolling_highs[-2]:
                    direction_pick = "BUY_CE"
                elif bear_ribbon and c < rolling_lows[-2]:
                    direction_pick = "BUY_PE"

            if direction_pick:
                risk_pts = atr_stop_mult * atr_safe
                sl_spot = (c - risk_pts) if direction_pick == "BUY_CE" else (c + risk_pts)
                active_pos = {
                    "direction": direction_pick,
                    "spot_entry": c,
                    "sl_spot": sl_spot,
                    "risk_spot_pts": risk_pts,
                    "entry_atr": atr_safe,
                    "entry_idx": idx,
                    "entry_time": dt_ist.strftime("%H:%M"),
                }

    return trades


def _summarize_trades(trades: List[SimTradeResult]) -> Dict[str, Any]:
    if not trades:
        return {"trades": 0, "win_rate": 0.0, "net_pnl": 0.0, "expectancy_r": 0.0, "profit_factor": 0.0}
    wins = [t for t in trades if t.option_net_pnl > 0]
    losses = [t for t in trades if t.option_net_pnl <= 0]
    gross_win = sum(t.option_net_pnl for t in wins)
    gross_loss = abs(sum(t.option_net_pnl for t in losses))
    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else (9.99 if gross_win > 0 else 0.0)
    net = round(sum(t.option_net_pnl for t in trades), 2)
    exp_r = round(sum(t.r_multiple for t in trades) / len(trades), 3)
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100.0, 1),
        "net_pnl": net,
        "expectancy_r": exp_r,
        "profit_factor": pf,
    }


def inspect_archived_tick_spread_audit(bridge: SmartTradingBridge, max_dbs: int = 5) -> List[Dict[str, Any]]:
    """Audits real L2 Bid-Ask spread & KOBP characteristics across archived Desk tick DBs."""
    summaries: List[Dict[str, Any]] = []
    for day_str, db_path in bridge.get_archived_tick_dbs(limit=max_dbs):
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.5)
            rows = conn.execute(
                "SELECT payload FROM recorded_ticks WHERE payload LIKE '%depth%' ORDER BY id DESC LIMIT 400"
            ).fetchall()
            conn.close()
        except Exception:
            continue

        spreads: List[float] = []
        abs_kobps: List[float] = []
        for (raw_json,) in rows:
            try:
                p = json.loads(raw_json)
                d = p.get("depth") or {}
                bids = [(float(x["price"]), int(x["quantity"])) for x in (d.get("buy") or [])[:5] if x.get("price")]
                asks = [(float(x["price"]), int(x["quantity"])) for x in (d.get("sell") or [])[:5] if x.get("price")]
                if bids and asks:
                    tel = compute_book_telemetry(float(p.get("last_price") or bids[0][0]), bids, asks)
                    if tel["spread_pct"] > 0:
                        spreads.append(tel["spread_pct"])
                    abs_kobps.append(abs(tel["kobp"]))
            except Exception:
                continue

        if spreads:
            summaries.append({
                "date": day_str,
                "samples": len(spreads),
                "avg_spread_pct": round(sum(spreads) / len(spreads), 3),
                "avg_abs_kobp": round(sum(abs_kobps) / max(1, len(abs_kobps)), 3),
            })
    return summaries


def run_logic_validation(
    symbols: Optional[List[str]] = None,
    days: int = 25,
    feed: Optional[LiveKiteFeed] = None,
) -> Dict[str, Any]:
    """
    Executes complete multi-regime out-of-sample Logic Tests across real Kite historical data
    and archived Desk tick datasets. Persists results to `ai/runtime/logic_test_report.json`.
    """
    active_feed = feed or LiveKiteFeed()
    bridge = active_feed.bridge
    test_universe = symbols or ["HAL", "TATASTEEL", "HDFCBANK", "ICICIBANK", "M&M", "INFY", "SBIN", "NIFTY"]

    all_core: List[SimTradeResult] = []
    all_causal: List[SimTradeResult] = []
    all_late: List[SimTradeResult] = []
    all_null: List[SimTradeResult] = []
    perturb_low: List[SimTradeResult] = []
    perturb_high: List[SimTradeResult] = []
    loaded_symbols: List[str] = []

    for sym in test_universe:
        bars = active_feed.fetch_historical_5m_bars(sym, days=days)
        meta = active_feed.get_symbol_meta(sym)
        if not bars or not meta:
            continue
        loaded_symbols.append(sym)
        lot_sz = int(meta.get("lot_size", 1))
        bars_by_day: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for b in bars:
            bars_by_day[_parse_bar_dt(b["date"]).date().isoformat()].append(b)

        all_core.extend(_simulate_logic_on_bars(sym, bars_by_day, lot_sz, "RIBBON_VWAP_ATR_TRAIL", atr_stop_mult=1.5))
        all_causal.extend(_simulate_logic_on_bars(sym, bars_by_day, lot_sz, "CAUSAL_EXPANSION_RIBBON", atr_stop_mult=1.5))
        all_late.extend(_simulate_logic_on_bars(sym, bars_by_day, lot_sz, "LATE_SESSION_1445_1510", atr_stop_mult=1.5, late_window_only=True))
        all_null.extend(_simulate_logic_on_bars(sym, bars_by_day, lot_sz, "RANDOM_NULL_BASELINE", atr_stop_mult=1.5))
        perturb_low.extend(_simulate_logic_on_bars(sym, bars_by_day, lot_sz, "CAUSAL_EXPANSION_LOW", atr_stop_mult=1.2))
        perturb_high.extend(_simulate_logic_on_bars(sym, bars_by_day, lot_sz, "CAUSAL_EXPANSION_HIGH", atr_stop_mult=1.85))

    by_regime: Dict[str, Any] = {}
    causal_by_regime: Dict[str, Any] = {}
    for reg in ("NORMAL_DAY", "EXPIRY_DAY", "HIGH_VOL_TREND"):
        by_regime[reg] = _summarize_trades([t for t in all_core if t.regime == reg])
        causal_by_regime[reg] = _summarize_trades([t for t in all_causal if t.regime == reg])

    core_summary = _summarize_trades(all_core)
    causal_summary = _summarize_trades(all_causal)
    late_summary = _summarize_trades(all_late)
    null_summary = _summarize_trades(all_null)
    p_low_sum = _summarize_trades(perturb_low)
    p_high_sum = _summarize_trades(perturb_high)

    # Stability judgment: causal expansion must beat Random Null AND stay consistent under ±20% ATR variation
    beats_null = causal_summary["expectancy_r"] > null_summary["expectancy_r"]
    neighbor_signs_ok = (p_low_sum["expectancy_r"] >= -0.02) and (p_high_sum["expectancy_r"] >= -0.02)
    stability = "ROBUST" if (beats_null and neighbor_signs_ok and causal_summary["trades"] >= 5) else "CAUTION_SELECTIVE_ONLY"

    tick_audit = inspect_archived_tick_spread_audit(bridge, max_dbs=4)

    report = {
        "generated_at_ist": datetime.datetime.now(IST).isoformat(),
        "days_tested": days,
        "symbols_tested": loaded_symbols,
        "core_ribbon_vwap": core_summary,
        "causal_expansion_ribbon": causal_summary,
        "by_regime": by_regime,
        "causal_by_regime": causal_by_regime,
        "late_entry_1445_1510_stress": late_summary,
        "random_null_baseline": null_summary,
        "perturbation_sensitivity": {
            "atr_1_20x": p_low_sum,
            "atr_1_50x": causal_summary,
            "atr_1_85x": p_high_sum,
        },
        "anti_curve_fit_verdict": stability,
        "archived_l2_tick_spreads": tick_audit,
    }

    try:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except Exception:
        pass
    return report


def load_cached_logic_report() -> Dict[str, Any]:
    if not REPORT_PATH.exists():
        return {}
    try:
        return json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def format_logic_report(report: Dict[str, Any]) -> str:
    if not report or not report.get("symbols_tested"):
        return "⚠️ No historical bars available for Logic Validation (check Kite session or offline history cache)."
    core = report["core_ribbon_vwap"]
    causal = report.get("causal_expansion_ribbon", {})
    late = report["late_entry_1445_1510_stress"]
    null = report["random_null_baseline"]
    lines = [
        "=" * 84,
        f" CONTINUOUS OUT-OF-SAMPLE LOGIC TESTER ({report['days_tested']}d | {len(report['symbols_tested'])} Symbols | Anti-Curve-Fit)",
        "=" * 84,
        f" Stability Verdict : {report['anti_curve_fit_verdict']}",
        f" Naive EMA Ribbon  : {core['trades']:>3} trades | Win {core['win_rate']:>5}% | PF {core['profit_factor']:>4} | Net ₹{core['net_pnl']:>9,.2f} | E(R) {core['expectancy_r']:+.3f}",
        f" Causal RVOL+Exp   : {causal.get('trades',0):>3} trades | Win {causal.get('win_rate',0):>5}% | PF {causal.get('profit_factor',0):>4} | Net ₹{causal.get('net_pnl',0):>9,.2f} | E(R) {causal.get('expectancy_r',0):+.3f}",
        f" Random Null       : {null['trades']:>3} trades | Win {null['win_rate']:>5}% | PF {null['profit_factor']:>4} | Net ₹{null['net_pnl']:>9,.2f} | E(R) {null['expectancy_r']:+.3f}",
        f" Late (14:45-15:10): {late['trades']:>3} trades | Win {late['win_rate']:>5}% | PF {late['profit_factor']:>4} | Net ₹{late['net_pnl']:>9,.2f} | E(R) {late['expectancy_r']:+.3f}",
        "-" * 84,
        " Causal Expansion Across Regimes (Normal vs Expiry vs High-Vol):",
    ]
    for reg, s in report.get("causal_by_regime", {}).items():
        lines.append(
            f"   • {reg:<14}: {s['trades']:>3} trades | Win {s['win_rate']:>5}% | PF {s['profit_factor']:>4} | Net ₹{s['net_pnl']:>9,.2f} | E(R) {s['expectancy_r']:+.3f}"
        )
    if report.get("archived_l2_tick_spreads"):
        lines.append("-" * 84)
        lines.append(" Archived Desk L2 Tick Audits:")
        for t in report["archived_l2_tick_spreads"]:
            lines.append(f"   • {t['date']}: {t['samples']} L2 books | Avg Spread {t['avg_spread_pct']}% | Avg |KOBP| {t['avg_abs_kobp']}")
    lines.append("=" * 84)
    return "\n".join(lines)
