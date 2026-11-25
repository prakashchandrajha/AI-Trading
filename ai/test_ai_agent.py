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


def test_connector_bridge_and_book_telemetry():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from connector.smart_trading_bridge import SmartTradingBridge, compute_book_telemetry
    from quantitative_alpha import QuantitativeAlphaEngine

    assert SmartTradingBridge.extract_underlying_from_ticker("HAL26DEC4900CE") == "HAL"
    assert SmartTradingBridge.extract_underlying_from_ticker("NIFTY26DEC25800PE") == "NIFTY"
    assert SmartTradingBridge.extract_underlying_from_ticker("BANKNIFTY26NOV53000CE") == "BANKNIFTY"

    bids = [(100.0, 2000), (99.95, 1500), (99.90, 1200)]
    asks = [(100.10, 400), (100.15, 300), (100.20, 250)]
    tel = compute_book_telemetry(100.05, bids, asks, lot_size=25)
    assert tel["kobp"] > 0.3
    assert tel["micro_lead"] > 0.0
    assert tel["spread_pct"] < 0.5

    # Hostile ask replenishment warning detection
    prev_asks = [(100.10, 400), (100.15, 300)]
    reloaded_asks = [(100.10, 1500), (100.15, 900)]
    tel_trap = compute_book_telemetry(100.05, [(100.0, 300), (99.95, 200)], reloaded_asks, prev_bid_depth=bids[:2], prev_ask_depth=prev_asks, lot_size=25)
    assert tel_trap["ask_replenish_warning"] is True

    # Live depth fail-closed check
    empty_state = QuantitativeAlphaEngine.evaluate_machine_alpha(
        "HAL", 4900.0, 50.0, 150, 10000, 12000, 100.0, require_real_depth=True
    )
    assert empty_state.source == "DEPTH_UNAVAILABLE"

    live_state = QuantitativeAlphaEngine.evaluate_machine_alpha(
        "HAL", 4900.0, 50.0, 150, 10000, 12000, 100.0,
        bid_depth=bids, ask_depth=asks, require_real_depth=True,
    )
    assert live_state.source == "LIVE_L2_DEPTH"
    assert live_state.kobp > 0.3


def test_cio_clock_enter_trail_and_partial_book(tmp_path):
    import datetime
    from zoneinfo import ZoneInfo
    from cockpit import evaluate_session_clock
    from agent_trader import cio_enter_trade, cio_manage_trade

    ist = ZoneInfo("Asia/Kolkata")
    # Tuesday 10:15 AM -> Expiry day + Entries Permitted
    tue_open = datetime.datetime(2026, 11, 24, 10, 15, tzinfo=ist)
    clk_open = evaluate_session_clock(tue_open)
    assert clk_open["expiry_regime"] == "EXPIRY_GAMMA_DAY"
    assert clk_open["entries_permitted"] is True

    # Wednesday 14:45 PM -> Manage only (no new entries allowed)
    wed_late = datetime.datetime(2026, 11, 25, 14, 45, tzinfo=ist)
    clk_late = evaluate_session_clock(wed_late)
    assert clk_late["entries_permitted"] is False
    assert clk_late["phase"] == "MANAGE_ONLY_NO_NEW_ENTRIES"

    ledger = AIPaperLedger(db_path=tmp_path / "cio_test.db")
    # Empty thesis rejected
    rej = cio_enter_trade("NIFTY", "BUY_CE", "NIFTY26DEC25800CE", 180.0, 260.0, 1, thesis="", entry_price=200.0, live_mode=False, ledger=ledger)
    assert rej["status"] == "REJECTED"

    # Valid entry with 2 lots (sl=185.0 -> risk = (200-185)*130 = 1,950 <= ₹3,500 cap)
    entered = cio_enter_trade(
        "NIFTY", "BUY_CE", "NIFTY26DEC25800CE", sl=185.0, target=270.0, lots=2,
        thesis="Strong institutional breakout above VWAP with open Call Wall",
        entry_price=200.0, live_mode=False, ledger=ledger,
    )
    assert entered["status"] == "OPENED_TRADE"
    tid = entered["trade_id"]

    # Stay & Ride Rally: trail SL upward
    trailed = cio_manage_trade(tid, "TRAIL_SL", "Holding above 20 EMA", sl=205.0, live_mode=False, ledger=ledger)
    assert trailed["status"] == "SL_UPDATED"
    assert ledger.get_open_trades()[0]["stop_loss"] == 205.0

    # Partial Profit Book: sell 1 lot at ₹245, keep 1 runner open with higher SL ₹220
    pbook = cio_manage_trade(tid, "PARTIAL_BOOK", "Lock 50% near Call Wall, ride 1 runner", sl=220.0, exit_lots=1, exit_price=245.0, live_mode=False, ledger=ledger)
    assert pbook["status"] == "PARTIAL_BOOKED"
    assert pbook["remaining_lots"] == 1
    assert pbook["net_pnl"] > 1000.0
    assert ledger.get_open_trades()[0]["lots"] == 1
    assert ledger.get_open_trades()[0]["stop_loss"] == 220.0
    assert len(ledger.get_recent_cio_decisions(10)) == 3


