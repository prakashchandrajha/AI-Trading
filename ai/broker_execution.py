"""
Spring Boot-style Dual-Mode Execution Broker Architecture for AI-Trading.
Provides a unified abstraction for order execution and position management:
- Mode "PAPER": Simulated fills at genuine real-time market marks, tracked in SQLite ledger.
- Mode "LIVE" : Real exchange orders submitted via Zerodha KiteConnect NFO API with dual-journaling.
Toggled seamlessly via strategy.yaml: external_cio_authority.target_venue = "PAPER" | "LIVE".
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

from live_kite_feed import LiveKiteFeed
from paper_ledger import AIPaperLedger
from trading_policy import TradingPolicy, get_trading_policy


def round_nse_tick(price: float, tick: float = 0.05) -> float:
    """Rounds a price to the nearest NSE NFO ₹0.05 option tick."""
    if price <= 0:
        return 0.05
    return max(0.05, round(round(price / tick) * tick, 2))


def _spread_net_mark_from_quotes(
    long_q: Optional[Dict[str, Any]],
    short_q: Optional[Dict[str, Any]],
    fallback_net: float,
    fallback_short: float,
) -> Tuple[float, float]:
    """Computes live spread liquidation value (long_bid - short_ask) and short exit mark."""
    long_bid = float((long_q or {}).get("bid") or 0.0)
    long_ltp = float((long_q or {}).get("ltp") or 0.0)
    long_exit = long_bid if long_bid > 0 else (float((long_q or {}).get("mark") or 0.0) or long_ltp)

    short_ask = float((short_q or {}).get("ask") or 0.0)
    short_ltp = float((short_q or {}).get("ltp") or 0.0)
    short_exit = short_ask if short_ask > 0 else (float((short_q or {}).get("mark") or 0.0) or short_ltp)

    if long_exit > 0 and short_exit > 0:
        return max(0.05, round(long_exit - short_exit, 2)), round(short_exit, 2)
    return max(0.05, round(fallback_net, 2)), round(fallback_short, 2)


class BaseExecutionBroker(ABC):
    """Abstract Base Execution Broker (Spring Boot Service Interface equivalent)."""

    @abstractmethod
    def get_mode(self) -> str:
        """Returns the execution mode ('PAPER' or 'LIVE')."""
        raise NotImplementedError

    @abstractmethod
    def execute_entry(
        self,
        symbol: str,
        direction: str,
        contract: str,
        lots: int,
        lot_size: int,
        exec_px: float,
        sl: float,
        target: float,
        thesis: str,
        clock: Dict[str, Any],
        max_risk: float,
        ticket: float,
        spot_stop_loss: Optional[float] = None,
        short_contract: Optional[str] = None,
        short_exec_px: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Executes trade entry (1-leg naked or 2-leg vertical debit spread) and logs decision."""
        raise NotImplementedError

    @abstractmethod
    def execute_exit(
        self,
        trade: Dict[str, Any],
        mark: float,
        action: str,
        reason: str,
        regime: str,
        exit_lots: Optional[int] = None,
        new_sl: Optional[float] = None,
        short_mark: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Manages active position: exit, book profit, partial book, or trail SL."""
        raise NotImplementedError

    @abstractmethod
    def square_off_all(
        self,
        open_trades: List[Dict[str, Any]],
        live_quotes: Dict[str, Any],
        reason: str,
    ) -> Dict[str, Any]:
        """Executes mandatory EOD square-off across all open positions."""
        raise NotImplementedError


class PaperExecutionBroker(BaseExecutionBroker):
    """
    Paper Trading Broker Implementation.
    Fills at genuine live market quotes, tracking PnL and statutory taxes in SQLite.
    """

    def __init__(self, ledger: Optional[AIPaperLedger] = None, feed: Optional[LiveKiteFeed] = None) -> None:
        self.ledger = ledger or AIPaperLedger()
        self.feed = feed or LiveKiteFeed()

    def get_mode(self) -> str:
        return "PAPER"

    def execute_entry(
        self,
        symbol: str,
        direction: str,
        contract: str,
        lots: int,
        lot_size: int,
        exec_px: float,
        sl: float,
        target: float,
        thesis: str,
        clock: Dict[str, Any],
        max_risk: float,
        ticket: float,
        spot_stop_loss: Optional[float] = None,
        short_contract: Optional[str] = None,
        short_exec_px: Optional[float] = None,
    ) -> Dict[str, Any]:
        qty = lots * lot_size
        short_entry = round(float(short_exec_px), 2) if (short_contract and short_exec_px is not None) else None
        tid = self.ledger.open_trade(
            underlying=symbol,
            symbol=contract,
            direction=direction,
            entry_price=round(exec_px, 2),
            stop_loss=round(sl, 2),
            target=round(target, 2),
            lots=lots,
            lot_size=lot_size,
            thesis=thesis,
            source_tag="LIVE_KITE",
            spot_stop_loss=spot_stop_loss,
            short_contract=short_contract,
            short_entry_price=short_entry,
        )
        self.ledger.log_cio_decision(
            action="ENTER",
            trade_id=tid,
            underlying=symbol,
            contract=contract,
            regime=clock.get("expiry_regime", "NORMAL_DAY"),
            reasoning=thesis,
            context={
                "venue": "PAPER",
                "entry": round(exec_px, 2),
                "sl": round(sl, 2),
                "target": round(target, 2),
                "spot_stop_loss": spot_stop_loss,
                "short_contract": short_contract,
                "short_entry_price": short_entry,
                "lots": lots,
                "qty": qty,
                "risk_inr": round(max_risk, 2),
            },
        )
        out: Dict[str, Any] = {
            "status": "OPENED_TRADE",
            "venue": "PAPER",
            "trade_id": tid,
            "contract": contract,
            "entry_price": round(exec_px, 2),
            "sl": round(sl, 2),
            "target": round(target, 2),
            "lots": lots,
            "quantity": qty,
            "ticket_inr": round(ticket, 2),
            "max_risk_inr": round(max_risk, 2),
        }
        if short_contract:
            out["short_contract"] = short_contract
            out["short_entry_price"] = short_entry
        if spot_stop_loss is not None:
            out["spot_stop_loss"] = spot_stop_loss
        return out

    def execute_exit(
        self,
        trade: Dict[str, Any],
        mark: float,
        action: str,
        reason: str,
        regime: str,
        exit_lots: Optional[int] = None,
        new_sl: Optional[float] = None,
        short_mark: Optional[float] = None,
    ) -> Dict[str, Any]:
        trade_id = int(trade["trade_id"])
        contract = trade["symbol"]
        act = action.upper()

        if act == "TRAIL_SL":
            if new_sl is None or new_sl <= float(trade["stop_loss"]):
                return {"status": "REJECTED", "reason": f"INVALID_TRAIL_SL:{new_sl} <= {trade['stop_loss']}"}
            ok = self.ledger.update_stop_loss(trade_id, round(float(new_sl), 2))
            if ok:
                self.ledger.log_cio_decision(
                    action="TRAIL_SL", trade_id=trade_id, underlying=trade["underlying"],
                    contract=contract, regime=regime,
                    reasoning=f"Trailed SL from ₹{trade['stop_loss']} -> ₹{new_sl:.2f}: {reason}",
                    context={"old_sl": trade["stop_loss"], "new_sl": new_sl, "mark": mark, "venue": "PAPER"},
                )
                return {"status": "SL_UPDATED", "trade_id": trade_id, "new_sl": round(float(new_sl), 2), "new_stop_loss": round(float(new_sl), 2), "mark": mark}
            return {"status": "ERROR", "reason": "TRAIL_UPDATE_FAILED"}

        if act == "PARTIAL_BOOK":
            book_lots = int(exit_lots or max(1, int(trade["lots"]) // 2))
            res = self.ledger.partial_close_trade(
                trade_id,
                book_lots,
                mark,
                exit_reason=f"CIO_PARTIAL_BOOK:{reason[:40]}",
                short_exit_price=short_mark,
            )
            if res and new_sl is not None and new_sl > float(trade["stop_loss"]):
                self.ledger.update_stop_loss(trade_id, round(float(new_sl), 2))
                res["runner_new_sl"] = round(float(new_sl), 2)
            if res:
                self.ledger.log_cio_decision(
                    action="PARTIAL_BOOK", trade_id=trade_id, underlying=trade["underlying"],
                    contract=contract, regime=regime, reasoning=reason, context={**res, "venue": "PAPER"},
                )
            return {"status": "PARTIAL_BOOKED", "venue": "PAPER", **res} if res else {"status": "ERROR", "reason": "PARTIAL_BOOK_FAILED"}

        if act in ("BOOK_PROFIT", "EXIT"):
            reason_tag = reason if reason.startswith(("GUARDIAN_", "HARD_EOD_")) else f"CIO_{act}"
            res = self.ledger.close_trade(trade_id, mark, exit_reason=reason_tag, short_exit_price=short_mark)
            if res:
                self.ledger.log_cio_decision(
                    action=act, trade_id=trade_id, underlying=trade["underlying"],
                    contract=contract, regime=regime, reasoning=reason, context={**res, "venue": "PAPER"},
                )
            return {"status": "CLOSED", "venue": "PAPER", **res} if res else {"status": "ERROR", "reason": "CLOSE_FAILED"}

        return {"status": "ERROR", "reason": f"UNSUPPORTED_ACTION:{act}"}

    def square_off_all(
        self,
        open_trades: List[Dict[str, Any]],
        live_quotes: Dict[str, Any],
        reason: str,
    ) -> Dict[str, Any]:
        closed_count = 0
        total_realized_net = 0.0
        total_friction = 0.0

        for t in open_trades:
            tid = t["trade_id"]
            sym = t["symbol"]
            entry_p = float(t["entry_price"])
            short_sym = t.get("short_contract")
            short_entry = float(t.get("short_entry_price") or 0.0)

            if short_sym:
                mark, short_exit = _spread_net_mark_from_quotes(
                    live_quotes.get(sym),
                    live_quotes.get(short_sym),
                    fallback_net=entry_p,
                    fallback_short=short_entry,
                )
                res = self.ledger.close_trade(tid, mark, reason, short_exit_price=short_exit)
            else:
                q = live_quotes.get(sym)
                if q and float(q.get("mark", 0.0)) > 0:
                    mark = float(q["mark"])
                else:
                    print(f" ⚠️ [PAPER RESILIENCE] Live quote missing for #{tid} ({sym}); squaring off at entry mark ₹{entry_p:.2f}.")
                    mark = entry_p
                res = self.ledger.close_trade(tid, mark, reason)

            total_friction += res.get("friction", 0.0)
            total_realized_net += res.get("net_pnl", 0.0)
            closed_count += 1

        return {
            "status": "ALL_SQUARED_OFF",
            "venue": "PAPER",
            "closed_count": closed_count,
            "failed_count": 0,
            "total_realized_net": total_realized_net,
            "total_friction": total_friction,
        }


class LiveKiteExecutionBroker(BaseExecutionBroker):
    """
    Live Real-Money Execution Broker via Zerodha KiteConnect NFO API.
    - Uses SEBI hedge-first sequencing on 2-leg vertical spreads (BUY long -> SELL short on entry;
      BUY short -> SELL long on exit) so ₹50k capital receives full hedge margin benefit.
    - Places Marketable Limit orders with 2% protection rounded to ₹0.05 tick instead of raw MARKET orders.
    - Enforces anti-short safety lock and never marks SQLite CLOSED if exchange order submission fails.
    """

    def __init__(
        self,
        ledger: Optional[AIPaperLedger] = None,
        feed: Optional[LiveKiteFeed] = None,
        policy: Optional[TradingPolicy] = None,
    ) -> None:
        self.ledger = ledger or AIPaperLedger()
        self.feed = feed or LiveKiteFeed()
        self.policy = policy or get_trading_policy()

    def get_mode(self) -> str:
        return "LIVE"

    def _get_kite(self):
        kite = self.feed._ensure_kite()
        if not kite:
            raise RuntimeError(f"Live KiteConnect initialization failed: {self.feed._kite_init_error or 'No active session'}")
        return kite

    def _resolve_product(self, kite: Any) -> Any:
        pref = getattr(self.policy, "exchange_product", "NRML").upper()
        if pref == "NRML":
            return getattr(kite, "PRODUCT_NRML", getattr(kite, "PRODUCT_MIS", "NRML"))
        return getattr(kite, "PRODUCT_MIS", "MIS")

    def _place_protected_order(
        self,
        kite: Any,
        tradingsymbol: str,
        trans_type: str,
        quantity: int,
        ref_price: float = 0.0,
        slippage_pct: float = 0.02,
    ) -> Tuple[str, Optional[float]]:
        """
        Places a Marketable Limit order (rounded to ₹0.05 NSE tick) when ref_price is known,
        falling back to MARKET only when ref_price <= 0. Confirms fill price if order_history is available.
        """
        product = self._resolve_product(kite)
        kwargs: Dict[str, Any] = {
            "variety": kite.VARIETY_REGULAR,
            "exchange": kite.EXCHANGE_NFO,
            "tradingsymbol": tradingsymbol,
            "transaction_type": trans_type,
            "quantity": int(quantity),
            "product": product,
        }
        if ref_price > 0 and hasattr(kite, "ORDER_TYPE_LIMIT"):
            if trans_type == "BUY":
                limit_px = round_nse_tick(ref_price * (1.0 + slippage_pct))
            else:
                limit_px = round_nse_tick(max(0.05, ref_price * (1.0 - slippage_pct)))
            kwargs["order_type"] = kite.ORDER_TYPE_LIMIT
            kwargs["price"] = limit_px
        else:
            kwargs["order_type"] = kite.ORDER_TYPE_MARKET

        order_id = str(kite.place_order(**kwargs))

        fill_px: Optional[float] = round(ref_price, 2) if ref_price > 0 else None
        if hasattr(kite, "order_history"):
            for _ in range(3):
                try:
                    hist = kite.order_history(order_id) or []
                    latest = hist[-1] if hist else {}
                    status = str(latest.get("status") or "").upper()
                    avg_p = float(latest.get("average_price") or 0.0)
                    if status == "COMPLETE" and avg_p > 0:
                        fill_px = round(avg_p, 2)
                        break
                    if status in ("REJECTED", "CANCELLED"):
                        reason = latest.get("status_message") or status
                        raise RuntimeError(f"Exchange order {order_id} {status}: {reason}")
                except RuntimeError:
                    raise
                except Exception:
                    pass
                time.sleep(0.15)
        return order_id, fill_px

    @staticmethod
    def _clamp_live_exit_qty(kite: Any, contract: str, desired_qty: int, expected_side: str = "LONG") -> int:
        """Prevents accidental reverse short/long positions by checking live net holdings when available."""
        if not hasattr(kite, "positions"):
            return desired_qty
        try:
            pos_payload = kite.positions() or {}
            net_rows = pos_payload.get("net") or []
            for row in net_rows:
                if str(row.get("tradingsymbol") or "").upper() == contract.upper():
                    q = int(row.get("quantity") or 0)
                    if expected_side == "LONG":
                        return max(0, min(desired_qty, q)) if q > 0 else 0
                    return max(0, min(desired_qty, abs(q))) if q < 0 else 0
        except Exception:
            pass
        return desired_qty

    def execute_entry(
        self,
        symbol: str,
        direction: str,
        contract: str,
        lots: int,
        lot_size: int,
        exec_px: float,
        sl: float,
        target: float,
        thesis: str,
        clock: Dict[str, Any],
        max_risk: float,
        ticket: float,
        spot_stop_loss: Optional[float] = None,
        short_contract: Optional[str] = None,
        short_exec_px: Optional[float] = None,
    ) -> Dict[str, Any]:
        kite = self._get_kite()
        qty = lots * lot_size
        trans_type = "BUY" if direction.startswith("BUY") else "SELL"

        if short_contract:
            short_ref = float(short_exec_px or 0.0)
            long_ref = round(exec_px + short_ref, 2) if short_ref > 0 else exec_px
            # Step 1: BUY protective Long leg FIRST so SEBI grants hedge margin on Step 2
            try:
                long_oid, filled_long = self._place_protected_order(
                    kite, contract, "BUY", qty, ref_price=long_ref,
                )
            except Exception as exc:
                return {"status": "LIVE_ORDER_REJECTED", "venue": "LIVE", "error": str(exc)}

            long_px = filled_long or long_ref

            # Step 2: SELL hedge OTM Short leg SECOND
            try:
                short_oid, filled_short = self._place_protected_order(
                    kite, short_contract, "SELL", qty, ref_price=short_ref,
                )
            except Exception as exc:
                # Immediately try protective unwind of Long leg so ₹50k capital is never left over-deployed naked
                try:
                    unwind_oid, _ = self._place_protected_order(
                        kite, contract, "SELL", qty, ref_price=long_px,
                    )
                    return {
                        "status": "LIVE_SPREAD_LEG2_REJECTED_UNWOUND",
                        "venue": "LIVE",
                        "long_order_id": long_oid,
                        "unwind_order_id": unwind_oid,
                        "error": str(exc),
                    }
                except Exception as unwind_exc:
                    # Unwind also failed: persist filled Long leg cleanly into SQLite so Guardian stops it immediately
                    safe_sl = max(0.05, round(long_px - (max_risk / max(1, qty)), 2))
                    tid = self.ledger.open_trade(
                        underlying=symbol,
                        symbol=contract,
                        direction=direction,
                        entry_price=round(long_px, 2),
                        stop_loss=safe_sl,
                        target=round(long_px * 1.25, 2),
                        lots=lots,
                        lot_size=lot_size,
                        thesis=f"[SHORT_LEG_FAILED_NAKED_GUARD] {thesis}",
                        source_tag=f"LIVE_EXCHANGE:{long_oid}:SHORT_FAIL",
                        spot_stop_loss=spot_stop_loss,
                    )
                    return {
                        "status": "OPENED_PARTIAL_NAKED_GUARDED",
                        "venue": "LIVE",
                        "trade_id": tid,
                        "long_order_id": long_oid,
                        "error": f"Short leg failed ({exc}); unwind failed ({unwind_exc})",
                    }

            short_px = filled_short or short_ref
            net_entry = round(max(0.05, long_px - short_px), 2)
            order_tag = f"{long_oid}/{short_oid}"
        else:
            try:
                order_tag, filled_px = self._place_protected_order(
                    kite, contract, trans_type, qty, ref_price=exec_px,
                )
            except Exception as exc:
                return {"status": "LIVE_ORDER_REJECTED", "venue": "LIVE", "error": str(exc)}
            net_entry = round(filled_px or exec_px, 2)
            short_px = None

        tid = self.ledger.open_trade(
            underlying=symbol,
            symbol=contract,
            direction=direction,
            entry_price=net_entry,
            stop_loss=round(sl, 2),
            target=round(target, 2),
            lots=lots,
            lot_size=lot_size,
            thesis=thesis,
            source_tag=f"LIVE_EXCHANGE:{order_tag}",
            spot_stop_loss=spot_stop_loss,
            short_contract=short_contract,
            short_entry_price=round(short_px, 2) if short_px is not None else None,
        )
        self.ledger.log_cio_decision(
            action="ENTER",
            trade_id=tid,
            underlying=symbol,
            contract=contract,
            regime=clock.get("expiry_regime", "NORMAL_DAY"),
            reasoning=thesis,
            context={
                "venue": "LIVE",
                "exchange_order_id": order_tag,
                "entry": net_entry,
                "sl": round(sl, 2),
                "target": round(target, 2),
                "spot_stop_loss": spot_stop_loss,
                "short_contract": short_contract,
                "short_entry_price": short_px,
                "lots": lots,
                "qty": qty,
                "risk_inr": round(max_risk, 2),
            },
        )
        out: Dict[str, Any] = {
            "status": "OPENED_TRADE",
            "venue": "LIVE",
            "exchange_order_id": order_tag,
            "trade_id": tid,
            "contract": contract,
            "entry_price": net_entry,
            "sl": round(sl, 2),
            "target": round(target, 2),
            "lots": lots,
            "quantity": qty,
            "ticket_inr": round(net_entry * qty, 2),
            "max_risk_inr": round(max_risk, 2),
        }
        if short_contract:
            out["short_contract"] = short_contract
            out["short_entry_price"] = round(short_px, 2) if short_px is not None else None
        if spot_stop_loss is not None:
            out["spot_stop_loss"] = spot_stop_loss
        return out

    def _exit_live_position(
        self,
        kite: Any,
        trade: Dict[str, Any],
        exit_qty: int,
        net_mark: float,
        short_mark: Optional[float],
        is_partial: bool,
    ) -> Tuple[Optional[str], float, Optional[float], Optional[str]]:
        """
        Executes single-leg or spread exit on Kite using anti-short lock and reverse hedge sequencing:
        For spreads: BUY short leg FIRST -> SELL long leg SECOND.
        Returns (order_tag, actual_net_mark, actual_short_mark, error_reason).
        """
        trade_id = int(trade["trade_id"])
        contract = trade["symbol"]
        short_sym = trade.get("short_contract")

        if short_sym:
            short_ref = float(short_mark if short_mark is not None else (trade.get("short_entry_price") or 0.0))
            long_ref = round(net_mark + short_ref, 2) if short_ref > 0 else net_mark

            short_qty = self._clamp_live_exit_qty(kite, short_sym, exit_qty, expected_side="SHORT")
            long_qty = self._clamp_live_exit_qty(kite, contract, exit_qty, expected_side="LONG")
            if short_qty <= 0 and long_qty <= 0:
                return None, net_mark, short_mark, "ANTI_SHORT_LOCK:ZERO_LIVE_POSITION"

            short_oid = "SKIPPED_ZERO"
            actual_short_ex = short_ref
            if short_qty > 0:
                try:
                    short_oid, filled_s = self._place_protected_order(
                        kite, short_sym, "BUY", short_qty, ref_price=short_ref,
                    )
                    if filled_s is not None:
                        actual_short_ex = filled_s
                except Exception as exc:
                    return None, net_mark, short_mark, str(exc)

            long_oid = "SKIPPED_ZERO"
            actual_long_ex = long_ref
            if long_qty > 0:
                try:
                    long_oid, filled_l = self._place_protected_order(
                        kite, contract, "SELL", long_qty, ref_price=long_ref,
                    )
                    if filled_l is not None:
                        actual_long_ex = filled_l
                except Exception as exc:
                    # Short leg already closed cleanly! Update full trade to reflect remaining naked Long leg if not partial
                    if not is_partial and long_qty == int(trade["quantity"]):
                        old_short_in = float(trade.get("short_entry_price") or 0.0)
                        long_in = round(float(trade["entry_price"]) + old_short_in, 2)
                        spread_stop_dist = max(0.05, float(trade["entry_price"]) - float(trade["stop_loss"]))
                        self.ledger.detach_short_leg(
                            trade_id,
                            new_entry_price=long_in,
                            new_stop_loss=round(max(0.05, long_in - spread_stop_dist), 2),
                        )
                    return None, net_mark, actual_short_ex, f"LONG_LEG_EXIT_FAILED_AFTER_SHORT_CLOSE:{exc}"

            actual_net = max(0.05, round(actual_long_ex - actual_short_ex, 2))
            return f"{short_oid}/{long_oid}", actual_net, round(actual_short_ex, 2), None

        # Single-leg long exit
        clamped_qty = self._clamp_live_exit_qty(kite, contract, exit_qty, expected_side="LONG")
        if clamped_qty <= 0:
            return None, net_mark, None, "ANTI_SHORT_LOCK:ZERO_LIVE_LONG_POSITION"
        try:
            oid, filled_px = self._place_protected_order(kite, contract, "SELL", clamped_qty, ref_price=net_mark)
        except Exception as exc:
            return None, net_mark, None, str(exc)
        return oid, round(filled_px or net_mark, 2), None, None

    def execute_exit(
        self,
        trade: Dict[str, Any],
        mark: float,
        action: str,
        reason: str,
        regime: str,
        exit_lots: Optional[int] = None,
        new_sl: Optional[float] = None,
        short_mark: Optional[float] = None,
    ) -> Dict[str, Any]:
        kite = self._get_kite()
        trade_id = int(trade["trade_id"])
        contract = trade["symbol"]
        act = action.upper()

        if act == "TRAIL_SL":
            if new_sl is None or new_sl <= float(trade["stop_loss"]):
                return {"status": "REJECTED", "reason": f"INVALID_TRAIL_SL:{new_sl} <= {trade['stop_loss']}"}
            ok = self.ledger.update_stop_loss(trade_id, round(float(new_sl), 2))
            if ok:
                self.ledger.log_cio_decision(
                    action="TRAIL_SL", trade_id=trade_id, underlying=trade["underlying"],
                    contract=contract, regime=regime,
                    reasoning=f"Trailed SL from ₹{trade['stop_loss']} -> ₹{new_sl:.2f}: {reason}",
                    context={"old_sl": trade["stop_loss"], "new_sl": new_sl, "mark": mark, "venue": "LIVE"},
                )
                return {"status": "SL_UPDATED", "venue": "LIVE", "trade_id": trade_id, "new_sl": round(float(new_sl), 2), "new_stop_loss": round(float(new_sl), 2), "mark": mark}
            return {"status": "ERROR", "reason": "TRAIL_UPDATE_FAILED"}

        if act in ("BOOK_PROFIT", "EXIT"):
            qty = int(trade["quantity"])
            order_id, fill_net, fill_short, err = self._exit_live_position(
                kite, trade, qty, mark, short_mark, is_partial=False,
            )
            if err:
                return {"status": "LIVE_EXIT_REJECTED", "venue": "LIVE", "error": err}

            reason_prefix = reason if reason.startswith(("GUARDIAN_", "HARD_EOD_")) else "LIVE_EXCHANGE_EXIT"
            res = self.ledger.close_trade(
                trade_id, fill_net, exit_reason=f"{reason_prefix}:{order_id}", short_exit_price=fill_short,
            )
            if res:
                self.ledger.log_cio_decision(
                    action=act, trade_id=trade_id, underlying=trade["underlying"],
                    contract=contract, regime=regime, reasoning=reason,
                    context={**res, "venue": "LIVE", "exchange_order_id": order_id},
                )
            return {"status": "CLOSED", "venue": "LIVE", "exchange_order_id": order_id, **res} if res else {"status": "ERROR", "reason": "CLOSE_FAILED"}

        if act == "PARTIAL_BOOK":
            open_lots = int(trade["lots"])
            book_lots = int(exit_lots or max(1, open_lots // 2))
            if book_lots >= open_lots:
                return self.execute_exit(trade, mark, "EXIT", reason, regime, exit_lots=None, new_sl=new_sl, short_mark=short_mark)

            lot_size = max(1, int(trade["quantity"]) // max(1, open_lots))
            exit_qty = book_lots * lot_size
            order_id, fill_net, fill_short, err = self._exit_live_position(
                kite, trade, exit_qty, mark, short_mark, is_partial=True,
            )
            if err:
                return {"status": "LIVE_PARTIAL_REJECTED", "venue": "LIVE", "error": err}

            res = self.ledger.partial_close_trade(
                trade_id, book_lots, fill_net,
                exit_reason=f"LIVE_EXCHANGE_PARTIAL:{order_id}",
                short_exit_price=fill_short,
            )
            if res and new_sl is not None and new_sl > float(trade["stop_loss"]):
                self.ledger.update_stop_loss(trade_id, round(float(new_sl), 2))
                res["runner_new_sl"] = round(float(new_sl), 2)
            if res:
                self.ledger.log_cio_decision(
                    action="PARTIAL_BOOK", trade_id=trade_id, underlying=trade["underlying"],
                    contract=contract, regime=regime, reasoning=reason,
                    context={**res, "venue": "LIVE", "exchange_order_id": order_id},
                )
            return {"status": "PARTIAL_BOOKED", "venue": "LIVE", "exchange_order_id": order_id, **res} if res else {"status": "ERROR", "reason": "PARTIAL_BOOK_FAILED"}

        return {"status": "ERROR", "reason": f"UNSUPPORTED_ACTION:{act}"}

    def square_off_all(
        self,
        open_trades: List[Dict[str, Any]],
        live_quotes: Dict[str, Any],
        reason: str,
    ) -> Dict[str, Any]:
        kite = self._get_kite()
        closed_count = 0
        failed_count = 0
        total_realized_net = 0.0
        total_friction = 0.0

        for t in open_trades:
            tid = t["trade_id"]
            sym = t["symbol"]
            short_sym = t.get("short_contract")
            qty = int(t["quantity"])
            entry_p = float(t["entry_price"])
            short_entry = float(t.get("short_entry_price") or 0.0)

            if short_sym:
                net_mark, short_mark = _spread_net_mark_from_quotes(
                    live_quotes.get(sym),
                    live_quotes.get(short_sym),
                    fallback_net=entry_p,
                    fallback_short=short_entry,
                )
            else:
                q = live_quotes.get(sym)
                net_mark = float(q.get("mark", 0.0)) if (q and float(q.get("mark", 0.0)) > 0) else entry_p
                short_mark = None

            order_id, fill_net, fill_short, err = self._exit_live_position(
                kite, t, qty, net_mark, short_mark, is_partial=False,
            )
            if err:
                # CRITICAL FAIL-CLOSED: Do NOT close SQLite row when exchange square-off fails!
                print(f"🛑 [LIVE EOD ERROR] Could not square off #{tid} ({sym}): {err}. Keeping OPEN for retry.")
                failed_count += 1
                continue

            res = self.ledger.close_trade(tid, fill_net, f"{reason}:{order_id}", short_exit_price=fill_short)
            total_friction += res.get("friction", 0.0)
            total_realized_net += res.get("net_pnl", 0.0)
            closed_count += 1

        return {
            "status": "ALL_SQUARED_OFF" if failed_count == 0 else "PARTIAL_SQUAREOFF_RETRY_NEEDED",
            "venue": "LIVE",
            "closed_count": closed_count,
            "failed_count": failed_count,
            "total_realized_net": total_realized_net,
            "total_friction": total_friction,
        }


def get_execution_broker(
    policy: Optional[TradingPolicy] = None,
    ledger: Optional[AIPaperLedger] = None,
    feed: Optional[LiveKiteFeed] = None,
    force_paper: bool = False,
) -> BaseExecutionBroker:
    """
    Factory creating the configured execution broker (Spring Boot @Profile equivalent).
    Reads policy.target_venue ('PAPER' or 'LIVE'). Always returns PAPER if force_paper=True.
    """
    if force_paper:
        return PaperExecutionBroker(ledger=ledger, feed=feed)
    active_policy = policy or get_trading_policy()
    target_venue = getattr(active_policy, "target_venue", "PAPER").upper()

    if target_venue == "LIVE":
        return LiveKiteExecutionBroker(ledger=ledger, feed=feed, policy=active_policy)
    return PaperExecutionBroker(ledger=ledger, feed=feed)
