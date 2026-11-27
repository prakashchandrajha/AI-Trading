"""
Lightweight AI Pilot Decision Cockpit (`cockpit.py`).

Role: Think like an Institutional Algo Pre-Processor so Antigravity stays LIGHT.
Continuously gathers heavy market telemetry across:
1. Zerodha Kite Live Quotes, 5-level Depth, 5m OHLCV, Exchange VWAP & Batched Breadth
2. SmartTrading Read-Only Connector (Desk HTTP/SHM/ZMQ signals, Collision Mutex, NSE Premarket & FII OI)
3. Sensibull / Nuvama Option Walls, Max Pain, PCR, Net GEX & Liquidity Checks
4. Volatility-Normalized 9/20/40 EMA + Z_VWAP + ATR Physics (Zero fixed curve-fit targets)
5. Spread-Adjusted Intraday GPU Monte Carlo PoT + Continuous Logic Test Regime Benchmarks

Compresses 500+ raw signals into a lightweight ~35-line briefing + JSON (`ai/runtime/cockpit_briefing.json`)
containing ONLY active position rally/profit hints and Top 1-3 vetted setups (plus explicit `STAND_ASIDE`).
All real decisions (`Which one`, `Why`, `Entry`, `SL`, `Book Profit` vs `Ride Rally`, `Exit`) stay in AI hands.
"""
from __future__ import annotations

import datetime
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

AI_DIR = Path(__file__).resolve().parent
ROOT_DIR = AI_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))

from broker_execution import _spread_net_mark_from_quotes, round_nse_tick
from connector.smart_trading_bridge import SmartTradingBridge, compute_book_telemetry
from gpu_engine import GPUQuantitativeEngine
from greeks_gex import GreeksGexEngine
from live_kite_feed import LiveKiteFeed
from logic_tester import load_cached_logic_report
from options_analytics import OptionsAnalyticsEngine
from paper_ledger import AIPaperLedger
from quantitative_alpha import QuantitativeAlphaEngine
from smart_edge import evaluate_smart_edge
from technical_lens import TechnicalLensEngine
from trading_policy import get_trading_policy

IST = ZoneInfo("Asia/Kolkata")
BRIEFING_PATH = AI_DIR / "runtime" / "cockpit_briefing.json"
CASH_SETTLED_INDICES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}

CORE_FO_UNIVERSE = [
    "HAL", "TATASTEEL", "M&M", "TMPV", "HDFCBANK", "SBIN",
    "SUNPHARMA", "BHARTIARTL", "LT", "ICICIBANK", "RELIANCE",
    "INFY", "TCS", "ITC", "BEL", "HINDALCO", "MARUTI", "CIPLA",
    "NTPC", "ONGC", "NIFTY",
]

GENERALS_WEIGHTS = [
    ("HDFCBANK", 0.0995),
    ("ICICIBANK", 0.0939),
    ("RELIANCE", 0.0730),
    ("BHARTIARTL", 0.0563),
    ("LT", 0.0425),
    ("INFY", 0.0392),
]


