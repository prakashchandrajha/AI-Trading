"""
NSE & Institutional Options Trading MCP Server
Exposes standardized Model Context Protocol (MCP) tools for autonomous AI agents.
Enables AI models to:
1. Inspect live/synthetic NSE option chains & Nuvama-style institutional analytics.
2. Calculate Net Gamma Exposure (GEX), Greeks, and Gamma Squeeze probability.
3. Check 'The Generals' (HDFCBANK, RELIANCE) macro alignment.
4. Scan liquid F&O universe for intraday stock option breakouts.
5. Execute paper orders with exact Indian derivatives friction (STT, GST, SEBI fee).
6. Track open positions, performance, and risk envelope.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional
from fastmcp import FastMCP

from agent_trader import AIAgentTrader
from greeks_gex import GreeksGexEngine
from options_analytics import OptionsAnalyticsEngine
from paper_ledger import AIPaperLedger
from technical_lens import TechnicalLensEngine

mcp = FastMCP("NSE_Smart_Trading")
ledger = AIPaperLedger()


@mcp.tool()
def analyze_options(symbol: str = "NIFTY", tape_db: Optional[str] = None) -> Dict:
    """
    Analyzes Open Interest (OI) metrics for an index or stock option chain.
    Returns Max Pain strike, Put-Call Ratio (PCR), Call Wall (Resistance),
    Put Wall (Support), 15-minute OI Velocity, and AI Trade Recommendation.
    Supported symbols: NIFTY, BANKNIFTY, MIDCPNIFTY, HDFCBANK, RELIANCE, SBIN, TATAMOTORS, INFY.
    """
    agent = AIAgentTrader(symbol=symbol)
    res = agent.inspect_market(tape_db)
    return {
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
    agent = AIAgentTrader(symbol=symbol)
    underlying_px, chain = agent.load_option_chain_from_tape()
    res = GreeksGexEngine.evaluate_frontier_landscape(
        symbol=symbol,
        underlying_price=underlying_px,
        chain=chain,
        dte=dte,
    )
    return {
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
    constituents = {}

    for sym, wt in generals_specs:
        res = AIAgentTrader(sym).inspect_market()
        dir_mult = 1.0 if "BUY_CE" in res.trade_recommendation else (-1.0 if "BUY_PE" in res.trade_recommendation else 0.0)
        weighted_score += dir_mult * wt
        constituents[sym] = {
            "weight_pct": round(wt * 100, 2),
            "spot": res.underlying_price,
            "sentiment": res.sentiment,
            "recommendation": res.trade_recommendation,
        }

    norm_score = weighted_score / total_weight
    nifty_res = AIAgentTrader("NIFTY").inspect_market()

    if norm_score >= 0.25:
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
        "message": message,
        "weighted_score": round(norm_score, 3),
        "covered_nifty_weight_pct": round(total_weight * 100, 2),
        "constituents": constituents,
        "hdfc_bank": constituents["HDFCBANK"],
        "reliance": constituents["RELIANCE"],
        "nifty_recommendation": nifty_res.trade_recommendation,
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
        agent = AIAgentTrader(sym)
        res = agent.inspect_market()
        atm_strike = int(round(res.underlying_price / agent.spec["strike_step"]) * agent.spec["strike_step"])
        rankings.append({
            "symbol": sym,
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
            sym_results.append({
                "symbol": sym,
                "spot": res.underlying_price,
                "signal": res.trade_recommendation,
                "conviction": int(res.confidence * 100),
                "thesis": res.analysis_thesis,
            })

        avg_conf = conf_sum / len(symbols) if symbols else 0.0
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
    return {
        "leading_sector": sector_rankings[0]["sector"],
        "top_actionable_trade": f"{sector_rankings[0]['top_signal']} {sector_rankings[0]['top_stock']}",
        "sector_rankings": sector_rankings,
    }


@mcp.tool()
def get_market_lens_breadth() -> Dict:
    """
    Fetches real-time NSE Market Lens exchange breadth metrics:
    Advances/Declines ratio, Market Breadth Sentiment, Top Turnover Sectors, and Volume Shockers.
    """
    breadth = TechnicalLensEngine.get_market_lens_breadth()
    return {
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
    5. NSE Market Lens: Advances/Declines Breadth (A/D ratio)
    """
    agent = AIAgentTrader(symbol)
    underlying_px, chain = agent.load_option_chain_from_tape()
    opt_res = OptionsAnalyticsEngine.analyze(underlying_px, chain, symbol=symbol)
    gex_res = GreeksGexEngine.evaluate_frontier_landscape(symbol, underlying_px, chain)

    hdfc_res = AIAgentTrader("HDFCBANK").inspect_market()
    rel_res = AIAgentTrader("RELIANCE").inspect_market()
    generals_status = "GREEN_LIGHT" if (hdfc_res.trade_recommendation == "BUY_CE" and rel_res.trade_recommendation == "BUY_CE") else ("RED_LIGHT" if (hdfc_res.trade_recommendation == "BUY_PE" and rel_res.trade_recommendation == "BUY_PE") else "CAUTION")

    return TechnicalLensEngine.synthesize_super_setup(
        symbol=symbol,
        current_price=underlying_px,
        options_recommendation=opt_res.trade_recommendation,
        options_confidence=opt_res.confidence,
        gex_regime=gex_res.gex_regime,
        gamma_squeeze_score=gex_res.gamma_squeeze_score,
        generals_status=generals_status,
    )


@mcp.tool()
def execute_paper_order(
    symbol: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    target: float,
    lots: int = 1,
    thesis: str = "Autonomous MCP execution",
) -> Dict:
    """
    Opens an autonomous paper trade with automatic Indian friction modeling (STT, GST, turnover, stamp duty).
    Direction: 'BUY_CE' or 'BUY_PE'.
    """
    spec = OptionsAnalyticsEngine.SYMBOL_SPECS.get(symbol.upper().split("_")[0], {"lot_size": 25})
    lot_size = spec["lot_size"]

    trade_id = ledger.open_trade(
        underlying=symbol.split("_")[0],
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        stop_loss=stop_loss,
        target=target,
        lots=lots,
        lot_size=lot_size,
        thesis=thesis,
    )
    return {
        "status": "ORDER_PLACED",
        "trade_id": trade_id,
        "symbol": symbol,
        "direction": direction,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "target": target,
        "lot_size": lot_size,
        "quantity": lots * lot_size,
    }


@mcp.tool()
def close_paper_order(trade_id: int, exit_price: float, exit_reason: str = "MANUAL_EXIT") -> Dict:
    """
    Closes an active paper trade and records exact net PnL after all statutory friction.
    """
    result = ledger.close_trade(trade_id, exit_price, exit_reason)
    return result


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


if __name__ == "__main__":
    mcp.run()
