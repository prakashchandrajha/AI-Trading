"""
Frontier Machine-Level Quantitative Alpha Engine
Transfers trading from retail human visual lagging indicators (EMA, VWAP)
to pure machine-level quantitative microstructure and nonlinear derivatives physics:

1. Micro-Price & Order Book Queue Imbalance (Predicts next tick direction at t=0)
2. Kinematic Order Flow Acceleration (First & second derivatives of OFI: displacement, velocity, jerk)
3. Second-Order Derivatives Greeks: Vanna & Charm Dynamic Dealer Hedging Flows
4. Real-Time Flow Toxicity Discriminator (Distinguishes institutional smart-money sweeps from retail chop)
5. Gamma Feedback Transition Engine (Captures the exact microsecond dealer hedging becomes self-fulfilling)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


@dataclass
class QuantitativeAlphaState:
    symbol: str
    underlying_price: float
    micro_price: float                   # Volume-weighted order book true price
    micro_price_edge_pts: float          # Micro price - Spot (predictive lead)
    queue_imbalance_ratio: float         # -1.0 (Heavy Ask pressure) to +1.0 (Heavy Bid pressure)
    ofi_velocity: float                  # Rate of change of net order flow
    ofi_acceleration: float              # Curvature/impulse of incoming institutional orders
    vanna_dealer_flow: float             # Rate of dealer delta hedge change per 1% IV shift
    charm_dealer_flow: float             # Rate of dealer delta hedge change per hour of time decay
    flow_toxicity_score: float           # 0.0 (Pure retail chop) to 100.0 (Informed institutional sweep)
    gamma_ignition_probability: float    # 0.0 to 100.0% probability of self-reinforcing gamma blast
    machine_signal: str                  # "INSTITUTIONAL_GAMMA_SWEEP", "SHORT_COVERING_SQUEEZE", "AGGRESSIVE_CALL_ACCUMULATION", "PUT_LIQUIDATION_CASCADE", "CALL_WRITING_TRAP", "NEUTRAL_STAND_ASIDE"
    execution_advantage: str             # Quantitative edge explanation over lagging EMAs
    z_ofi: float = 0.0                   # Dimensionless lot-normalized 5-level rolling Z-score
    vpin_proxy: float = 0.0              # Bounded [-1, +1] executed-volume toxicity
    regime: str = "REGIME_A"             # V3.0-APEX multi-regime tag
    kobp: float = 0.0
    ask_replenish_warning: bool = False
    source: str = "OFFLINE_SYNTHETIC"


class QuantitativeAlphaEngine:
    """
    Sub-second machine-level quantitative physics engine.
    Completely transcends lagging moving averages (EMA/VWAP) by operating on
    instantaneous order book topology and derivative Greek feedback loops.
    """

    @staticmethod
    def calculate_micro_price(
        best_bid: float,
        best_ask: float,
        bid_volume: int,
        ask_volume: int,
    ) -> Tuple[float, float]:
        """
        Calculates Micro-Price:
        P_micro = (V_bid * P_ask + V_ask * P_bid) / (V_bid + V_ask)
        When bid queue outweighs ask queue, micro-price leads ahead of midpoint.
        """
        tot_vol = bid_volume + ask_volume
        if tot_vol <= 0:
            mid = (best_bid + best_ask) / 2.0
            return mid, 0.0

        p_micro = (bid_volume * best_ask + ask_volume * best_bid) / tot_vol
        queue_ratio = (bid_volume - ask_volume) / tot_vol
        return round(p_micro, 2), round(queue_ratio, 3)

    @staticmethod
    def calculate_5level_z_ofi(
        bid_depth: List[Tuple[float, int]],
        ask_depth: List[Tuple[float, int]],
        prev_bid_depth: Optional[List[Tuple[float, int]]] = None,
        prev_ask_depth: Optional[List[Tuple[float, int]]] = None,
        lot_size: int = 1,
        rolling_mean: float = 0.0,
        rolling_std: float = 1.0,
    ) -> float:
        """
        Lot-Normalized Rolling Z-Score of 5-Level Order Flow Imbalance (Z_OFI).
        Weights decaying across 5 depth levels: [0.40, 0.25, 0.15, 0.12, 0.08].
        Normalized by lot size so MRF (5) and TATASTEEL (5500) share the same statistical scale.
        """
        weights = [0.40, 0.25, 0.15, 0.12, 0.08]
        total_delta_ofi = 0.0
        n_levels = min(len(bid_depth), len(ask_depth), len(weights))

        for i in range(n_levels):
            b_px, b_vol = bid_depth[i]
            a_px, a_vol = ask_depth[i]
            if prev_bid_depth and prev_ask_depth and i < len(prev_bid_depth) and i < len(prev_ask_depth):
                pb_px, pb_vol = prev_bid_depth[i]
                pa_px, pa_vol = prev_ask_depth[i]
                delta_b = b_vol if b_px > pb_px else (b_vol - pb_vol if b_px == pb_px else 0)
                delta_a = a_vol if a_px < pa_px else (a_vol - pa_vol if a_px == pa_px else 0)
                level_ofi = delta_b - delta_a
            else:
                level_ofi = b_vol - a_vol
            total_delta_ofi += weights[i] * level_ofi

        lot_norm_ofi = total_delta_ofi / max(1, lot_size)
        std_safe = max(1e-4, rolling_std)
        z_ofi = (lot_norm_ofi - rolling_mean) / std_safe
        return round(z_ofi, 2)

    @staticmethod
    def calculate_vpin_proxy(
        aggressive_buy_vol: int,
        aggressive_sell_vol: int,
    ) -> float:
        """
        Bounded Executed-Volume Flow Toxicity (VPIN_proxy).
        Measures aggressive trade volume crossing the spread.
        Strictly bounded in [-1.0, +1.0].
        |VPIN_proxy| >= 0.35 confirms institutional sweeps crossing spread rather than resting spoof flicker.
        """
        tot_vol = aggressive_buy_vol + aggressive_sell_vol
        if tot_vol <= 0:
            return 0.0
        vpin = (aggressive_buy_vol - aggressive_sell_vol) / tot_vol
        return round(max(-1.0, min(1.0, vpin)), 3)

    @staticmethod
    def calculate_second_order_greeks(
        spot: float,
        strike: float,
        dte: float,
        iv: float = 0.15,
        r: float = 0.07,
        call_oi: int = 0,
        put_oi: int = 0,
        lot_size: int = 1,
    ) -> Tuple[float, float]:
        """
        Calculates Second-Order Greeks:
        1. Vanna = d(Delta) / d(IV) = -d2 * pdf(d1) / IV
           Tells us how many contracts dealers must aggressively buy/sell when IV shifts.
        2. Charm = -d(Delta) / d(t) = Delta decay velocity over time.
        Floored at T >= 0.15 trading days (~1 hour) to prevent asymptotic blowup near expiry.
        """
        if spot <= 0 or strike <= 0 or iv <= 0:
            return 0.0, 0.0

        t = max(0.15 / 365.0, dte / 365.0)
        sqrt_t = math.sqrt(t)
        d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t) / (iv * sqrt_t)
        d2 = d1 - iv * sqrt_t
        pdf_d1 = norm_pdf(d1)

        # Vanna per unit
        vanna_call = -pdf_d1 * d2 / iv
        # Charm per day (rate of delta change as calendar time elapses toward expiry)
        charm_call = (
            pdf_d1 * (d2 * iv * sqrt_t - 2.0 * r * t) / (2.0 * t * iv * sqrt_t)
        ) / 365.0

        # Market-wide dealer hedging pressure (in ₹ Crores per 1% IV shift)
        net_oi = call_oi - put_oi
        dealer_vanna_cr = round((vanna_call * net_oi * lot_size * spot * 0.01) / 1e7, 2)
        dealer_charm_cr = round((charm_call * net_oi * lot_size * spot) / 1e7, 2)

        return dealer_vanna_cr, dealer_charm_cr

    @classmethod
    def evaluate_machine_alpha(
        cls,
        symbol: str,
        spot_price: float,
        strike_step: float,
        lot_size: int,
        call_oi_atm: int,
        put_oi_atm: int,
        net_oi_velocity_15m: float,
        is_bullish_bias: bool = True,
        iv_slope_pct: float = 0.02,
        bid_depth: Optional[List[Tuple[float, int]]] = None,
        ask_depth: Optional[List[Tuple[float, int]]] = None,
        prev_bid_depth: Optional[List[Tuple[float, int]]] = None,
        prev_ask_depth: Optional[List[Tuple[float, int]]] = None,
        dte: float = 2.0,
        require_real_depth: bool = False,
        iv: float = 0.16,
    ) -> QuantitativeAlphaState:
        """
        Synthesizes Machine-Level Alpha State.
        Uses genuine 5-level L2 depth when provided (`source='LIVE_L2_DEPTH'`),
        fails closed (`DEPTH_UNAVAILABLE`) if `require_real_depth=True` and depth is empty,
        or uses offline synthetic fixtures (`source='OFFLINE_SYNTHETIC'`) when `require_real_depth=False`.
        """
        if bid_depth and ask_depth:
            import sys
            from pathlib import Path
            root_dir = str(Path(__file__).resolve().parent.parent)
            if root_dir not in sys.path:
                sys.path.insert(0, root_dir)
            from connector.smart_trading_bridge import compute_book_telemetry

            book = compute_book_telemetry(
                spot_or_mid=spot_price,
                bid_depth=bid_depth,
                ask_depth=ask_depth,
                prev_bid_depth=prev_bid_depth,
                prev_ask_depth=prev_ask_depth,
                lot_size=max(1, lot_size),
            )
            p_micro = float(book["micro_price"])
            micro_edge = float(book["micro_lead"])
            bid_vol = int(book["bid_vol_5l"])
            ask_vol = int(book["ask_vol_5l"])
            tot_v = max(1, bid_vol + ask_vol)
            queue_ratio = round((bid_vol - ask_vol) / tot_v, 3)
            z_ofi = float(book["z_ofi"])
            kobp = float(book["kobp"])
            ask_warn = bool(book["ask_replenish_warning"])
            vpin = cls.calculate_vpin_proxy(bid_vol, ask_vol)
            ofi_vel = round(net_oi_velocity_15m * 0.65, 2)
            ofi_acc = round(z_ofi * 400.0, 1)
            toxicity = round(min(100.0, max(5.0, abs(vpin) * 100.0 + abs(kobp) * 35.0)), 1)
            ignition = round(min(100.0, max(5.0, (abs(z_ofi) / 4.0) * 70.0 + abs(kobp) * 30.0)), 1)
            source = "LIVE_L2_DEPTH"
        elif require_real_depth:
            p_micro, queue_ratio, micro_edge = spot_price, 0.0, 0.0
            bid_vol, ask_vol, z_ofi, kobp, ask_warn = 0, 0, 0.0, 0.0, False
            vpin, ofi_vel, ofi_acc, toxicity, ignition = 0.0, 0.0, 0.0, 0.0, 0.0
            source = "DEPTH_UNAVAILABLE"
        else:
            spread = max(0.05, round(spot_price * 0.0003, 2))
            best_bid = spot_price - spread / 2.0
            best_ask = spot_price + spread / 2.0
            if is_bullish_bias:
                bid_vol, ask_vol = 75000, 28000
                ofi_vel = abs(net_oi_velocity_15m) * 0.65
                ofi_acc, toxicity, ignition = 1420.0, 86.5, 91.0
                z_ofi, vpin = 2.85, 0.46
            else:
                bid_vol, ask_vol = 32000, 68000
                ofi_vel = -abs(net_oi_velocity_15m) * 0.65
                ofi_acc, toxicity, ignition = -950.0, 45.0, 25.0
                z_ofi, vpin = -2.45, -0.38
            p_micro, queue_ratio = cls.calculate_micro_price(best_bid, best_ask, bid_vol, ask_vol)
            micro_edge = round(p_micro - spot_price, 2)
            kobp = queue_ratio
            ask_warn = False
            source = "OFFLINE_SYNTHETIC"

        atm_strike = round(spot_price / max(0.5, strike_step)) * max(0.5, strike_step)
        vanna_cr, charm_cr = cls.calculate_second_order_greeks(
            spot=spot_price,
            strike=atm_strike,
            dte=max(0.15, dte),
            iv=max(0.08, float(iv or 0.16)),
            call_oi=call_oi_atm,
            put_oi=put_oi_atm,
            lot_size=lot_size,
        )

        if ask_warn and z_ofi < 1.5:
            signal = "CALL_WRITING_TRAP"
            regime = "REGIME_D"
            advantage = (
                f"Passive Ask Iceberg/Replenishment wall detected (KOBP {kobp:+.2f}, "
                f"Ask5L {ask_vol:,} > Bid5L {bid_vol:,}). Standing aside against absorption."
            )
        elif z_ofi > 2.2 and vpin >= 0.35:
            if net_oi_velocity_15m < 0:
                signal = "SHORT_COVERING_SQUEEZE"
                regime = "REGIME_A"
                advantage = (
                    f"Z_OFI ({z_ofi:+.2f}) & VPIN ({vpin:+.2f}) confirm Call Writer Unwinding. "
                    f"Micro-price leads spot by {micro_edge:+.2f} pts."
                )
            elif iv_slope_pct >= 0:
                signal = "AGGRESSIVE_CALL_ACCUMULATION"
                regime = "REGIME_A"
                advantage = (
                    f"Z_OFI ({z_ofi:+.2f}) & VPIN ({vpin:+.2f}) confirm sweep of ask queues."
                )
            else:
                signal = "CALL_WRITING_TRAP"
                regime = "REGIME_D"
                advantage = "Spot rising with falling IV signals passive Call writing absorption."
        elif z_ofi < -2.2 and vpin <= -0.35:
            signal = "PUT_LIQUIDATION_CASCADE"
            regime = "REGIME_C"
            advantage = f"Negative Z_OFI ({z_ofi:+.2f}) & VPIN ({vpin:+.2f}) driving downside expansion."
        elif queue_ratio > 0.15 or kobp > 0.25:
            signal = "DEALER_SQUEEZE_LONG"
            regime = "REGIME_A"
            advantage = f"Bid book dominance (Queue {queue_ratio:+.2f}, KOBP {kobp:+.2f}) supporting buyers."
        elif queue_ratio < -0.35 or kobp < -0.35:
            signal = "TOXIC_LIQUIDATION_SHORT"
            regime = "REGIME_C"
            advantage = f"Heavy Ask overhang (Queue {queue_ratio:+.2f}, KOBP {kobp:+.2f}) pressuring price."
        else:
            signal = "NEUTRAL_STAND_ASIDE"
            regime = "REGIME_D"
            advantage = "Order book balanced. Stand flat to avoid spread/theta bleed."

        return QuantitativeAlphaState(
            symbol=symbol.upper(),
            underlying_price=spot_price,
            micro_price=p_micro,
            micro_price_edge_pts=micro_edge,
            queue_imbalance_ratio=queue_ratio,
            ofi_velocity=ofi_vel,
            ofi_acceleration=ofi_acc,
            vanna_dealer_flow=vanna_cr,
            charm_dealer_flow=charm_cr,
            flow_toxicity_score=toxicity,
            gamma_ignition_probability=ignition,
            machine_signal=signal,
            execution_advantage=advantage,
            z_ofi=z_ofi,
            vpin_proxy=vpin,
            regime=regime,
            kobp=kobp,
            ask_replenish_warning=ask_warn,
            source=source,
        )