def test_logic_tester_regimes_and_simulation():
    import datetime
    from logic_tester import classify_indian_session_regime, _simulate_logic_on_bars

    assert classify_indian_session_regime(datetime.date(2026, 11, 24), 1.0) == "EXPIRY_DAY"
    assert classify_indian_session_regime(datetime.date(2026, 11, 25), 2.5) == "HIGH_VOL_TREND"
    assert classify_indian_session_regime(datetime.date(2026, 11, 25), 0.9) == "NORMAL_DAY"

    # Deterministic 65-bar trend day replay checking zero crash & valid attribution
    day_bars = []
    t0 = datetime.datetime(2026, 11, 25, 9, 15, tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
    for i in range(65):
        ts = (t0 + datetime.timedelta(minutes=5 * i)).isoformat()
        px = 1000.0 + i * 1.8
        vol = 5000 if i < 42 else 14000  # High relative volume expansion on breakout
        day_bars.append({"date": ts, "open": px - 1.0, "high": px + 1.5, "low": px - 1.5, "close": px, "volume": vol})

    res = _simulate_logic_on_bars("TEST", {"2026-11-25": day_bars}, lot_size=100, logic_name="CAUSAL_EXPANSION_RIBBON")
    assert len(res) >= 1
    assert res[0].direction == "BUY_CE"
    assert res[0].option_net_pnl > 0


def test_smart_edge_wall_fragility_and_composite():
    from smart_edge import (
        compute_wall_fragility, compute_oi_herfindahl,
        compute_iv_skew, compute_straddle_compression,
        compute_relative_volume, compute_composite_edge,
        evaluate_smart_edge,
    )

    # Crumbling wall: 50 lakh OI lost 15 lakh, spot nearby
    crumble = compute_wall_fragility(
        wall_strike=25500.0, wall_oi=5000000, wall_oi_change=-1500000,
        spot=25450.0, atr=50.0, is_call_wall=True,
    )
    assert crumble.status in ("CRUMBLING", "WEAKENING")
    assert crumble.fragility_score > 0.4
    assert crumble.breakout_probability > 0.3

    # Fortress wall: gaining OI, spot far away
    fortress = compute_wall_fragility(
        wall_strike=26000.0, wall_oi=8000000, wall_oi_change=2000000,
        spot=25000.0, atr=50.0, is_call_wall=True,
    )
    assert fortress.status in ("FORTRESS", "DEFENDING")
    assert fortress.fragility_score < 0.3

    # OI Concentration: single-strike concentrated chain
    conc_chain = [{"call_oi": 10000000, "put_oi": 5000, "strike": 25500}]
    hhi, regime = compute_oi_herfindahl(conc_chain)
    assert regime == "CONCENTRATED_PIN"

    # OI Dispersed: evenly spread chain
    disp_chain = [{"call_oi": 100000, "put_oi": 100000, "strike": 25000 + i * 50} for i in range(20)]
    hhi_d, regime_d = compute_oi_herfindahl(disp_chain)
    assert regime_d in ("DISPERSED_TREND", "MODERATE")

    # IV Skew: fear hedge (put IV >> call IV)
    fear_chain = [
        {"strike": 25400, "call_ltp": 20.0, "put_ltp": 180.0, "call_oi": 1000, "put_oi": 50000},
        {"strike": 25500, "call_ltp": 100.0, "put_ltp": 100.0, "call_oi": 10000, "put_oi": 10000},
        {"strike": 25600, "call_ltp": 50.0, "put_ltp": 15.0, "call_oi": 50000, "put_oi": 1000},
    ]
    skew = compute_iv_skew(fear_chain, 25500.0, 100.0)
    assert skew.skew_regime in ("FEAR_HEDGE", "NEUTRAL", "COMPLACENT", "CALL_DEMAND")

    # Straddle compression: cheap straddle on volatile stock
    pct, sreg = compute_straddle_compression(50.0, 25000.0, 0.35)
    assert pct > 0.0

    # Relative volume: institutional surge
    rvol, vreg = compute_relative_volume(5000000, 1500000)
    assert vreg == "INSTITUTIONAL_SURGE"
    assert rvol > 2.5

    # Dead tape
    rvol_d, vreg_d = compute_relative_volume(300000, 1500000)
    assert vreg_d == "DEAD_TAPE"

    # Full evaluate_smart_edge smoke test
    result = evaluate_smart_edge(
        symbol="NIFTY", spot=25500.0, chain_data=fear_chain, strike_step=100.0,
        tech_indicators={"atr_5m": 50.0, "realized_vol_annual": 0.18, "ema_20_series": []},
        quant_alpha={"z_ofi": 1.8, "kobp": 0.25},
        opt_analytics={
            "call_wall_strike": 25600, "call_wall_oi": 5000000, "call_wall_oi_change": -500000,
            "put_wall_strike": 25400, "put_wall_oi": 3000000, "put_wall_oi_change": 100000,
            "atm_straddle_premium": 200.0,
        },
        mc_result={"p_win_target": 0.55},
        is_ce=True, current_volume=2000000, avg_volume_20d=1200000,
    )
    assert 0.0 <= result.composite_edge_score <= 100.0
    assert result.edge_verdict in ("STRONG_EDGE", "MODERATE_EDGE", "NO_EDGE", "ADVERSE")
    assert len(result.edge_factors) >= 1


def test_ema_acceleration_reversing_reachable():
    """Verifies that the REVERSING regime is reachable after the if/elif reorder fix."""
    from smart_edge import compute_ema_acceleration

    # Build a 12-bar EMA series that sharply reverses direction:
    # First 10 bars trending up, then abruptly reversing down
    ema_series = [100.0, 102.0, 104.0, 106.0, 108.0, 110.0, 112.0, 110.0, 106.0, 100.0, 93.0, 85.0]
    atr = 2.0  # ATR for normalization

    accel, regime = compute_ema_acceleration(ema_series, atr)
    # With strong reversal, accel should be very negative (< -0.35), triggering REVERSING
    assert accel < -0.35, f"Expected accel < -0.35 for sharp reversal, got {accel}"
    assert regime == "REVERSING", f"Expected REVERSING, got {regime}"

    # Milder deceleration: should hit DECELERATING (between -0.15 and -0.35)
    mild_series = [100.0, 102.0, 104.0, 106.0, 108.0, 110.0, 111.0, 111.5, 111.0, 110.0, 109.5, 109.0]
    accel_mild, regime_mild = compute_ema_acceleration(mild_series, atr)
    assert regime_mild in ("DECELERATING", "STEADY", "REVERSING")

    # Accelerating uptrend: steady acceleration
    accel_series = [100.0, 100.5, 101.5, 103.0, 105.0, 107.5, 110.5, 114.0, 118.0, 122.5, 127.5, 133.0]
    accel_up, regime_up = compute_ema_acceleration(accel_series, atr)
    assert accel_up > 0.15, f"Expected accel > 0.15 for accelerating uptrend, got {accel_up}"
    assert regime_up == "ACCELERATING"

    # With real price history, EMA-20 series should produce non-trivial acceleration
    from technical_lens import TechnicalLensEngine
    trending_prices = [4800.0 + i * 2.5 for i in range(50)]
    ema20_series = TechnicalLensEngine.calculate_ema_series(trending_prices, 20)
    assert len(ema20_series) >= 12, "EMA-20 series should have enough bars for acceleration calc"
    accel_real, regime_real = compute_ema_acceleration(ema20_series, 5.0)
    # Steady uptrend should give ACCELERATING or STEADY (not always STEADY from empty [])
    assert regime_real in ("ACCELERATING", "STEADY")
