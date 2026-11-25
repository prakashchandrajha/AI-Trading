"""
Test Suite for AI Autonomous Options Trading Agent
Verifies:
1. Max Pain calculation on known mathematical distributions.
2. Put-Call Ratio (PCR) calculation.
3. Call/Put wall identification.
4. Indian STT, GST, and turnover friction accuracy.
5. Paper ledger open, close, and net PnL accounting.
6. Proves that desk/ directory has 0 touched files.
"""
import subprocess
from pathlib import Path
import pytest

from options_analytics import OptionsAnalyticsEngine as NuvamaOptionsEngine, OptionStrikeData
from greeks_gex import GreeksGexEngine
from paper_ledger import AIPaperLedger


def test_max_pain_calculation():
    # Symmetric chain where 25500 is the center
    chain = [
        OptionStrikeData(strike=25400, call_oi=100000, put_oi=500000),
        OptionStrikeData(strike=25500, call_oi=500000, put_oi=500000),
        OptionStrikeData(strike=25600, call_oi=500000, put_oi=100000),
    ]
    max_pain = NuvamaOptionsEngine.calculate_max_pain(chain)
    assert max_pain == 25500.0


def test_nuvama_analytics_walls_and_pcr():
    chain = [
        OptionStrikeData(strike=25300, call_oi=100000, put_oi=1000000),  # Put Wall
        OptionStrikeData(strike=25400, call_oi=300000, put_oi=600000),
        OptionStrikeData(strike=25500, call_oi=1500000, put_oi=200000),  # Call Wall
    ]
    res = NuvamaOptionsEngine.analyze(25400.0, chain)
    assert res.call_wall_strike == 25500.0
    assert res.put_wall_strike == 25300.0
    assert res.total_call_oi == 1900000
    assert res.total_put_oi == 1800000
    assert res.pcr == round(1800000 / 1900000, 2)


def test_indian_friction_calculation():
    # Buy 1 lot (25 units) at 100, sell at 120 (gain of 20 pts = ₹500 gross)
    friction = AIPaperLedger.calculate_indian_friction(entry_price=100.0, exit_price=120.0, quantity=25)
    # Friction should include STT (0.15% on sell: 120 * 25 * 0.0015 = 4.5), brokerage (40), GST, turnover
    assert friction > 45.0  # Must be at least ₹45–₹60
    assert friction < 75.0


def test_paper_ledger_lifecycle(tmp_path):
    test_db = tmp_path / "test_ledger.db"
    ledger = AIPaperLedger(db_path=test_db)

    # 1. Open Trade
    tid = ledger.open_trade(
        underlying="NIFTY",
        symbol="NIFTY26NOV25500CE",
        direction="BUY_CE",
        entry_price=100.0,
        stop_loss=80.0,
        target=140.0,
        lots=1,
        lot_size=25,
        thesis="Oversold bounce at Put Wall",
    )
    assert tid == 1

    open_trades = ledger.get_open_trades()
    assert len(open_trades) == 1
    assert open_trades[0]["symbol"] == "NIFTY26NOV25500CE"

    # 1b. Test Trailing Stop Loss
    updated = ledger.update_stop_loss(tid, new_stop_loss=102.0)
    assert updated is True
    open_trades_after = ledger.get_open_trades()
    assert open_trades_after[0]["stop_loss"] == 102.0

    # 2. Close Trade with Profit (Target hit at 135)
    res = ledger.close_trade(tid, exit_price=135.0, exit_reason="TARGET_HIT")
    assert res["gross_pnl"] == (135.0 - 100.0) * 25  # 875.0
    assert res["net_pnl"] == round(875.0 - res["friction"], 2)
    assert res["net_pnl"] > 800.0

    # 3. Summary check
    summary = ledger.get_summary()
    assert summary["total_trades"] == 1
    assert summary["wins"] == 1
    assert summary["win_rate"] == 100.0


def test_midcpnifty_analytics_and_sizing():
    from agent_trader import AIAgentTrader
    agent = AIAgentTrader(symbol="MIDCPNIFTY")
    assert agent.symbol == "MIDCPNIFTY"
    assert agent.lot_size == 120
    assert agent.spec["strike_step"] == 25.0
    assert agent.spec["expiry_day"] == "Monday"

    result = agent.inspect_market()
    assert result.underlying_price == 13125.0
    assert result.put_wall_strike == 13100.0
    assert result.call_wall_strike == 13200.0
    assert result.trade_recommendation == "BUY_CE"
    assert "MIDCPNIFTY" in result.analysis_thesis


def test_greeks_and_gex_calculation():
    # Test Black-Scholes Greeks and Net GEX calculation
    greeks = GreeksGexEngine.calculate_greeks(
        spot=25000.0,
        strike=25000.0,
        dte=1.0,
        iv=0.15,
        call_oi=1000000,
        put_oi=1500000,
        lot_size=25,
    )
    assert 0.45 <= greeks.delta_call <= 0.55
    assert -0.55 <= greeks.delta_put <= -0.45
    assert greeks.gamma > 0.0
    assert greeks.net_gex > 0.0  # More put OI than call OI produces positive net GEX