def evaluate_session_clock(now_ist: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """Determines exact market window, minutes to EOD square-off, and expiry regime."""
    policy = get_trading_policy()
    now = now_ist or datetime.datetime.now(IST)
    today = now.date()
    is_expiry = policy.is_expiry_day(today)

    eod_hh_mm = policy.get_eod_square_off(on_date=today, is_expiry=is_expiry)
    mkt_close_hh_mm = policy.get_market_close(on_date=today)
    eod_dt = now.replace(hour=eod_hh_mm[0], minute=eod_hh_mm[1], second=0, microsecond=0)
    mins_to_eod = int((eod_dt - now).total_seconds() // 60)
    hhmm = (now.hour, now.minute)

    if now.weekday() >= 5:
        phase = "WEEKEND_CLOSED"
    elif hhmm < (9, 15):
        phase = "PRE_MARKET"
    elif hhmm < (9, 25):
        phase = "OPENING_QUARANTINE"
    elif hhmm < (11, 15):
        phase = "GOLDEN_MORNING"
    elif hhmm < (13, 25):
        phase = "MIDDAY_SELECTIVE"
    elif hhmm < policy.entry_cutoff:
        phase = "GOLDEN_AFTERNOON"
    elif hhmm < eod_hh_mm:
        phase = "MANAGE_ONLY_NO_NEW_ENTRIES"
    elif hhmm < mkt_close_hh_mm:
        phase = "POST_SQUAREOFF_CLOSING"
    else:
        phase = "MARKET_CLOSED"

    entries_permitted = phase in ("GOLDEN_MORNING", "MIDDAY_SELECTIVE", "GOLDEN_AFTERNOON") and mins_to_eod >= 35

    return {
        "time_ist": now.strftime("%Y-%m-%d %H:%M:%S IST"),
        "phase": phase,
        "expiry_regime": "EXPIRY_GAMMA_DAY" if is_expiry else "NORMAL_DAY",
        "minutes_to_1515_eod": max(0, mins_to_eod),
        "minutes_to_eod": max(0, mins_to_eod),
        "eod_cutoff": f"{eod_hh_mm[0]:02d}:{eod_hh_mm[1]:02d}",
        "entries_permitted": entries_permitted,
    }


class CockpitBuilder:
    """Builds the ultra-light AI CIO decision cockpit from live & desk feeds."""

    def __init__(
        self,
        feed: Optional[LiveKiteFeed] = None,
        bridge: Optional[SmartTradingBridge] = None,
        ledger: Optional[AIPaperLedger] = None,
        gpu: Optional[GPUQuantitativeEngine] = None,
    ) -> None:
        self.bridge = bridge or SmartTradingBridge()
        self.feed = feed or LiveKiteFeed(bridge=self.bridge)
        self.ledger = ledger or AIPaperLedger()
        self.gpu = gpu or GPUQuantitativeEngine()

    def _compute_generals_from_breadth(self, breadth_payload: Dict[str, Any]) -> Dict[str, Any]:
        moves = {m["symbol"]: m for m in (breadth_payload.get("stock_moves") or [])}
        tot_w = sum(w for _, w in GENERALS_WEIGHTS)
        score = 0.0
        detail: Dict[str, Any] = {}
        for sym, wt in GENERALS_WEIGHTS:
            m = moves.get(sym)
            if not m:
                continue
            chg = float(m.get("chg_pct", 0.0))
            above = bool(m.get("above_vwap", False))
            mult = 1.0 if (chg > 0.25 and above) else (-1.0 if (chg < -0.25 and not above) else 0.0)
            score += mult * wt
            detail[sym] = {"chg_pct": chg, "above_vwap": above}
        norm = round(score / tot_w, 3) if tot_w > 0 else 0.0
        status = "GREEN_LIGHT" if norm >= 0.25 else ("RED_LIGHT" if norm <= -0.25 else "CAUTION")
        return {"status": status, "weighted_score": norm, "constituents": detail}

    def _inspect_open_positions(
        self,
        open_trades: List[Dict[str, Any]],
        clock: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        if not open_trades:
            return []
        contract_keys = list(dict.fromkeys(
            sym for t in open_trades for sym in (t["symbol"], t.get("short_contract")) if sym
        ))
        quotes = self.feed.fetch_contract_quotes(contract_keys)
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        out: List[Dict[str, Any]] = []

        for t in open_trades:
            tid = t["trade_id"]
            u_sym = t["underlying"]
            c_sym = t["symbol"]
            short_sym = t.get("short_contract")
            short_entry = float(t.get("short_entry_price") or 0.0)
            entry_p = float(t["entry_price"])
            sl = float(t["stop_loss"])
            spot_sl = t.get("spot_stop_loss")
            qty = int(t["quantity"])
            direction = t["direction"]
            is_ce = direction == "BUY_CE"

            q = quotes.get(c_sym, {})
            if short_sym:
                short_q = quotes.get(short_sym)
                if q and short_q:
                    mark, short_exit = _spread_net_mark_from_quotes(q, short_q, entry_p, short_entry)
                else:
                    mark, short_exit = 0.0, short_entry
            else:
                mark = float(q.get("mark") or 0.0)
                short_exit = 0.0

            bid_d = q.get("bid_depth") or []
            ask_d = q.get("ask_depth") or []
            book = compute_book_telemetry(
                mark or entry_p, bid_d, ask_d, q.get("prev_bid_depth"), q.get("prev_ask_depth"), lot_size=max(1, qty // max(1, int(t["lots"])))
            )

            snap = self.feed.fetch_spot_and_option_chain(u_sym, num_strikes=4)
            spot = snap.spot_price if snap else 0.0
            tech = (
                TechnicalLensEngine.analyze_technicals(
                    u_sym, spot, snap.price_history, snap.volume_history, snap.vwap_live,
                    require_real_history=True, high_history=snap.high_history, low_history=snap.low_history
                )
                if snap and spot > 0 else None
            )
            opt_res = OptionsAnalyticsEngine.analyze(spot, snap.chain, symbol=u_sym) if snap and snap.chain else None

            try:
                ed = datetime.datetime.fromisoformat(t["entry_timestamp"])
                if ed.tzinfo is None:
                    ed = ed.replace(tzinfo=datetime.timezone.utc)
                elapsed_min = round((now_utc - ed).total_seconds() / 60.0, 1)
            except Exception:
                elapsed_min = 0.0

            if mark > 0:
                gross = (mark - entry_p) * qty
                if short_sym:
                    fric = AIPaperLedger.calculate_vertical_spread_friction(
                        long_entry=entry_p + short_entry,
                        long_exit=mark + short_exit,
                        short_entry=short_entry,
                        short_exit=short_exit,
                        quantity=qty,
                    )
                else:
                    fric = AIPaperLedger.calculate_indian_friction(entry_p, mark, qty, direction)
                net_est = round(gross - fric, 2)
                ret_pct = round(((mark - entry_p) / entry_p) * 100.0, 2) if entry_p > 0 else 0.0
            else:
                net_est = None
                ret_pct = None

            # Synthesize actionable Rally vs Book hint for CIO review
            wall_dist_atr = None
            if opt_res and tech and tech.atr_5m > 0:
                wall_px = opt_res.call_wall_strike if is_ce else opt_res.put_wall_strike
                if wall_px > 0:
                    dist = (wall_px - spot) if is_ce else (spot - wall_px)
                    wall_dist_atr = round(dist / tech.atr_5m, 2)

            hint = "HOLD_MONITOR"
            if clock["minutes_to_1515_eod"] <= 5:
                hint = "EOD_IMMINENT_PREPARE_EXIT"
            elif spot_sl and spot > 0 and ((is_ce and spot <= float(spot_sl)) or (not is_ce and spot >= float(spot_sl))):
                hint = "SPOT_SL_BREACHED_EXIT"
            elif ret_pct is not None and ret_pct >= 25.0 and wall_dist_atr is not None and wall_dist_atr <= 1.2:
                hint = "NEAR_WALL_BOOK_OR_PARTIAL_LOCK"
            elif tech and ((is_ce and spot > tech.ema_20 > tech.ema_40) or (not is_ce and spot < tech.ema_20 < tech.ema_40)):
                hint = "RIDE_RALLY_ABOVE_20EMA" if is_ce else "RIDE_RALLY_BELOW_20EMA"
            elif tech and ((is_ce and spot < tech.ema_20) or (not is_ce and spot > tech.ema_20)):
                hint = "20EMA_BREACHED_TIGHTEN_OR_EXIT"
            elif book["ask_replenish_warning"]:
                hint = "ASK_ABSORPTION_CAUTION"

            out.append({
                "trade_id": tid,
                "underlying": u_sym,
                "contract": c_sym,
                "short_contract": short_sym,
                "direction": direction,
                "lots": int(t["lots"]),
                "quantity": qty,
                "entry": entry_p,
                "live_mark": mark if mark > 0 else None,
                "sl": sl,
                "spot_sl": spot_sl,
                "target": float(t["target"]),
                "est_net_pnl": net_est,
                "ret_pct": ret_pct,
                "elapsed_min": elapsed_min,
                "spot": round(spot, 2) if spot else None,
                "vwap": tech.vwap if tech else None,
                "ema_9": tech.ema_9 if tech else None,
                "ema_20": tech.ema_20 if tech else None,
                "ema_40": tech.ema_40 if tech else None,
                "wall_dist_atr": wall_dist_atr,
                "opt_kobp": book["kobp"],
                "opt_spread_pct": book["spread_pct"],
                "cio_hint": hint,
            })
        return out

    @staticmethod
    def _build_naked_spec(
        cand: Any,
        snap: Any,
        is_ce: bool,
        opt_tag: str,
        available_cash: float,
        max_budget: float,
        flags: List[str],
    ) -> Optional[Dict[str, Any]]:
        tsym = snap.contract_map.get((cand.strike, opt_tag))
        if not tsym:
            return None
        ltp = cand.call_ltp if is_ce else cand.put_ltp
        bid = cand.call_bid if is_ce else cand.put_bid
        ask = cand.call_ask if is_ce else cand.put_ask
        oi = cand.call_oi if is_ce else cand.put_oi
        vol = cand.call_volume if is_ce else cand.put_volume
        mid = ((bid + ask) / 2.0) if (bid > 0 and ask > 0) else ltp
        spread_pct = ((ask - bid) / mid * 100.0) if (bid > 0 and ask > 0 and mid > 0) else 0.0
        entry_px = ask if ask > 0 else ltp
        if entry_px <= 0 or (oi <= 0 and vol <= 0) or spread_pct > 3.0:
            return None
        lot_cost = entry_px * snap.lot_size
        if lot_cost > available_cash or lot_cost > max_budget:
            return None
        if spread_pct > 2.0:
            flags.append(f"WARN:SPREAD_{spread_pct:.1f}%")
        return {
            "contract": tsym,
            "strike": cand.strike,
            "entry_ask": round(entry_px, 2),
            "bid": round(bid, 2),
            "spread_pct": round(spread_pct, 2),
            "lot_size": snap.lot_size,
            "lot_cost": round(lot_cost, 2),
            "oi": oi,
            "volume": vol,
            "is_vertical_spread": False,
        }

    @staticmethod
    def _build_vertical_spread_spec(
        atm_obj: Any,
        snap: Any,
        is_ce: bool,
        opt_tag: str,
        available_cash: float,
        policy: Any,
        flags: List[str],
    ) -> Optional[Dict[str, Any]]:
        long_tsym = snap.contract_map.get((atm_obj.strike, opt_tag))
        long_ask = (atm_obj.call_ask if is_ce else atm_obj.put_ask) or (atm_obj.call_ltp if is_ce else atm_obj.put_ltp)
        long_bid = (atm_obj.call_bid if is_ce else atm_obj.put_bid) or long_ask
        long_oi = atm_obj.call_oi if is_ce else atm_obj.put_oi
        long_vol = atm_obj.call_volume if is_ce else atm_obj.put_volume
        if not long_tsym or long_ask <= 0 or (long_oi <= 0 and long_vol <= 0):
            return None

        long_mid = (long_bid + long_ask) / 2.0 if long_bid > 0 else long_ask
        long_spread_pct = ((long_ask - long_bid) / max(0.01, long_mid)) * 100.0 if long_bid > 0 else 0.0
        # Sequential Zerodha Buy-First requires Cash >= long_ask * lot_size unless portfolio already has margin headroom
        if long_spread_pct > 3.0 or (long_ask * snap.lot_size) > available_cash:
            return None

        step_dir = 1 if is_ce else -1
        for k_steps in (1, 2):
            short_strike = atm_obj.strike + (step_dir * k_steps * snap.strike_step)
            short_obj = next((s for s in snap.chain if abs(s.strike - short_strike) < 0.01), None)
            if not short_obj:
                continue
            short_tsym = snap.contract_map.get((short_obj.strike, opt_tag))
            short_bid = (short_obj.call_bid if is_ce else short_obj.put_bid) or (short_obj.call_ltp if is_ce else short_obj.put_ltp)
            short_ask = (short_obj.call_ask if is_ce else short_obj.put_ask) or short_bid
            short_oi = short_obj.call_oi if is_ce else short_obj.put_oi
            short_vol = short_obj.call_volume if is_ce else short_obj.put_volume
            if not short_tsym or short_bid <= 0 or (short_oi <= 0 and short_vol <= 0):
                continue

            short_mid = (short_bid + short_ask) / 2.0 if short_ask >= short_bid > 0 else short_bid
            short_spread_pct = ((short_ask - short_bid) / max(0.01, short_mid)) * 100.0 if short_ask >= short_bid > 0 else 0.0
            if short_spread_pct > 4.0:
                continue

            net_debit_pts = round(long_ask - short_bid, 2)
            width_pts = round(abs(short_obj.strike - atm_obj.strike), 2)
            if net_debit_pts <= 0 or width_pts <= 0:
                continue

            # Reject over-priced debit spreads where Debit > 60% of strike width (R:R < 0.67)
            if net_debit_pts > (width_pts * 0.60):
                continue

            # Guard against hidden 2-leg illiquid exit slippage
            liq_pts = max(0.0, long_bid - short_ask)
            roundtrip_slip = max(0.0, net_debit_pts - liq_pts)
            if roundtrip_slip > max(1.0, net_debit_pts * 0.35):
                continue

            net_debit_inr = round(net_debit_pts * snap.lot_size, 2)
            max_profit_pts = round(width_pts - net_debit_pts, 2)
            max_profit_inr = round(max_profit_pts * snap.lot_size, 2)
            if 0 < net_debit_inr <= policy.max_spread_net_debit_inr and net_debit_inr <= available_cash:
                flags.append("STRUC:VERTICAL_SPREAD")
                return {
                    "contract": long_tsym,
                    "strike": atm_obj.strike,
                    "entry_ask": round(long_ask, 2),
                    "bid": round(long_bid, 2),
                    "spread_pct": round(long_spread_pct, 2),
                    "lot_size": snap.lot_size,
                    "lot_cost": net_debit_inr,
                    "oi": long_oi,
                    "volume": long_vol,
                    "is_vertical_spread": True,
                    "spread_spec": {
                        "long_contract": long_tsym,
                        "long_strike": atm_obj.strike,
                        "long_ask": round(long_ask, 2),
                        "short_contract": short_tsym,
                        "short_strike": short_obj.strike,
                        "short_bid": round(short_bid, 2),
                        "short_ask": round(short_ask, 2),
                        "width_pts": width_pts,
                        "net_debit_pts": net_debit_pts,
                        "net_debit_inr": net_debit_inr,
                        "max_profit_inr": max_profit_inr,
                        "reward_to_risk": round(max_profit_inr / max(1.0, net_debit_inr), 2),
                    },
                }
        return None

    def _compute_physics_levels_and_sizing(
        self,
        spot: float,
        tech: Any,
        atr_safe: float,
        wall_dist_pts: float,
        is_ce: bool,
        chosen_contract: Dict[str, Any],
        snap: Any,
        eff_delta: float,
        eff_gamma: float,
        qalpha: Any,
        horizon_steps: int,
        available_cash: float,
        today_live_pnl: float,
        policy: Any,
        flags: List[str],
    ) -> Tuple[Dict[str, float], int, float, float, float, float, float, float]:
        """Computes scale-free Spot SL/Target, Greek Taylor option levels, risk sizing, and aligned MC PoT."""
        is_spread = bool(chosen_contract.get("is_vertical_spread", False))
        sp_spec = chosen_contract.get("spread_spec") or {}
        entry_px = float(sp_spec["net_debit_pts"]) if is_spread else float(chosen_contract["entry_ask"])
        lot_cost = float(chosen_contract["lot_cost"])

        cb_room = max(0.0, policy.daily_loss_limit_inr + today_live_pnl)
        risk_cap_inr = min(policy.max_single_trade_risk_inr, cb_room if cb_room > 0 else policy.max_single_trade_risk_inr)

        # 1. Structural Spot SL anchored to EMA20 (and VWAP only when within 2 ATRs), clamped in [0.75, 2.0] ATR
        vwap_near = abs(spot - tech.vwap) <= (2.0 * atr_safe)
        if is_ce:
            struct_level = min(tech.ema_20 - 0.5 * atr_safe, tech.vwap if vwap_near else spot - atr_safe)
            raw_sl_dist = max(0.0, spot - struct_level)
        else:
            struct_level = max(tech.ema_20 + 0.5 * atr_safe, tech.vwap if vwap_near else spot + atr_safe)
            raw_sl_dist = max(0.0, struct_level - spot)

        spot_sl_dist = max(0.75 * atr_safe, min(2.0 * atr_safe, raw_sl_dist))

        # Couple Spot SL cleanly with max single-trade risk via effective Greek Delta
        safe_delta = max(0.15 if is_spread else 0.25, abs(eff_delta))
        budget_spot_dist = risk_cap_inr / max(1.0, safe_delta * snap.lot_size)
        if budget_spot_dist >= 0.65 * atr_safe:
            spot_sl_dist = min(spot_sl_dist, budget_spot_dist)

        # 2. Structural Spot Target trimmed inside nearby intact Option Wall
        if 1.0 * atr_safe <= wall_dist_pts <= 2.5 * atr_safe:
            spot_target_dist = max(1.25 * atr_safe, wall_dist_pts * 0.90)
        else:
            spot_target_dist = 2.0 * atr_safe

        suggested_spot_sl = round((spot - spot_sl_dist) if is_ce else (spot + spot_sl_dist), 2)
        suggested_spot_target = round((spot + spot_target_dist) if is_ce else max(0.05, spot - spot_target_dist), 2)

        # 3. Aligned GPU Monte Carlo PoT using exact barrier distances & live flow drift (0.0 when no depth)
        slip_pts = (
            max(0.05, entry_px - max(0.0, chosen_contract["bid"] - sp_spec.get("short_ask", 0.0)))
            if is_spread
            else entry_px * max(0.005, chosen_contract["spread_pct"] / 100.0)
        )
        spread_spot_drag = slip_pts / safe_delta
        if qalpha.source == "LIVE_L2_DEPTH" and abs(qalpha.z_ofi) >= 0.5:
            dir_z = qalpha.z_ofi if is_ce else -qalpha.z_ofi
            mc_drift = max(-0.15, min(0.35, dir_z * 0.08))
        else:
            mc_drift = 0.0

        mc = self.gpu.simulate_monte_carlo_po_t(
            spot=spot,
            annualized_vol=tech.realized_vol_annual,
            target_pts=round(spot_target_dist, 2),
            stop_pts=round(spot_sl_dist, 2),
            num_paths=5000,
            intraday_steps=horizon_steps,
            drift=mc_drift,
            friction_pts=spread_spot_drag,
            is_bullish=is_ce,
        )

        # 4. Option Target via Taylor Greek expansion + Risk-Bounded Catastrophic Stop (NSE ₹0.05 tick)
        tgt_delta_move = safe_delta * spot_target_dist + 0.5 * eff_gamma * (spot_target_dist ** 2)
        if is_spread:
            width_pts = float(sp_spec["width_pts"])
            suggested_tgt = round_nse_tick(min(width_pts, entry_px + max(0.25, tgt_delta_move)))
            max_loss_per_lot_pts = min(entry_px * 0.60, max(entry_px * 0.35, risk_cap_inr / max(1, snap.lot_size)))
            suggested_sl = round_nse_tick(max(0.05, entry_px - max_loss_per_lot_pts))
            risk_per_lot = max(1.0, (entry_px - suggested_sl) * snap.lot_size)
            max_deploy = min(available_cash, policy.max_spread_net_debit_inr)
            long_leg_cash = float(sp_spec["long_ask"]) * snap.lot_size
            long_cap_lots = int(available_cash / max(1.0, long_leg_cash))
            cap_lots = min(long_cap_lots, int(max_deploy / max(1.0, lot_cost)))
        else:
            suggested_tgt = round_nse_tick(entry_px + max(0.25, tgt_delta_move))
            raw_sl_pts = safe_delta * spot_sl_dist
            min_sl_pts = entry_px * 0.05
            max_sl_pts = min(entry_px * 0.22, risk_cap_inr / max(1, snap.lot_size))
            bounded_sl_pts = max(min_sl_pts, min(max_sl_pts, raw_sl_pts))
            suggested_sl = round_nse_tick(max(0.05, entry_px - bounded_sl_pts))
            risk_per_lot = max(1.0, (entry_px - suggested_sl) * snap.lot_size)
            cap_lots = int(min(available_cash, policy.max_position_deployment_inr) / max(1.0, lot_cost))

        target_risk_budget = min(policy.max_single_trade_risk_inr * 0.75, cb_room * 0.70) if cb_room > 0 else policy.max_single_trade_risk_inr * 0.75
        risk_lots = int(target_risk_budget / risk_per_lot) if risk_per_lot > 0 else 1
        suggested_lots = max(1, min(max(1, cap_lots), max(1, risk_lots)))
        suggested_ticket = round(suggested_lots * lot_cost, 2)
        suggested_max_risk = round(risk_per_lot * suggested_lots, 2)
        if suggested_max_risk > policy.max_single_trade_risk_inr:
            flags.append(f"WARN:RISK_₹{suggested_max_risk:.0f}>{policy.max_single_trade_risk_inr:.0f}")

        return (
            mc,
            suggested_lots,
            suggested_ticket,
            suggested_max_risk,
            suggested_sl,
            suggested_tgt,
            suggested_spot_sl,
            suggested_spot_target,
        )

    def build_briefing(
        self,
        max_candidates: int = 3,
        candidate_pool: Optional[List[str]] = None,
        save_json: bool = True,
    ) -> Dict[str, Any]:
        """Runs the heavy algo scan and returns a compact CIO cockpit briefing dict."""
        clock = evaluate_session_clock()
        desk_ctx = self.bridge.get_unified_desk_context()
        open_trades = self.ledger.get_open_trades()
        today_live_pnl = self.ledger.get_today_pnl(source_tag="LIVE_KITE")
        deployed = sum(float(t["entry_price"]) * int(t["quantity"]) for t in open_trades)
        policy = get_trading_policy()
        available_cash = max(0.0, policy.demat_budget_inr - deployed)

        if candidate_pool:
            available_universe = self.feed.get_available_universe(candidate_pool)
        else:
            full_fno = self.feed.get_full_fno_universe(include_indices=False)
            available_universe = full_fno if full_fno else self.feed.get_available_universe(CORE_FO_UNIVERSE)

        breadth_raw = self.feed.fetch_live_market_breadth(available_universe)
        market_lens = TechnicalLensEngine.compute_live_market_lens_breadth(
            breadth_raw, sector_map=OptionsAnalyticsEngine.SECTOR_MAP
        )
        generals = self._compute_generals_from_breadth(breadth_raw)

        # Prefer Desk SHM macro if fresh (<10s), else live Kite indices
        shm_macro = desk_ctx.get("shm_macro_quotes") or {}
        indices_view = dict(breadth_raw.get("indices") or {})
        for idx_name in ("NIFTY 50", "NIFTY BANK", "INDIA VIX"):
            shm_item = shm_macro.get(idx_name)
            if shm_item and not shm_item.get("stale") and shm_item.get("last_price", 0.0) > 0:
                pc = float(shm_item.get("prev_close") or shm_item["last_price"])
                chg = ((shm_item["last_price"] - pc) / pc * 100.0) if pc > 0 else 0.0
                indices_view[idx_name] = {
                    "ltp": shm_item["last_price"],
                    "chg_pct": round(chg, 2),
                    "source": "DESK_SHM",
                }

        active_positions_view = self._inspect_open_positions(open_trades, clock)

        # Stage 1 -> Stage 2 Algo Funnel: liquid active movers + index & 6-Generals structural anchors
        moves = breadth_raw.get("stock_moves") or []
        liquid_moves = [m for m in moves if float(m.get("turnover_cr", 0.0)) >= 20.0] or moves
        ranked_by_activity = sorted(
            liquid_moves,
            key=lambda x: abs(float(x.get("chg_pct", 0.0))) * math.log1p(max(1.0, float(x.get("turnover_cr", 1.0)))),
            reverse=True,
        )
        scan_order = [m["symbol"] for m in ranked_by_activity[:10]]
        anchor_list = ("NIFTY", "BANKNIFTY", *[s for s, _ in GENERALS_WEIGHTS])
        for anchor_sym in anchor_list:
            if anchor_sym not in scan_order and (anchor_sym == "NIFTY" or anchor_sym in available_universe):
                scan_order.append(anchor_sym)
                if len(scan_order) >= 15:
                    break

        blocked_desk = set(desk_ctx.get("collision_blocked_underlyings") or [])
        fno_ban = set(desk_ctx.get("nse_premarket", {}).get("fno_ban_list") or [])
        open_underlyings = {t["underlying"] for t in open_trades}

        veto_counts: Dict[str, int] = {}
        vetted_candidates: List[Dict[str, Any]] = []

        def _bump(reason: str) -> None:
            veto_counts[reason] = veto_counts.get(reason, 0) + 1

        horizon_steps = max(2, min(75, clock["minutes_to_1515_eod"] // 5)) if clock["entries_permitted"] else 3

        for sym in scan_order:
            if sym in open_underlyings:
                _bump("ALREADY_OPEN")
                continue
            if sym in blocked_desk:
                _bump("DESK_COLLISION_MUTEX")
                continue
            if sym in fno_ban:
                _bump("NSE_FNO_BAN")
                continue

            meta = self.feed.get_symbol_meta(sym) or {}
            # SEBI T-4 Physical Delivery Margin Blackout on non-index single-stock options
            if (
                policy.physical_delivery_t4_blackout
                and sym.upper() not in CASH_SETTLED_INDICES
                and "dte_days" in meta
                and int(meta["dte_days"]) <= 4
            ):
                _bump("PHYSICAL_DELIVERY_T4_BLACKOUT")
                continue

            snap = self.feed.fetch_spot_and_option_chain(sym, num_strikes=6)
            if snap is None or not snap.chain or snap.spot_price <= 0:
                _bump("NO_LIVE_FEED")
                continue

            spot = snap.spot_price
            dte_days = max(0.15, float(meta.get("dte_days", 5.0)))
            tech = TechnicalLensEngine.analyze_technicals(
                sym, spot, snap.price_history, snap.volume_history, snap.vwap_live,
                require_real_history=True, high_history=snap.high_history, low_history=snap.low_history
            )
            rv_safe = max(0.10, tech.realized_vol_annual)
            opt = OptionsAnalyticsEngine.analyze(spot, snap.chain, symbol=sym)
            gex = GreeksGexEngine.evaluate_frontier_landscape(
                sym, spot, snap.chain, dte=dte_days, iv=rv_safe, lot_size=snap.lot_size, strike_step=snap.strike_step,
            )

            flags: List[str] = []
            if tech.trend_regime == "BULLISH_TREND_ALIGNMENT":
                is_ce = True
                flags.append("TRIPLE_RIBBON" if tech.is_triple_ribbon_bull else "VWAP+9>40")
            elif tech.trend_regime == "BEARISH_TREND_ALIGNMENT":
                is_ce = False
                flags.append("TRIPLE_RIBBON" if tech.is_triple_ribbon_bear else "VWAP+9<40")
            elif tech.is_above_vwap and tech.ema_9 > tech.ema_20:
                is_ce = True
                flags.append("EARLY_9>20_PRE_40EMA")
            elif (not tech.is_above_vwap) and tech.ema_9 < tech.ema_20:
                is_ce = False
                flags.append("EARLY_9<20_PRE_40EMA")
            else:
                day_chg_tmp = (spot - snap.prev_close) if snap.prev_close > 0 else 0.0
                is_ce = (spot >= tech.vwap) if abs(spot - tech.vwap) > 0 else (day_chg_tmp >= 0)
                flags.append("CHOP_TANGLED")

            if tech.is_overextended:
                flags.append("WARN:VWAP_STRETCH")

            opt_tag = "CE" if is_ce else "PE"
            direction = "BUY_CE" if is_ce else "BUY_PE"

            if gex.gex_regime in ("LONG_GAMMA_PIN", "LONG_GAMMA_PINNING"):
                flags.append("WARN:LONG_GAMMA_PIN")
            elif gex.gex_regime in ("SHORT_GAMMA_VOL", "SHORT_GAMMA_VOLATILITY"):
                flags.append("GEX:SQUEEZE")

            # Scale-free Wall OI Dynamics (>= 3% wall OI shift and >= 2 lots)
            atr_safe = max(tech.atr_5m, spot * 0.0012)
            wall_px = opt.call_wall_strike if is_ce else opt.put_wall_strike
            wall_dist_pts = ((wall_px - spot) if is_ce else (spot - wall_px)) if wall_px > 0 else atr_safe * 5.0
            wall_dist_atr = round(wall_dist_pts / atr_safe, 2)

            wall_obj = next((s for s in snap.chain if abs(s.strike - wall_px) < 0.01), None)
            wall_oi_chg = (wall_obj.call_change_oi if is_ce else wall_obj.put_change_oi) if wall_obj else 0
            wall_oi_total = (opt.call_wall_oi if is_ce else opt.put_wall_oi) or 0
            sig_oi_thresh = max(snap.lot_size * 2, int(wall_oi_total * 0.03))
            if wall_oi_chg < -sig_oi_thresh:
                wall_status = f"Covering {wall_oi_chg:+,.0f} OI"
                if is_ce:
                    flags.append("SQUEEZE:CALL_COVERING")
            elif wall_oi_chg > sig_oi_thresh:
                wall_status = f"Reinforcing {wall_oi_chg:+,.0f} OI"
                if 0 < wall_dist_atr < 1.2:
                    flags.append("WARN:WALL_DEFENDED")
            else:
                wall_status = "Static"

            if wall_px > 0 and 0 < wall_dist_atr < 1.2 and "WARN:WALL_DEFENDED" not in flags:
                flags.append(f"WARN:WALL_{wall_dist_atr}ATR")

            if opt.pcr_divergence == "BULLISH_DIVERGENCE" and is_ce:
                flags.append("SMART:BULL_PCR_DIV")
            elif opt.pcr_divergence == "BEARISH_DIVERGENCE" and not is_ce:
                flags.append("SMART:BEAR_PCR_DIV")

            # Contract structure selection: ATM naked -> ATM/OTM spread (eliminates ₹20k flip-flop) -> OTM naked
            atm_obj = min(snap.chain, key=lambda s: abs(s.strike - spot))
            otm_strike = atm_obj.strike + (snap.strike_step if is_ce else -snap.strike_step)
            otm_obj = next((s for s in snap.chain if abs(s.strike - otm_strike) < 0.01), None)

            chosen_contract = self._build_naked_spec(
                atm_obj, snap, is_ce, opt_tag, available_cash, policy.tier_1_super_setup_budget_inr, flags,
            )
            if chosen_contract is None and policy.vertical_spreads_enabled and atm_obj:
                chosen_contract = self._build_vertical_spread_spec(
                    atm_obj, snap, is_ce, opt_tag, available_cash, policy, flags,
                )
            if chosen_contract is None and otm_obj is not None:
                chosen_contract = self._build_naked_spec(
                    otm_obj, snap, is_ce, opt_tag, available_cash, policy.tier_1_super_setup_budget_inr, flags,
                )

            if chosen_contract is None:
                _bump("SPREAD_OR_BUDGET_REJECT")
                continue

            # Live Spot 5-level Order Book Microstructure + True Single or Net-Spread Greeks
            atm_oi_call = atm_obj.call_oi
            atm_oi_put = atm_obj.put_oi
            is_spread = bool(chosen_contract.get("is_vertical_spread", False))
            if is_spread and "spread_spec" in chosen_contract:
                sp_spec = chosen_contract["spread_spec"]
                greeks_map = self.gpu.compute_vectorized_greeks(
                    spots=[spot, spot],
                    strikes=[sp_spec["long_strike"], sp_spec["short_strike"]],
                    dte_days=[dte_days, dte_days],
                    ivs=[rv_safe, rv_safe],
                    is_calls=[is_ce, is_ce],
                )
                opt_delta = round(float(greeks_map["delta"][0]) - float(greeks_map["delta"][1]), 2)
                opt_theta = round(float(greeks_map["theta"][0]) - float(greeks_map["theta"][1]), 2)
                eff_gamma = max(0.0, float(greeks_map["gamma"][0]) - float(greeks_map["gamma"][1]))
            else:
                greeks_map = self.gpu.compute_vectorized_greeks(
                    spots=[spot],
                    strikes=[chosen_contract["strike"]],
                    dte_days=[dte_days],
                    ivs=[rv_safe],
                    is_calls=[is_ce],
                )
                opt_delta = round(float(greeks_map["delta"][0]), 2)
                opt_theta = round(float(greeks_map["theta"][0]), 2)
                eff_gamma = max(0.0, float(greeks_map["gamma"][0]))

            qalpha = QuantitativeAlphaEngine.evaluate_machine_alpha(
                symbol=sym,
                spot_price=spot,
                strike_step=snap.strike_step,
                lot_size=snap.lot_size,
                call_oi_atm=atm_oi_call,
                put_oi_atm=atm_oi_put,
                net_oi_velocity_15m=opt.near_atm_oi_velocity,
                is_bullish_bias=is_ce,
                bid_depth=snap.spot_bid_depth,
                ask_depth=snap.spot_ask_depth,
                prev_bid_depth=snap.prev_spot_bid_depth,
                prev_ask_depth=snap.prev_spot_ask_depth,
                dte=dte_days,
                require_real_depth=True,
                iv=rv_safe,
            )

            if is_ce and qalpha.ask_replenish_warning:
                flags.append("WARN:ASK_ICEBERG")

            (
                mc,
                suggested_lots,
                suggested_ticket,
                suggested_max_risk,
                suggested_sl,
                suggested_tgt,
                suggested_spot_sl,
                suggested_spot_target,
            ) = self._compute_physics_levels_and_sizing(
                spot=spot,
                tech=tech,
                atr_safe=atr_safe,
                wall_dist_pts=wall_dist_pts,
                is_ce=is_ce,
                chosen_contract=chosen_contract,
                snap=snap,
                eff_delta=opt_delta,
                eff_gamma=eff_gamma,
                qalpha=qalpha,
                horizon_steps=horizon_steps,
                available_cash=available_cash,
                today_live_pnl=today_live_pnl,
                policy=policy,
                flags=flags,
            )

            qty_total = suggested_lots * snap.lot_size
            if is_spread and "spread_spec" in chosen_contract:
                sp_spec = chosen_contract["spread_spec"]
                est_fric = AIPaperLedger.calculate_vertical_spread_friction(
                    long_entry=sp_spec["long_ask"],
                    long_exit=sp_spec["long_ask"] + max(0.0, suggested_tgt - sp_spec["net_debit_pts"]),
                    short_entry=sp_spec["short_bid"],
                    short_exit=sp_spec["short_bid"],
                    quantity=qty_total,
                )
            else:
                est_fric = AIPaperLedger.calculate_indian_friction(
                    chosen_contract["entry_ask"], suggested_tgt, qty_total, direction,
                )
            day_chg_pct = round(((spot - snap.prev_close) / snap.prev_close * 100.0), 2) if snap.prev_close > 0 else 0.0

            # Smart Algo Edge: compute machine-level signals beyond retail Nuvama/Sensibull
            chain_dicts = [
                {"strike": s.strike, "call_oi": s.call_oi, "put_oi": s.put_oi,
                 "call_ltp": s.call_ltp, "put_ltp": s.put_ltp,
                 "call_change_oi": s.call_change_oi, "put_change_oi": s.put_change_oi}
                for s in snap.chain
            ]
            call_wall_obj = next((s for s in snap.chain if abs(s.strike - opt.call_wall_strike) < 0.01), None)
            put_wall_obj = next((s for s in snap.chain if abs(s.strike - opt.put_wall_strike) < 0.01), None)
            opt_dict = {
                "call_wall_strike": opt.call_wall_strike, "call_wall_oi": opt.call_wall_oi,
                "call_wall_oi_change": call_wall_obj.call_change_oi if call_wall_obj else 0,
                "put_wall_strike": opt.put_wall_strike, "put_wall_oi": opt.put_wall_oi,
                "put_wall_oi_change": put_wall_obj.put_change_oi if put_wall_obj else 0,
                "atm_straddle_premium": opt.atm_straddle_premium,
            }
            tech_dict = {
                "atr_5m": tech.atr_5m, "realized_vol_annual": tech.realized_vol_annual,
                "ema_20_series": tech.ema_20_series or TechnicalLensEngine.calculate_ema_series(snap.price_history, 20),
            }
            quant_dict = {"z_ofi": qalpha.z_ofi, "kobp": qalpha.kobp}

            # Relative Volume: ONLY use real equity spot volume (indices with spot_volume=0 stay 1.0x NORMAL, never option volume/1!)
            today_spot_vol = snap.spot_volume_today if snap.spot_volume_today > 0 else 0
            vol_hist = snap.volume_history or []
            bars_per_day = 75
            if today_spot_vol <= 0:
                avg_vol_20d = 0
            elif len(vol_hist) > bars_per_day:
                yesterday_vols = vol_hist[: len(vol_hist) - bars_per_day]
                y_sum = sum(yesterday_vols)
                avg_vol_20d = y_sum if y_sum > 0 else today_spot_vol
            else:
                breadth_move = next((m for m in moves if m["symbol"] == sym), None)
                b_vol = int(breadth_move["volume"]) if breadth_move and breadth_move.get("volume") else 0
                avg_vol_20d = b_vol if b_vol > 0 else today_spot_vol

            smart = evaluate_smart_edge(
                symbol=sym, spot=spot, chain_data=chain_dicts,
                strike_step=snap.strike_step, tech_indicators=tech_dict,
                quant_alpha=quant_dict, opt_analytics=opt_dict,
                mc_result=mc, is_ce=is_ce,
                current_volume=today_spot_vol,
                avg_volume_20d=avg_vol_20d,
                dte_days=dte_days,
            )

            # Algo Edge-Enhanced Ranking: replaces simple flag-based scoring
            # Base technical regime score
            tech_base = 40.0 if "TRIPLE_RIBBON" in flags else (30.0 if "VWAP+" in " ".join(flags) else (22.0 if "EARLY_" in " ".join(flags) else 10.0))
            # Smart Edge contribution (up to 35 points from algo computations)
            edge_contrib = max(-15.0, min(35.0, (smart.composite_edge_score - 50.0) * 0.70))
            rank_score = (
                tech_base
                + edge_contrib
                + min(15.0, max(0.0, wall_dist_atr * 3.0))
                + (8.0 if (is_ce and generals["status"] == "GREEN_LIGHT") or (not is_ce and generals["status"] == "RED_LIGHT") else 0.0)
                - (8.0 if any(f.startswith("WARN:") for f in flags) else 0.0)
            )

            vetted_candidates.append({
                "symbol": sym,
                "direction": direction,
                "day_chg_pct": day_chg_pct,
                "rank_score": round(rank_score, 1),
                "flags": flags,
                "spot": round(spot, 2),
                "vwap": tech.vwap,
                "z_vwap": tech.z_vwap,
                "ribbon_9_20_40": f"{tech.ema_9:.1f}/{tech.ema_20:.1f}/{tech.ema_40:.1f}",
                "triple_ribbon": tech.is_triple_ribbon_bull or tech.is_triple_ribbon_bear,
                "atr_5m": tech.atr_5m,
                "realized_vol_pct": round(tech.realized_vol_annual * 100.0, 1),
                "pcr": opt.pcr,
                "max_pain": opt.max_pain_strike,
                "straddle_be": f"{opt.expected_breakeven_low:.0f}-{opt.expected_breakeven_high:.0f}",
                "opt_delta": opt_delta,
                "opt_theta": opt_theta,
                "oi_velocity": opt.near_atm_oi_velocity,
                "wall_target": wall_px,
                "wall_dist_atr": wall_dist_atr,
                "wall_status": wall_status,
                "gex_regime": gex.gex_regime,
                "gex_flip_strike": gex.gex_flip_strike,
                "gex_regime_short": "SHORT_VOL" if "SHORT" in gex.gex_regime else ("PINNING" if "LONG" in gex.gex_regime else "NEUTRAL"),
                "l2_kobp": qalpha.kobp,
                "l2_z_ofi": qalpha.z_ofi,
                "l2_source": qalpha.source,
                "mc_pot_pct": round(mc["p_win_target"] * 100.0, 1),
                "mc_edge_pct": mc["mathematical_edge_pct"],
                "contract_spec": chosen_contract,
                "est_roundtrip_friction": est_fric,
                "suggested_lots": suggested_lots,
                "suggested_ticket": suggested_ticket,
                "suggested_max_risk": suggested_max_risk,
                "suggested_sl": suggested_sl,
                "suggested_target": suggested_tgt,
                "suggested_spot_sl": suggested_spot_sl,
                "suggested_spot_target": suggested_spot_target,
                # Smart Algo Edge (beyond retail)
                "algo_edge_score": smart.composite_edge_score,
                "algo_edge_verdict": smart.edge_verdict,
                "algo_edge_factors": smart.edge_factors[:3],  # Top 3 factors for display
                "iv_skew_regime": smart.iv_skew.skew_regime,
                "iv_skew_val": smart.iv_skew.put_call_iv_skew,
                "straddle_pct": smart.straddle_premium_pct,
                "straddle_regime": smart.straddle_regime,
                "oi_concentration": smart.oi_concentration_regime,
                "call_wall_fragility": smart.call_wall_fragility.status,
                "put_wall_fragility": smart.put_wall_fragility.status,
                "volume_regime": smart.volume_regime,
                "accel_regime": smart.acceleration_regime,
            })

        vetted_candidates.sort(key=lambda c: c["rank_score"], reverse=True)
        logic_rep = load_cached_logic_report()
        regime_bench = {}
        if logic_rep:
            key_reg = "EXPIRY_DAY" if clock["expiry_regime"] == "EXPIRY_GAMMA_DAY" else "NORMAL_DAY"
            regime_bench = {
                "stability": logic_rep.get("anti_curve_fit_verdict"),
                "current_regime_stats": (logic_rep.get("causal_by_regime") or {}).get(key_reg, {}),
                "high_vol_stats": (logic_rep.get("causal_by_regime") or {}).get("HIGH_VOL_TREND", {}),
            }

        nse_pm = desk_ctx.get("nse_premarket") or {}
        part = nse_pm.get("participant_positioning") or {}

        briefing = {
            "clock": clock,
            "capital": {
                "budget_inr": policy.demat_budget_inr,
                "deployed_inr": round(deployed, 2),
                "available_cash_inr": round(available_cash, 2),
                "today_live_realized_pnl": today_live_pnl,
                "circuit_breaker_remaining": round(policy.daily_loss_limit_inr + today_live_pnl, 2),
            },
            "macro": {
                "indices": indices_view,
                "generals_6_score": generals["weighted_score"],
                "generals_status": generals["status"],
                "market_lens_breadth": f"{market_lens.advances}A/{market_lens.declines}D ({market_lens.ad_ratio}x {market_lens.market_breadth_sentiment})",
                "breadth_universe_count": len(available_universe),
                "breadth_source": market_lens.source,
                "top_sectors": market_lens.top_turnover_sectors,
                "nse_fii_futures": part.get("fii_net_futures"),
                "nse_fii_bias": part.get("fii_directional_bias"),
                "nse_premarket_source": nse_pm.get("premarket_source"),
                "nse_premarket_fresh": nse_pm.get("is_today_fresh", False),
                "nse_premarket_delivery": nse_pm.get("raw_premarket", {}),
                "market_lens_integration": {
                    "status": "NOT_CONNECTED_TERMS_RESTRICTED",
                    "reason": "NSE Market Lens terms prohibit automated collection and use for virtual trading or simulation.",
                    "cio_use": "Do not treat Market Lens as a machine feed; use compliant live broker data and NSE pre-open evidence instead.",
                },
            },
            "desk_connector": {
                "status": desk_ctx["desk_status"],
                "blocked_symbols": desk_ctx["collision_blocked_underlyings"],
                "macro_regime": desk_ctx.get("desk_macro_regime") or {},
                "recent_signals": desk_ctx["recent_desk_signals"][-3:],
            },
            "logic_test_context": regime_bench,
            "active_positions": active_positions_view,
            "screened_count": len(scan_order),
            "universe_scanned_stage1": len(available_universe),
            "veto_summary": veto_counts,
            "top_candidates": vetted_candidates[:max_candidates],
        }

        if save_json:
            try:
                BRIEFING_PATH.parent.mkdir(parents=True, exist_ok=True)
                BRIEFING_PATH.write_text(json.dumps(briefing, indent=2), encoding="utf-8")
            except Exception:
                pass
        return briefing


def _fmt_inr(val: Optional[float]) -> str:
    if val is None:
        return "N/A"
    return f"-₹{abs(val):,.2f}" if val < 0 else f"₹{val:,.2f}"


def format_briefing_text(b: Dict[str, Any]) -> str:
    """Formats the lightweight ~35-line CIO Cockpit view for instant Antigravity inspection."""
    clk = b["clock"]
    cap = b["capital"]
    mac = b["macro"]
    dsk = b["desk_connector"]
    dmr = dsk.get("macro_regime") or {}
    gift = dmr.get("gift_nifty") or {}
    jrnl = dmr.get("latest_journal") or {}
    pm_delivery = mac.get("nse_premarket_delivery") or {}

    lines = [
        "=" * 90,
        f" ANTIGRAVITY AI CIO COCKPIT | {clk['time_ist']} | {clk['phase']} ({clk['minutes_to_1515_eod']}m to EOD [{clk.get('eod_cutoff', '15:15')}])",
        f" Expiry: {clk['expiry_regime']} | Entries Permitted: {'YES' if clk['entries_permitted'] else 'NO (MANAGE ONLY)'}",
        "=" * 90,
        f" BUDGET : Cash Left ₹{cap['available_cash_inr']:,.0f} / ₹50,000 | Today Live PnL: {_fmt_inr(cap['today_live_realized_pnl'])} | CB Room: ₹{cap['circuit_breaker_remaining']:,.0f}",
        f" MACRO  : Breadth ({mac.get('breadth_universe_count', 0)} F&O): {mac['market_lens_breadth']} [{mac['breadth_source']}] | 6-Gen {mac['generals_status']} ({mac['generals_6_score']:+.2f})",
        f" SECTORS: {', '.join(mac.get('top_sectors') or ['N/A'])} | FII Net Fut: {mac['nse_fii_futures']} ({mac['nse_fii_bias']}) | GIFT: {gift.get('gap_type', 'N/A')} ({gift.get('gap_pct', 0.0):+.2f}%)",
        f" PREOPEN: {pm_delivery.get('status', 'MISSING')} | Source: {pm_delivery.get('source', 'UNAVAILABLE')} | Fresh: {'YES' if mac.get('nse_premarket_fresh') else 'NO'} | Symbols: {pm_delivery.get('symbol_count', 0)}",
        f" DESK   : {dsk['status']} | Mutex: {dsk['blocked_symbols'] or 'NONE'} | Regime: {dmr.get('http_regime') or dmr.get('frontier_regime') or 'N/A'} | Frontier: {dmr.get('frontier_status', 'UNAVAILABLE')} (Age {dmr.get('frontier_age_min')}m) | Nifty Fut OFI: {dmr.get('nifty_fut_ofi_z')}z",
    ]
    if jrnl.get("reason"):
        lines.append(
            f" JOURNAL: [{jrnl.get('underlying')}] FutOFI {jrnl.get('fut_ofi_z'):+.2f}z | AggImb {jrnl.get('aggressor_imb_z'):+.2f}z | {jrnl['reason']}"
        )
    idx = mac.get("indices") or {}
    if idx:
        idx_str = " | ".join(f"{k}: {v.get('ltp')} ({v.get('chg_pct'):+.2f}%)" for k, v in idx.items() if v.get("ltp"))
        if idx_str:
            lines.append(f" INDICES: {idx_str}")

    ltc = b.get("logic_test_context") or {}
    if ltc.get("current_regime_stats"):
        rs = ltc["current_regime_stats"]
        hv = ltc.get("high_vol_stats") or {}
        lines.append(
            f" ALGO-TEST: Verdict={ltc.get('stability')} | Regime PF={rs.get('profit_factor')} E(R)={rs.get('expectancy_r')} | HighVol PF={hv.get('profit_factor')} E(R)={hv.get('expectancy_r')}"
        )

    lines.append("-" * 90)
    act = b.get("active_positions") or []
    lines.append(f" ACTIVE OPEN POSITIONS ({len(act)}):")
    if not act:
        lines.append("   • 100% Cash (0 Open Positions).")
    for p in act:
        c_label = f"{p['contract']} / {p['short_contract']}" if p.get("short_contract") else p["contract"]
        p_spot_sl = f" | Spot SL ₹{p['spot_sl']}" if p.get("spot_sl") else ""
        lines.append(
            f"   • #{p['trade_id']} {c_label} ({p['lots']}L={p['quantity']}) | Entry ₹{p['entry']} -> Mark ₹{p['live_mark']} "
            f"| Net {_fmt_inr(p['est_net_pnl'])} ({p['ret_pct']}%) | SL ₹{p['sl']}{p_spot_sl} | {p['elapsed_min']}m"
        )
        lines.append(
            f"     Spot ₹{p['spot']} (VWAP ₹{p['vwap']}, EMA20 ₹{p['ema_20']}) | WallDist {p['wall_dist_atr']}ATR | L2 KOBP {p['opt_kobp']} -> HINT: {p['cio_hint']}"
        )

    lines.append("-" * 90)
    cands = b.get("top_candidates") or []
    lines.append(
        f" HELPING-HAND CANDIDATES ({len(cands)} of {b['screened_count']} Stage-2 | {b.get('universe_scanned_stage1', 0)} Stage-1 | Hard Vetoes: {b['veto_summary']}):"
    )
    if not cands:
        lines.append("   • STAND ASIDE — No affordable/liquid contract fits current budget & spread rules.")
    for i, c in enumerate(cands, 1):
        cs = c["contract_spec"]
        lots = c.get("suggested_lots", 1)
        tkt = c.get("suggested_ticket", cs["lot_cost"])
        m_risk = c.get("suggested_max_risk", 0.0)
        flags_str = " ".join(f"[{f}]" for f in (c.get("flags") or []))
        if cs.get("is_vertical_spread") and "spread_spec" in cs:
            sp = cs["spread_spec"]
            contract_desc = f"Spread ({sp['long_contract']} / {sp['short_contract']}) @ Net ₹{sp['net_debit_pts']:.2f} (R:R {sp['reward_to_risk']:.2f}, Max ₹{sp['max_profit_inr']:.0f})"
        else:
            contract_desc = f"{cs['contract']} @ Ask ₹{cs['entry_ask']} (Spread {cs['spread_pct']}%)"
        spot_sl_str = f" | Spot SL ₹{c['suggested_spot_sl']}" if "suggested_spot_sl" in c else ""
        lines.append(
            f"   [{i}] {c['direction']} {c['symbol']} ({c.get('day_chg_pct', 0.0):+.2f}%) -> {contract_desc} (Fric ~₹{c['est_roundtrip_friction']:.0f}) {flags_str}"
        )
        lines.append(
            f"       Tech: Score {c['rank_score']} | Spot ₹{c['spot']} (Z_VWAP {c['z_vwap']:+.2f}, EMAs {c['ribbon_9_20_40']}, ATR ₹{c['atr_5m']})"
        )
        lines.append(
            f"       Smart Nuvama/Sensibull: PCR {c['pcr']} (Vel {c['oi_velocity']:+,.0f}) | Pain ₹{c['max_pain']:.0f} | Wall ₹{c['wall_target']:.0f} ({c['wall_dist_atr']} ATR, {c.get('wall_status', 'Static')}) | GEX: {c.get('gex_regime_short', 'N/A')} (Flip ₹{c.get('gex_flip_strike', 0):.0f}) | Δ {c['opt_delta']} θ {c['opt_theta']} | BE [{c['straddle_be']}]"
        )
        lines.append(
            f"       Desk L2 & Sizing: KOBP {c['l2_kobp']:+.2f} Z_OFI {c['l2_z_ofi']:+.2f} [{c['l2_source']}] | MC PoT {c['mc_pot_pct']}% | {lots}L ({lots*cs['lot_size']}Q) Tkt ₹{tkt:,.0f} Risk ₹{m_risk:,.0f} SL ₹{c['suggested_sl']}{spot_sl_str}"
        )
        # Smart Algo Edge line — the information edge retail cannot see
        edge_factors_str = " | ".join(c.get("algo_edge_factors", [])[:2]) or "—"
        lines.append(
            f"       Algo Edge: {c.get('algo_edge_score', 50):.0f}/100 ({c.get('algo_edge_verdict', 'N/A')}) | IV Skew: {c.get('iv_skew_regime', 'N/A')} ({c.get('iv_skew_val', 0):+.3f}) | Straddle: {c.get('straddle_pct', 0):.2f}% ({c.get('straddle_regime', 'N/A')}) | OI: {c.get('oi_concentration', 'N/A')} | {edge_factors_str}"
        )
    lines.append("=" * 90)
    return "\n".join(lines)
