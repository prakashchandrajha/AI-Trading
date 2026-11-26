"""MCP helping hands for an external discretionary paper-trading CIO.

The connected IDE agent (Codex, Antigravity, or another approved agent) makes
trading decisions.  This server exposes market evidence, policy, gated CIO
actions, ledger state, and safety controls.  It never exposes an autonomous or
ungated order path.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional
from fastmcp import FastMCP

from agent_trader import AIAgentTrader
from connector.smart_trading_bridge import SmartTradingBridge
from greeks_gex import GreeksGexEngine
from live_kite_feed import LiveKiteFeed
from options_analytics import OptionsAnalyticsEngine
from paper_ledger import AIPaperLedger
from technical_lens import TechnicalLensEngine
from trading_policy import get_trading_policy

mcp = FastMCP("NSE_Smart_Trading")
ledger = AIPaperLedger()


def _live_option_snapshot(symbol: str):
    """Returns a complete live option snapshot, never a tape or synthetic fallback."""
    agent = AIAgentTrader(symbol=symbol, live_mode=True)
    snapshot = agent._fetch_live_snapshot()
    if snapshot is None or snapshot.spot_price <= 0 or not snapshot.chain:
        return agent, None
    return agent, snapshot


def _analyze_live_options(symbol: str):
    """Analyzes one symbol only when its live spot and option chain are complete."""
    _, snapshot = _live_option_snapshot(symbol)
    if snapshot is None:
        return None
    return OptionsAnalyticsEngine.analyze(snapshot.spot_price, snapshot.chain, symbol=symbol)


def _live_data_unavailable(symbol: str, reason: str) -> Dict:
    """Consistent fail-closed result for advisory MCP tools."""
    return {
        "status": "UNAVAILABLE",
        "source": "LIVE_KITE",
        "symbol": symbol.upper(),
        "reason": reason,
        "trade_recommendation": "STAND_ASIDE",
        "actionable_signal": "STAND_ASIDE",
        "confidence": 0.0,
        "conviction_pct": 0,
    }


@mcp.tool()
def analyze_options(symbol: str = "NIFTY", tape_db: Optional[str] = None) -> Dict:
    """
    Analyzes Open Interest (OI) metrics for an index or stock option chain.
    Returns Max Pain strike, Put-Call Ratio (PCR), Call Wall (Resistance),
    Put Wall (Support), 15-minute OI Velocity, and AI Trade Recommendation.
    Supported symbols: NIFTY, BANKNIFTY, MIDCPNIFTY, HDFCBANK, RELIANCE, SBIN, TATAMOTORS, INFY.
    """
    # `tape_db` is retained only for MCP compatibility; a trading-session call
    # always uses a fresh Kite snapshot and never falls back to tape/fixtures.
    _ = tape_db
    res = _analyze_live_options(symbol)
    if res is None:
        return _live_data_unavailable(symbol, "NO_LIVE_OPTION_CHAIN")
    return {
        "status": "LIVE",
        "source": "LIVE_KITE",
        "symbol": symbol.upper(),
        "spot_price": res.underlying_price,
        "max_pain": res.max_pain_strike,
        "pcr": res.pcr,
        "pcr_divergence": res.pcr_divergence,
        "put_wall_support": res.put_wall_strike,
        "call_wall_resistance": res.call_wall_strike,
        "atm_straddle_premium": res.atm_straddle_premium,
        "expected_breakeven_low": res.expected_breakeven_low,
        "expected_breakeven_high": res.expected_breakeven_high,
        "sentiment": res.sentiment,
        "trade_recommendation": res.trade_recommendation,
        "confidence": res.confidence,
        "structural_stop": res.structural_stop_index,
        "structural_target": res.structural_target_index,
        "thesis": res.analysis_thesis,
    }


@mcp.tool()
def get_gex_and_greeks(symbol: str = "NIFTY", dte: float = 2.0) -> Dict:
    """
    Calculates Black-Scholes Greeks and Net Market Maker Gamma Exposure (GEX).
    Identifies whether the market is in 'LONG_GAMMA_PIN' (stabilizing/mean-reverting)
    or 'SHORT_GAMMA_VOLATILITY' (explosive momentum/gamma squeeze) regime.
    """
    _, snapshot = _live_option_snapshot(symbol)
    if snapshot is None:
        return _live_data_unavailable(symbol, "NO_LIVE_OPTION_CHAIN")
    res = GreeksGexEngine.evaluate_frontier_landscape(
        symbol=symbol,
        underlying_price=snapshot.spot_price,
        chain=snapshot.chain,
        dte=dte,
    )
    return {
        "status": "LIVE",
        "source": "LIVE_KITE",
        "symbol": symbol.upper(),
        "spot_price": res.underlying_price,
        "net_gex_crores": res.net_gex_crores,
        "gex_regime": res.gex_regime,
        "volatility_flip_level": res.gex_flip_strike,
        "gamma_squeeze_score": res.gamma_squeeze_score,
        "strategy": res.execution_strategy,
        "actionable_signal": res.actionable_signal,
        "conviction_pct": res.conviction_pct,
        "thesis": res.frontier_thesis,
    }


@mcp.tool()
def check_generals_alignment() -> Dict:
    """
    Checks institutional Open Interest flow on 'The 6 Generals' Composite:
    HDFCBANK (9.95%), ICICIBANK (9.39%), RELIANCE (7.30%), BHARTIARTL (5.63%),
    LT (4.25%), INFY (3.92%) = 40.44% of NIFTY 50.
    Returns GREEN_LIGHT, RED_LIGHT, or CAUTION with full constituent breakdown.
    """
    generals_specs = [
        ("HDFCBANK", 0.0995),
        ("ICICIBANK", 0.0939),
        ("RELIANCE", 0.0730),
        ("BHARTIARTL", 0.0563),
        ("LT", 0.0425),
        ("INFY", 0.0392),
    ]

    total_weight = sum(w for _, w in generals_specs)
    weighted_score = 0.0
    covered_weight = 0.0
    constituents = {}
    unavailable_symbols: List[str] = []

    for sym, wt in generals_specs:
        res = _analyze_live_options(sym)
        if res is None:
            unavailable_symbols.append(sym)
            constituents[sym] = {
                "weight_pct": round(wt * 100, 2),
                "status": "UNAVAILABLE",
                "spot": None,
                "sentiment": "UNAVAILABLE",
                "recommendation": "STAND_ASIDE",
            }
            continue
        dir_mult = 1.0 if "BUY_CE" in res.trade_recommendation else (-1.0 if "BUY_PE" in res.trade_recommendation else 0.0)
        weighted_score += dir_mult * wt
        covered_weight += wt
        constituents[sym] = {
            "weight_pct": round(wt * 100, 2),
            "status": "LIVE",
            "spot": res.underlying_price,
            "sentiment": res.sentiment,
            "recommendation": res.trade_recommendation,
        }

    norm_score = weighted_score / covered_weight if covered_weight else 0.0
    nifty_res = _analyze_live_options("NIFTY")

    if not covered_weight:
        status = "UNAVAILABLE"
        message = "No live option-chain data for the 6 Generals; stand aside."
    elif unavailable_symbols:
        status = "PARTIAL"
        message = f"Incomplete live coverage ({', '.join(unavailable_symbols)} unavailable); no index-direction permission."
    elif norm_score >= 0.25:
        status = "GREEN_LIGHT"
        message = f"6 Generals leading confluent institutional accumulation (+{norm_score*100:.1f}% weighted score). Safe to buy Nifty calls."
    elif norm_score <= -0.25:
        status = "RED_LIGHT"
        message = f"6 Generals facing heavy institutional liquidation/writing ({norm_score*100:.1f}% weighted score). Avoid long index positions."
    else:
        status = "CAUTION"
        message = f"Divergent/neutral flow across 6 Generals ({norm_score*100:+.1f}% weighted score). Trade selective stocks only."

    return {
        "alignment_status": status,
        "source": "LIVE_KITE",
        "message": message,
        "weighted_score": round(norm_score, 3),
        "covered_nifty_weight_pct": round(covered_weight * 100, 2),
        "unavailable_symbols": unavailable_symbols,
        "constituents": constituents,
        "hdfc_bank": constituents["HDFCBANK"],
        "reliance": constituents["RELIANCE"],
        "nifty_recommendation": nifty_res.trade_recommendation if nifty_res else "STAND_ASIDE",
    }


@mcp.tool()
def scan_fo_breakouts() -> List[Dict]:
    """
    Scans liquid F&O stocks across all sectors (HAL, TATASTEEL, M&M, HDFCBANK, SBIN, etc.)
    and ranks the top intraday breakout candidates by conviction.
    """
    universe = [
        "HAL", "TATASTEEL", "M&M", "TATAMOTORS", "HDFCBANK",
        "SBIN", "SUNPHARMA", "BHARTIARTL", "LT", "ICICIBANK",
        "RELIANCE", "INFY", "TCS", "ITC"
    ]
    rankings = []

    for sym in universe:
        res = _analyze_live_options(sym)
        if res is None:
            continue
        spec = OptionsAnalyticsEngine.SYMBOL_SPECS.get(sym, OptionsAnalyticsEngine.SYMBOL_SPECS["NIFTY"])
        atm_strike = int(round(res.underlying_price / spec["strike_step"]) * spec["strike_step"])
        rankings.append({
            "symbol": sym,
            "source": "LIVE_KITE",
            "spot_price": res.underlying_price,
            "suggested_strike": atm_strike,
            "sentiment": res.sentiment,
            "signal": res.trade_recommendation,
            "conviction": int(res.confidence * 100),
            "thesis": res.analysis_thesis,
        })

    rankings.sort(key=lambda x: x["conviction"], reverse=True)
    return rankings


@mcp.tool()
def get_sector_heatmap() -> Dict:
    """
    Ranks Indian market sectors (Metals, Auto, Defense, Pharma, IT, Banking, Energy)
    by institutional capital flow, Net OI acceleration, and breakout probability.
    Highlights where profit is actually flowing today.
    """
    sectors = OptionsAnalyticsEngine.SECTOR_MAP
    sector_rankings = []

    unavailable_symbols: List[str] = []
    for sec_name, symbols in sectors.items():
        conf_sum = 0.0
        bull_count = 0
        sym_results = []
        for sym in symbols:
            res = _analyze_live_options(sym)
            if res is None:
                unavailable_symbols.append(sym)
                continue
            conf_sum += res.confidence
            if "BUY" in res.trade_recommendation:
                bull_count += 1
            sym_results.append({
                "symbol": sym,
                "spot": res.underlying_price,
                "signal": res.trade_recommendation,
                "conviction": int(res.confidence * 100),
                "thesis": res.analysis_thesis,
            })

        if not sym_results:
            continue
        avg_conf = conf_sum / len(sym_results)
        top_stock = max(sym_results, key=lambda x: x["conviction"])
        status = "TOP_LEADER" if avg_conf >= 0.75 else ("OUTPERFORM" if bull_count > 0 else "NEUTRAL")

        sector_rankings.append({
            "sector": sec_name,
            "status": status,
            "avg_conviction": round(avg_conf * 100, 1),
            "top_stock": top_stock["symbol"],
            "top_signal": top_stock["signal"],
            "constituents": sym_results,
        })

    sector_rankings.sort(key=lambda x: x["avg_conviction"], reverse=True)
    if not sector_rankings:
        return {
            "status": "UNAVAILABLE",
            "source": "LIVE_KITE",
            "leading_sector": None,
            "top_actionable_trade": "STAND_ASIDE",
            "sector_rankings": [],
            "unavailable_symbols": sorted(set(unavailable_symbols)),
        }
    return {
        "status": "LIVE" if not unavailable_symbols else "PARTIAL",
        "source": "LIVE_KITE",
        "leading_sector": sector_rankings[0]["sector"],
        "top_actionable_trade": f"{sector_rankings[0]['top_signal']} {sector_rankings[0]['top_stock']}",
        "sector_rankings": sector_rankings,
        "unavailable_symbols": sorted(set(unavailable_symbols)),
    }


@mcp.tool()
def get_market_lens_breadth() -> Dict:
    """
    Compatibility status for NSE Market Lens.

    Market Lens is deliberately not automated: its published terms prohibit
    automated collection and use in virtual trading/simulation.  This tool never
    fabricates Market Lens data; use `get_live_market_breadth` and
    `get_premarket_intelligence` for the compliant data actually used by the
    cockpit.
    """
    return {
        "status": "NOT_CONNECTED_TERMS_RESTRICTED",
        "source": "NSE_MARKET_LENS_NOT_AUTOMATED",
        "reason": "No Market Lens scrape, API reverse engineering, or virtual-trading use is permitted by this cockpit.",
        "use_instead": ["get_premarket_intelligence", "get_live_market_breadth", "get_cockpit_briefing"],
    }


@mcp.tool()
def get_premarket_intelligence() -> Dict:
    """Returns NSE pre-open evidence plus verified SmartTrading delivery provenance."""
    premarket = SmartTradingBridge().read_nse_premarket_and_oi()
    ban_list = premarket.get("fno_ban_list") or []
    part_pos = premarket.get("participant_positioning") or {}

    # Enrich from NSE institutional intel archive if local desk server was offline
    if not ban_list or not part_pos:
        try:
            from nse_institutional_intel import get_nse_intel
            cached_intel = get_nse_intel()
            if not ban_list:
                ban_list = cached_intel.get("fno_ban_list", [])
            if not part_pos:
                part_pos = cached_intel.get("participant_positioning", {})
        except Exception:
            pass

    return {
        "status": "FRESH" if premarket.get("is_today_fresh") else "STALE_OR_INCOMPLETE",
        "trading_date": premarket.get("trading_date"),
        "raw_delivery": premarket.get("raw_premarket"),
        "analysis_source": premarket.get("premarket_source"),
        "auction_breadth": premarket.get("auction_breadth"),
        "participant_positioning": part_pos,
        "session_bias": premarket.get("premarket_bias"),
        "fno_ban_list": ban_list,
        "guardrail": "Pre-open evidence sets hypotheses only; the external CIO must wait for live price/flow confirmation before any entry.",
    }


@mcp.tool()
def get_live_market_breadth() -> Dict:
    """Returns fail-closed live F&O breadth from batched Zerodha Kite quotes, never a fixture."""
    feed = LiveKiteFeed()
    universe = feed.get_full_fno_universe(include_indices=False)
    raw = feed.fetch_live_market_breadth(universe) if universe else {}
    breadth = TechnicalLensEngine.compute_live_market_lens_breadth(
        raw, sector_map=OptionsAnalyticsEngine.SECTOR_MAP
    )
    return {
        "status": "LIVE" if breadth.source == "LIVE_KITE_BATCH" else "UNAVAILABLE",
        "source": breadth.source,
        "universe_count": len(universe),
        "advances": breadth.advances,
        "declines": breadth.declines,
        "unchanged": breadth.unchanged,
        "ad_ratio": breadth.ad_ratio,
        "sentiment": breadth.market_breadth_sentiment,
        "top_turnover_sectors": breadth.top_turnover_sectors,
        "volume_shockers": breadth.volume_shockers,
    }


@mcp.tool()
def evaluate_super_setup(symbol: str = "TATAMOTORS") -> Dict:
    """
    Evaluates the complete 5-engine institutional confluence:
    1. Technicals: Price vs VWAP, 9 EMA, 40 EMA (Senior's setup)
    2. Nuvama Derivatives: OI velocity, Call/Put Walls, Max Pain
    3. Quantsapp: Net GEX & Gamma Squeeze score
    4. Sensibull: Macro Generals (HDFCBANK + RELIANCE)
    5. Live broker breadth (when available; never a fabricated Market Lens feed)
    """
    _, snapshot = _live_option_snapshot(symbol)
    if snapshot is None:
        return {
            **_live_data_unavailable(symbol, "NO_LIVE_OPTION_CHAIN"),
            "super_setup_score": 0,
            "decision": "STAND_ASIDE",
            "action_description": "NO TRADE (live option chain unavailable)",
            "technicals": {},
            "live_market_breadth": {"source": "UNAVAILABLE"},
            "confluence_points": [],
        }
    underlying_px = snapshot.spot_price
    chain = snapshot.chain
    opt_res = OptionsAnalyticsEngine.analyze(underlying_px, chain, symbol=symbol)
    gex_res = GreeksGexEngine.evaluate_frontier_landscape(symbol, underlying_px, chain)

    hdfc_res = AIAgentTrader("HDFCBANK", live_mode=True).inspect_market()
    rel_res = AIAgentTrader("RELIANCE", live_mode=True).inspect_market()
    generals_status = "GREEN_LIGHT" if (hdfc_res.trade_recommendation == "BUY_CE" and rel_res.trade_recommendation == "BUY_CE") else ("RED_LIGHT" if (hdfc_res.trade_recommendation == "BUY_PE" and rel_res.trade_recommendation == "BUY_PE") else "CAUTION")
    feed = LiveKiteFeed()
    universe = feed.get_full_fno_universe(include_indices=False)
    raw_breadth = feed.fetch_live_market_breadth(universe) if universe else {}
    live_breadth = TechnicalLensEngine.compute_live_market_lens_breadth(
        raw_breadth, sector_map=OptionsAnalyticsEngine.SECTOR_MAP
    )

    result = TechnicalLensEngine.synthesize_super_setup(
        symbol=symbol,
        current_price=underlying_px,
        options_recommendation=opt_res.trade_recommendation,
        options_confidence=opt_res.confidence,
        gex_regime=gex_res.gex_regime,
        gamma_squeeze_score=gex_res.gamma_squeeze_score,
        generals_status=generals_status,
        market_breadth=live_breadth,
    )
    result.update({"status": "LIVE", "source": "LIVE_KITE"})
    return result


@mcp.tool()
def execute_paper_order(
    symbol: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    target: float,
    lots: int = 1,
    thesis: str = "",
) -> Dict:
    """
    Deprecated compatibility tool. It intentionally refuses all calls because an
    external CIO must use `cio_authorize_entry`, which applies the full policy,
    requires a thesis, and records a CIO decision journal entry.
    """
    return {
        "status": "REJECTED",
        "reason": "UNGATED_ORDER_PATH_DISABLED",
        "required_tool": "cio_authorize_entry",
        "message": "Only an explicit external-CIO decision can open a paper trade.",
    }


@mcp.tool()
def close_paper_order(trade_id: int, exit_price: float, exit_reason: str = "MANUAL_EXIT") -> Dict:
    """
    Deprecated compatibility tool. It intentionally refuses all calls because a
    discretionary exit must use `cio_manage_position` with a written reason.
    """
    return {
        "status": "REJECTED",
        "reason": "UNGATED_EXIT_PATH_DISABLED",
        "required_tool": "cio_manage_position",
        "message": "Only an explicit external-CIO decision can close a paper trade.",
    }


@mcp.tool()
def get_trading_authority() -> Dict:
    """Returns the enforced external-CIO authority contract and risk envelope."""
    policy = get_trading_policy()
    return {
        "execution_mode": policy.execution_mode,
        "authorized_decision_makers": list(policy.authorized_decision_makers),
        "autonomous_entry": "DISABLED",
        "autonomous_management": "DISABLED",
        "guardian_authority": ["REFRESH_COCKPIT", "HARD_STOP_LOSS", "EOD_SQUARE_OFF"],
        "cio_authority": ["ENTER", "STAND_ASIDE", "HOLD", "TRAIL_SL", "PARTIAL_BOOK", "BOOK_PROFIT", "EXIT"],
        "risk_envelope": {
            "demat_budget_inr": policy.demat_budget_inr,
            "max_concurrent_positions": policy.max_concurrent_positions,
            "max_single_trade_risk_inr": policy.max_single_trade_risk_inr,
            "daily_loss_limit_inr": policy.daily_loss_limit_inr,
        },
    }


@mcp.tool()
def get_portfolio_summary() -> Dict:
    """
    Returns the real-time paper ledger performance summary:
    Total closed trades, win rate, gross PnL, total STT & statutory friction, and net realized PnL.
    """
    return ledger.get_summary()


@mcp.tool()
def get_open_positions() -> List[Dict]:
    """
    Returns all currently open active paper positions.
    """
    return ledger.get_open_trades()


@mcp.tool()
def get_cockpit_briefing(max_candidates: int = 3) -> Dict:
    """
    Builds the lightweight AI CIO Cockpit Briefing synthesizing live Kite quotes/bars/depth,
    SmartTrading Desk connector telemetry (HTTP/SHM/ZMQ/NSE OI/Collision Mutex), Sensibull walls,
    volatility-normalized EMAs, spread-adjusted GPU Monte Carlo PoT, and active position hints.
    """
    from cockpit import CockpitBuilder
    return CockpitBuilder().build_briefing(max_candidates=max_candidates)


@mcp.tool()
def cio_authorize_entry(
    symbol: str,
    direction: str,
    contract: str,
    sl: float,
    target: float,
    lots: int = 1,
    thesis: str = "",
) -> Dict:
    """
    Executes an explicit AI CIO trade decision (validates live Ask spread, Demat ₹50k budget,
    Desk Collision Mutex, NSE F&O ban, clock window, and risk cap).
    """
    from agent_trader import cio_enter_trade
    return cio_enter_trade(
        symbol=symbol,
        direction=direction,
        contract=contract,
        sl=sl,
        target=target,
        lots=lots,
        thesis=thesis,
        live_mode=True,
    )


@mcp.tool()
def cio_manage_position(
    trade_id: int,
    action: str,
    reason: str,
    sl: Optional[float] = None,
    exit_lots: Optional[int] = None,
) -> Dict:
    """
    Executes an AI CIO active position decision: TRAIL_SL (stay & ride rally),
    PARTIAL_BOOK (book part profit & trail rest), BOOK_PROFIT, or EXIT.
    """
    from agent_trader import cio_manage_trade
    return cio_manage_trade(
        trade_id=trade_id,
        action=action,
        reason=reason,
        sl=sl,
        exit_lots=exit_lots,
        live_mode=True,
    )


@mcp.tool()
def run_regime_logic_test(days: int = 25) -> Dict:
    """
    Runs continuous anti-curve-fit out-of-sample walk-forward Logic Testing across
    Normal, Expiry, and High-Vol regimes using real NSE 5m bars & archived Desk tick DBs.
    """
    from logic_tester import run_logic_validation
    return run_logic_validation(days=days)


@mcp.tool()
def get_screener_intelligence(symbol: Optional[str] = None) -> Dict:
    """
    Returns fundamental risk scoring, ROCE, debt flags, and quarterly results context
    harvested from Screener.in via the Screener Bot Bridge extension.
    """
    p = Path(__file__).resolve().parent / "runtime" / "screener_intelligence.json"
    if not p.exists():
        data_dir = Path(__file__).resolve().parent.parent / "extension" / "screener" / "data"
        candidates = sorted(data_dir.glob("*/*.json"), key=lambda x: x.stat().st_mtime, reverse=True)
        if candidates:
            p = candidates[0]
        else:
            return {
                "status": "NO_DATA",
                "message": "No Screener.in data captured yet. Run 'screener' in the extension or execute python3 extension/screener/main.py",
                "safe_universe": [],
                "avoid_list": [],
            }

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "PARSE_ERROR", "error": str(exc)}

    candidates = data.get("screen_candidates") or []
    comp_snap = data.get("company_snapshot")

    if symbol:
        target = symbol.upper().strip()
        matched = None
        if comp_snap and comp_snap.get("symbol") == target:
            matched = comp_snap
            return {
                "status": "MATCHED_COMPANY",
                "symbol": target,
                "name": matched.get("name"),
                "bot_score": matched.get("bot_score"),
                "risk_bucket": matched.get("risk_bucket"),
                "position_size_hint": matched.get("position_size_hint"),
                "risk_flags": matched.get("risk_flags", []),
                "blocked_reasons": matched.get("blocked_reasons", []),
                "ratios": matched.get("ratios", {}),
            }
        for c in candidates:
            c_sym = (c.get("symbol") or c.get("name") or "").upper().strip()
            if target in c_sym or c_sym in target:
                matched = c
                break
        if matched:
            return {
                "status": "MATCHED_CANDIDATE",
                "symbol": target,
                "name": matched.get("name"),
                "bot_score": matched.get("bot_score"),
                "risk_bucket": matched.get("risk_bucket"),
                "intraday_use": matched.get("intraday_use"),
                "position_size_multiplier": matched.get("position_size_multiplier"),
                "reasons": matched.get("reasons", []),
                "warnings": matched.get("warnings", []),
                "blocked_reasons": matched.get("blocked_reasons", []),
                "metrics": matched.get("metrics", {}),
            }
        return {
            "status": "SYMBOL_NOT_IN_SCREEN",
            "symbol": target,
            "message": f"{target} not found in latest Screener run ({data.get('timestamp')}).",
        }

    safe_list = [c.get("symbol") or c.get("name") for c in candidates if c.get("intraday_use") in ("watch", "normal_trade")]
    avoid_list = [c.get("symbol") or c.get("name") for c in candidates if c.get("intraday_use") == "avoid" or c.get("blocked_reasons")]
    
    return {
        "status": "ACTIVE_SUMMARY",
        "phase": data.get("phase"),
        "timestamp": data.get("timestamp"),
        "total_screened": len(candidates),
        "safe_fno_universe": [s for s in safe_list if s][:15],
        "avoid_list": [s for s in avoid_list if s][:10],
        "top_candidates": [
            {
                "symbol": c.get("symbol") or c.get("name"),
                "score": c.get("bot_score"),
                "intraday_use": c.get("intraday_use"),
                "reasons": c.get("reasons", [])[:2],
            }
            for c in candidates[:5]
        ],
    }


@mcp.tool()
def get_nse_institutional_intel(symbol: Optional[str] = None) -> Dict:
    """
    Returns official clearing & settlement intelligence from NSE Archives:
    - Participant-wise Open Interest: FII Index Long Ratio, Client vs FII option trap matrix.
    - Cash Market Delivery Data: Deliverable quantity, delivery percentage, and institutional accumulation quality.
    - F&O Ban Tracker: Checks whether a symbol is legally banned by NSE from opening new derivative positions.
    """
    try:
        from nse_institutional_intel import get_nse_intel
        return get_nse_intel(symbol=symbol)
    except Exception as exc:
        return {"status": "ERROR", "error": str(exc)}


if __name__ == "__main__":
    mcp.run()