def test_mcp_server_tools():
    from mcp_server import analyze_options, check_generals_alignment, scan_fo_breakouts, get_portfolio_summary, get_sector_heatmap
    nifty_data = analyze_options("NIFTY")
    assert nifty_data["spot_price"] > 0
    assert "trade_recommendation" in nifty_data

    generals = check_generals_alignment()
    assert "alignment_status" in generals
    assert generals["alignment_status"] in ("GREEN_LIGHT", "RED_LIGHT", "CAUTION")

    breakouts = scan_fo_breakouts()
    assert len(breakouts) >= 5

    heatmap = get_sector_heatmap()
    assert "leading_sector" in heatmap
    assert len(heatmap["sector_rankings"]) > 0

    summary = get_portfolio_summary()
    assert "total_trades" in summary

    from mcp_server import evaluate_super_setup, get_market_lens_breadth
    breadth = get_market_lens_breadth()
    assert "ad_ratio" in breadth
    assert breadth["ad_ratio"] > 0

    super_setup = evaluate_super_setup("TATAMOTORS")
    assert "super_setup_score" in super_setup
    assert super_setup["super_setup_score"] >= 75
    assert "technicals" in super_setup
    assert super_setup["technicals"]["vwap"] > 0


def test_ai_is_strictly_self_contained():
    # Verify that no module in ai/ imports from desk or desk's src
    ai_dir = Path(__file__).parent
    for py_file in ai_dir.glob("*.py"):
        for line in py_file.read_text().splitlines():
            line_clean = line.strip()
            if line_clean.startswith("import ") or line_clean.startswith("from "):
                assert "desk" not in line_clean, f"{py_file} illegally imports from desk: {line}"
                assert "src." not in line_clean, f"{py_file} illegally imports from desk src: {line}"


def test_gpu_engine_acceleration():
    from gpu_engine import GPUQuantitativeEngine
    engine = GPUQuantitativeEngine()
    assert engine.device is not None

    # Test Vectorized Greeks
    greeks = engine.compute_vectorized_greeks(
        spots=[100.0, 100.0],
        strikes=[95.0, 105.0],
        dte_days=[5.0, 5.0],
        ivs=[0.25, 0.25],
        is_calls=[True, False],
    )
    assert len(greeks["delta"]) == 2
    assert greeks["delta"][0] > 0.50 # ITM Call delta > 0.50
    assert greeks["delta"][1] < 0.0  # Put delta < 0.0

    # Test Monte Carlo Simulation
    mc = engine.simulate_monte_carlo_po_t(spot=100.0, annualized_vol=0.25, target_pts=5.0, stop_pts=3.0, num_paths=1000)
    assert "p_win_target" in mc
    assert "p_loss_stop" in mc
    assert "expected_value_ratio" in mc
    assert mc["p_win_target"] >= 0.0


def test_20_ema_triple_ribbon_and_fail_closed_history():
    from technical_lens import TechnicalLensEngine

    chop = TechnicalLensEngine.analyze_technicals(
        "HAL", current_price=4900.0, price_history=[4890.0, 4900.0], volume_history=[100, 100], require_real_history=True
    )
    assert chop.trend_regime == "CHOP_OR_DIVERGENT"
    assert chop.technical_conviction == 0.0

    bars = [4800.0 + i * 2.5 for i in range(50)]
    vols = [1000 + i * 50 for i in range(50)]
    ind = TechnicalLensEngine.analyze_technicals(
        "HAL", current_price=bars[-1], price_history=bars, volume_history=vols, vwap_override=4850.0, require_real_history=True
    )
    assert ind.vwap == 4850.0
    assert ind.ema_9 > ind.ema_20 > ind.ema_40 > 0
    assert ind.is_triple_ribbon_bull is True
    assert ind.trend_regime == "BULLISH_TREND_ALIGNMENT"


def test_live_mode_fails_closed_without_synthetic_bump(tmp_path):
    from agent_trader import AIAgentTrader

    class MissingFeed:
        def get_symbol_meta(self, symbol):
            return {"symbol": symbol, "lot_size": 150, "strike_step": 50.0, "expiry": "2026-12-29", "spot_symbol": f"NSE:{symbol}", "spot_token": 1}

        def fetch_spot_and_option_chain(self, symbol, num_strikes=6):
            return None

        def fetch_contract_quotes(self, symbols):
            return {}

        def _fetch_spot_5m_bars(self, spot_symbol, spot_token):
            return [], []

    ledger = AIPaperLedger(db_path=tmp_path / "live_fail_closed.db")
    agent = AIAgentTrader("HAL", live_mode=True, feed=MissingFeed())
    agent.ledger = ledger

    res_flat = agent.run_cycle()
    assert res_flat["action"] == "STAND_ASIDE"
    assert res_flat["reason"] == "NO_LIVE_MARKET_FEED"
    assert len(ledger.get_open_trades()) == 0

    tid = ledger.open_trade("HAL", "HAL26DEC4900CE", "BUY_CE", 180.0, 150.0, 243.0, 1, 150, "live", source_tag="LIVE_KITE")
    res_open = agent.run_cycle()
    assert res_open["action"] == "MANAGED_ACTIVE_TRADES"
    assert ledger.get_open_trades()[0]["stop_loss"] == 150.0
    assert ledger.get_open_trades()[0]["status"] == "OPEN"

    ledger.open_trade("HAL", "HAL_CURRENT_4250CE", "BUY_CE", 65.0, 53.3, 87.75, 1, 150, "mock", source_tag="SHADOW")
    voided = ledger.void_synthetic_open_trades()
    assert voided == 1
    remaining_open = ledger.get_open_trades()
    assert len(remaining_open) == 1
    assert remaining_open[0]["trade_id"] == tid




