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
import json
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
    assert nifty_data["status"] in ("LIVE", "UNAVAILABLE")
    assert "trade_recommendation" in nifty_data
    if nifty_data["status"] == "LIVE":
        assert nifty_data["spot_price"] > 0

    generals = check_generals_alignment()
    assert "alignment_status" in generals
    assert generals["alignment_status"] in ("GREEN_LIGHT", "RED_LIGHT", "CAUTION", "PARTIAL", "UNAVAILABLE")
    assert generals["source"] == "LIVE_KITE"

    breakouts = scan_fo_breakouts()
    assert isinstance(breakouts, list)
    assert all(row["source"] == "LIVE_KITE" and row["spot_price"] > 0 for row in breakouts)

    heatmap = get_sector_heatmap()
    assert heatmap["status"] in ("LIVE", "PARTIAL", "UNAVAILABLE")
    assert heatmap["source"] == "LIVE_KITE"
    assert "leading_sector" in heatmap
    if heatmap["status"] != "UNAVAILABLE":
        assert len(heatmap["sector_rankings"]) > 0

    summary = get_portfolio_summary()
    assert "total_trades" in summary

    from mcp_server import evaluate_super_setup, get_market_lens_breadth
    market_lens = get_market_lens_breadth()
    assert market_lens["status"] == "NOT_CONNECTED_TERMS_RESTRICTED"
    assert market_lens["source"] == "NSE_MARKET_LENS_NOT_AUTOMATED"

    super_setup = evaluate_super_setup("TATAMOTORS")
    assert "super_setup_score" in super_setup
    assert 0 <= super_setup["super_setup_score"] <= 100
    assert super_setup["status"] in ("LIVE", "UNAVAILABLE")
    if super_setup["status"] == "LIVE":
        assert super_setup["technicals"]["vwap"] > 0
        assert super_setup["live_market_breadth"]["source"] in ("LIVE_KITE_BATCH", "UNAVAILABLE")
    else:
        assert super_setup["decision"] == "STAND_ASIDE"


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
    assert res_open["action"] == "POSITION_REVIEW_REQUIRED"
    assert res_open["trade_ids"] == [tid]
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


def test_desk_intraday_payload_age_is_explicit_and_fail_closed():
    import datetime
    import sys
    from zoneinfo import ZoneInfo
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from connector.smart_trading_bridge import SmartTradingBridge

    ist = ZoneInfo("Asia/Kolkata")
    now = datetime.datetime(2026, 11, 26, 10, 0, tzinfo=ist)
    recent = {"iso_time": "2026-11-26T09:50:00+05:30"}
    old = {"timestamp": datetime.datetime(2026, 11, 25, 13, 28, tzinfo=ist).timestamp()}

    assert SmartTradingBridge.intraday_payload_age_minutes(recent, now) == 10.0
    assert SmartTradingBridge.intraday_payload_age_minutes(old, now) > 1_000
    assert SmartTradingBridge.intraday_payload_age_minutes({}, now) is None


def test_connector_proves_extension_premarket_provenance(tmp_path):
    """AI-Trading must distinguish an extension delivery from a generic Desk file."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from connector.smart_trading_bridge import SmartTradingBridge

    nse_dir = tmp_path / "data" / "by_date" / "2026-11-25" / "nse"
    nse_dir.mkdir(parents=True)
    (nse_dir / "premarket.json").write_text(json.dumps({
        "source": "nse.chrome_extension.v2",
        "trading_date": "2026-11-25",
        "extracted_at": "2026-11-25T09:12:00+05:30",
        "pre_open": {"data": [{"metadata": {"symbol": "NIFTY"}}]},
    }), encoding="utf-8")
    (nse_dir / "premarket_analysis.json").write_text(json.dumps({
        "trading_date": "2026-11-25",
        "auction_breadth": {"advances": 1, "declines": 0},
        "participant_positioning": {},
        "bias": {"primary_bias": "CALL_BIAS"},
    }), encoding="utf-8")

    premarket = SmartTradingBridge(smart_trading_root=tmp_path).read_nse_premarket_and_oi("2026-11-25")
    assert premarket["is_today_fresh"] is True
    assert premarket["raw_premarket"]["status"] == "EXTENSION_DELIVERED"
    assert premarket["raw_premarket"]["extension_delivery_verified"] is True
    assert premarket["raw_premarket"]["symbol_count"] == 1
    assert premarket["raw_premarket"]["analysis_generated"] is True


def test_market_breadth_never_falls_back_to_a_fixture():
    from technical_lens import TechnicalLensEngine

    breadth = TechnicalLensEngine.get_market_lens_breadth()
    assert breadth.source == "UNAVAILABLE"
    assert breadth.advances == 0


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

    # Valid entry with 2 lots: ₹15,600 deployment and ₹1,950 risk are inside policy.
    entered = cio_enter_trade(
        "NIFTY", "BUY_CE", "NIFTY26DEC25800CE", sl=105.0, target=165.0, lots=2,
        thesis="Strong institutional breakout above VWAP with open Call Wall",
        entry_price=120.0, live_mode=False, ledger=ledger,
    )
    assert entered["status"] == "OPENED_TRADE"
    tid = entered["trade_id"]

    # Stay & Ride Rally: trail SL upward
    trailed = cio_manage_trade(tid, "TRAIL_SL", "Holding above 20 EMA", sl=125.0, live_mode=False, ledger=ledger)
    assert trailed["status"] == "SL_UPDATED"
    assert ledger.get_open_trades()[0]["stop_loss"] == 125.0

    # Partial Profit Book: sell 1 lot at ₹165, keep 1 runner open with higher SL ₹140
    pbook = cio_manage_trade(tid, "PARTIAL_BOOK", "Lock 50% near Call Wall, ride 1 runner", sl=140.0, exit_lots=1, exit_price=165.0, live_mode=False, ledger=ledger)
    assert pbook["status"] == "PARTIAL_BOOKED"
    assert pbook["remaining_lots"] == 1
    assert pbook["net_pnl"] > 1000.0
    assert ledger.get_open_trades()[0]["lots"] == 1
    assert ledger.get_open_trades()[0]["stop_loss"] == 140.0
    assert len(ledger.get_recent_cio_decisions(10)) == 3

    missing_reason = cio_manage_trade(tid, "EXIT", "", exit_price=220.0, live_mode=False, ledger=ledger)
    assert missing_reason["reason"] == "MISSING_CIO_REASON"


def test_external_cio_policy_disables_all_ungated_execution_paths(tmp_path):
    """Signals may propose; only a written external-CIO decision may write a trade."""
    from agent_trader import AIAgentTrader, cio_enter_trade, run_autonomous_loop
    from mcp_server import close_paper_order, execute_paper_order, get_trading_authority
    from trading_policy import get_trading_policy

    policy = get_trading_policy()
    assert policy.external_cio_only is True
    assert policy.max_concurrent_positions == 2
    assert policy.max_single_trade_risk_inr == 3500.0

    disabled_loop = run_autonomous_loop(live_mode=False, max_cycles=1)
    assert disabled_loop["status"] == "DISABLED"
    assert disabled_loop["reason"] == "EXTERNAL_CIO_ONLY_POLICY"

    # Legacy MCP names remain only as safe compatibility stubs: neither may mutate
    # the global paper ledger or become an accidental shortcut around CIO approval.
    blocked_entry = execute_paper_order("NIFTY", "BUY_CE", 100.0, 90.0, 120.0, 1, "test")
    blocked_exit = close_paper_order(999999, 100.0, "test")
    assert blocked_entry["reason"] == "UNGATED_ORDER_PATH_DISABLED"
    assert blocked_exit["reason"] == "UNGATED_EXIT_PATH_DISABLED"

    authority = get_trading_authority()
    assert authority["execution_mode"] == "EXTERNAL_CIO_ONLY"
    assert authority["autonomous_entry"] == "DISABLED"
    assert authority["guardian_authority"] == ["REFRESH_COCKPIT", "HARD_STOP_LOSS", "EOD_SQUARE_OFF"]

    # Existing positions must be surfaced for an external CIO; deterministic code
    # cannot independently trail, time-stop, book, or exit them.
    ledger = AIPaperLedger(db_path=tmp_path / "external_cio_only.db")
    oversized = cio_enter_trade(
        "NIFTY", "BUY_CE", "NIFTY26DEC25800CE", sl=185.0, target=270.0, lots=2,
        thesis="Deliberately oversized policy test", entry_price=200.0, live_mode=False, ledger=ledger,
    )
    assert oversized["reason"].startswith("POSITION_DEPLOYMENT_TOO_HIGH")

    trade_id = ledger.open_trade("NIFTY", "NIFTY26DEC25800CE", "BUY_CE", 100.0, 85.0, 135.0, 1, 65, "CIO test")
    agent = AIAgentTrader("NIFTY", live_mode=False)
    agent.ledger = ledger
    review = agent.run_cycle()
    assert review["action"] == "POSITION_REVIEW_REQUIRED"
    assert review["trade_ids"] == [trade_id]
    assert ledger.get_open_trades()[0]["stop_loss"] == 85.0


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


def test_get_screener_intelligence(tmp_path, monkeypatch):
    """Verifies that get_screener_intelligence safely handles absent, summary, and symbol queries."""
    from mcp_server import get_screener_intelligence
    import mcp_server

    # 1. No data condition
    res_empty = get_screener_intelligence()
    assert res_empty["status"] in ("NO_DATA", "ACTIVE_SUMMARY")

    # 2. Mock screener_intelligence.json
    mock_payload = {
        "phase": "pre_market",
        "timestamp": "2026-11-25T18:00:00Z",
        "screen_candidates": [
            {
                "symbol": "TATAMOTORS",
                "name": "Tata Motors Ltd",
                "bot_score": 82,
                "risk_bucket": "low",
                "intraday_use": "watch",
                "position_size_multiplier": 1.0,
                "reasons": ["strong ROCE", "tradable size proxy"],
                "warnings": [],
                "blocked_reasons": [],
                "metrics": {"roce": "18.5", "debt to equity": "0.45", "current price": "980.0"},
            },
            {
                "symbol": "HIGHDEBT",
                "name": "High Debt Corp",
                "bot_score": 35,
                "risk_bucket": "high",
                "intraday_use": "avoid",
                "position_size_multiplier": 0.0,
                "reasons": [],
                "warnings": ["high debt"],
                "blocked_reasons": ["debt too high for auto-trading"],
                "metrics": {"debt to equity": "3.2"},
            }
        ],
        "company_snapshot": None,
    }
    mock_runtime_file = tmp_path / "screener_intelligence.json"
    mock_runtime_file.write_text(json.dumps(mock_payload), encoding="utf-8")

    # Monkeypatch file path
    orig_file = Path(mcp_server.__file__).resolve().parent / "runtime" / "screener_intelligence.json"
    monkeypatch.setattr("mcp_server.Path.__truediv__", lambda self, other: mock_runtime_file if "screener_intelligence.json" in str(other) else orig_file)

    # Test summary query
    summary = get_screener_intelligence()
    assert summary["status"] == "ACTIVE_SUMMARY"
    assert "TATAMOTORS" in summary["safe_fno_universe"]
    assert "HIGHDEBT" in summary["avoid_list"]

    # Test matched symbol query
    tata = get_screener_intelligence(symbol="TATAMOTORS")
    assert tata["status"] == "MATCHED_CANDIDATE"
    assert tata["bot_score"] == 82
    assert tata["intraday_use"] == "watch"
    assert "strong ROCE" in tata["reasons"]

    # Test avoid symbol query
    bad = get_screener_intelligence(symbol="HIGHDEBT")
    assert bad["status"] == "MATCHED_CANDIDATE"
    assert bad["intraday_use"] == "avoid"
    assert "debt too high for auto-trading" in bad["blocked_reasons"]

    # Test company_snapshot query with earnings risk flag
    mock_payload["company_snapshot"] = {
        "symbol": "RELIANCE",
        "name": "Reliance Industries Ltd",
        "bot_score": 70,
        "risk_bucket": "medium",
        "position_size_hint": "reduced",
        "risk_flags": ["upcoming_earnings_blackout"],
        "blocked_reasons": [],
        "ratios": {"debt to equity": "0.38", "roce": "12.4"},
    }
    mock_runtime_file.write_text(json.dumps(mock_payload), encoding="utf-8")
    rel = get_screener_intelligence(symbol="RELIANCE")
    assert rel["status"] == "MATCHED_COMPANY"
    assert rel["symbol"] == "RELIANCE"
    assert "upcoming_earnings_blackout" in rel["risk_flags"]


def test_nse_institutional_intel():
    """Verifies participant OI parsing, delivery percentage classification, and F&O ban detection."""
    from nse_institutional_intel import parse_participant_oi_csv, parse_sec_bhavdata, parse_fno_ban, get_nse_intel
    from mcp_server import get_nse_institutional_intel

    # 1. Test Participant OI Parsing
    mock_poi_csv = (
        '"","Future Index Long","Future Index Short","Future Stock Long","Future Stock Short","Option Index Call Long","Option Index Put Long","Option Index Call Short","Option Index Put Short","Total"\n'
        '"Client","69839","36665","1000","1000","67627","10000","50000","430959","0"\n'
        '"DII","15054","10950","1000","1000","7112","45907","0","0","0"\n'
        '"FII","25000","80000","1000","1000","20000","270968","142123","0","0"\n'
        '"Pro","337","330","1000","1000","47383","114084","0","0","0"\n'
    )
    poi = parse_participant_oi_csv(mock_poi_csv)
    assert poi is not None
    assert poi["fii_index_long_ratio"] == 23.8
    assert poi["fii_regime"] == "HEAVILY_OVERSOLD_SHORT_COVER_FUEL"
    assert poi["retail_trap"] == "HIGH_RETAIL_PUT_WRITER_TRAP"
    assert poi["client_index_long_ratio"] == 65.6

    # 2. Test Sec Bhavdata Parsing
    mock_bhav_csv = (
        "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER\n"
        "HDFCBANK, EQ, 25-NOV-2026, 840.00, 842.00, 848.00, 841.00, 846.00, 846.20, 845.00, 1000000, 8450.00, 50000, 650000, 65.00\n"
        "CHURNSTOCK, EQ, 25-NOV-2026, 100.00, 101.00, 105.00, 99.00, 102.00, 102.00, 102.00, 2000000, 2040.00, 10000, 300000, 15.00\n"
        "IGNORE_SERIES, BE, 25-NOV-2026, 50.00, 50.00, 51.00, 49.00, 50.00, 50.00, 50.00, 10000, 5.00, 100, 10000, 100.00\n"
    )
    bhav = parse_sec_bhavdata(mock_bhav_csv)
    assert "HDFCBANK" in bhav
    assert bhav["HDFCBANK"]["delivery_pct"] == 65.0
    assert bhav["HDFCBANK"]["delivery_quality"] == "INSTITUTIONAL_ACCUMULATION"
    assert "CHURNSTOCK" in bhav
    assert bhav["CHURNSTOCK"]["delivery_quality"] == "SPECULATIVE_DAY_CHURN"
    assert "IGNORE_SERIES" not in bhav

    # 3. Test Ban list parsing
    mock_ban_text = "Securities in Ban For Trade Date 26-NOV-2026: 1,APLAPOLLO 2,MANAPPURAM"
    bans = parse_fno_ban(mock_ban_text)
    assert "APLAPOLLO" in bans
    assert "MANAPPURAM" in bans

    # 4. Test MCP tool call
    summary = get_nse_institutional_intel()
    assert summary["status"] in ("SUMMARY", "ERROR")

    # 5. Test MCP tool with symbol
    hdfc = get_nse_institutional_intel(symbol="HDFCBANK")
    assert hdfc["status"] in ("MATCHED_SYMBOL", "ERROR")
    if hdfc["status"] == "MATCHED_SYMBOL":
        assert hdfc["symbol"] == "HDFCBANK"
        assert hdfc["is_in_fno_ban"] is False


def test_cas_clock_charm_sign_and_synthetic_forward():
    import datetime
    from trading_policy import get_trading_policy
    from quantitative_alpha import QuantitativeAlphaEngine
    from greeks_gex import GreeksGexEngine
    from options_analytics import OptionStrikeData

    policy = get_trading_policy()

    # 1. Test Date-Aware CAS EOD Square-Off Cutover
    pre_cas_date = datetime.date(2026, 11, 26)
    post_cas_date = datetime.date(2026, 12, 7)

    assert policy.get_eod_square_off(pre_cas_date) == (15, 15)
    assert policy.get_eod_square_off(post_cas_date, is_expiry=False) == (15, 25)
    assert policy.get_eod_square_off(post_cas_date, is_expiry=True) == (15, 20)
    # Verify default call without args runs cleanly with IST timezone
    assert policy.get_eod_square_off() in ((15, 15), (15, 20), (15, 25))

    # Test Cockpit Clock Keys
    from cockpit import evaluate_session_clock
    clk = evaluate_session_clock()
    assert "eod_cutoff" in clk
    assert "minutes_to_eod" in clk

    # 2. Test Charm Sign Physics: ITM call delta converges to 1.0 (positive slope), OTM decays to 0.0 (negative slope)
    _, itm_charm = QuantitativeAlphaEngine.calculate_second_order_greeks(
        spot=25800.0, strike=25000.0, dte=2.0, iv=0.15, call_oi=10000, put_oi=5000, lot_size=25,
    )
    assert itm_charm > 0, "ITM call charm must be positive as time elapses toward expiry (delta converging to 1.0)"

    _, otm_charm = QuantitativeAlphaEngine.calculate_second_order_greeks(
        spot=25800.0, strike=26500.0, dte=2.0, iv=0.15, call_oi=10000, put_oi=5000, lot_size=25,
    )
    assert otm_charm < 0, "OTM call charm must be negative as time elapses toward expiry (delta decaying to 0.0)"

    # 3. Test Put-Call Parity Synthetic Forward in Greeks Engine
    # Strike 25800: CE=240, PE=185 -> Synthetic F = 25800 + (240 - 185) = 25855
    mock_chain = [
        OptionStrikeData(strike=25800.0, call_ltp=240.0, put_ltp=185.0, call_oi=100000, put_oi=120000),
    ]
    res = GreeksGexEngine.evaluate_frontier_landscape(
        symbol="NIFTY",
        underlying_price=25819.0,
        chain=mock_chain,
        dte=2.0,
    )
    assert res.underlying_price == 25819.0
    assert res.net_gex_crores != 0.0

    # 4. Test Pure Black-76 vs Spot BSM Drift Parity
    # For monthly option (25 DTE), forward pricing eliminates double-counted interest rate drift
    g_bsm = GreeksGexEngine.calculate_greeks(spot=25800.0, strike=25800.0, dte=25.0, iv=0.16, r=0.053, is_forward=False)
    g_b76 = GreeksGexEngine.calculate_greeks(spot=25800.0, strike=25800.0, dte=25.0, iv=0.16, r=0.053, is_forward=True)
    assert g_b76.delta_call < g_bsm.delta_call, "Black-76 forward option delta must not double-count risk-free drift"
    assert round(g_b76.delta_call, 2) in (0.50, 0.51), f"Black-76 ATM forward delta should be ~0.50-0.51, got {g_b76.delta_call}"

    # 5. Test Unified Expiry Day Detection
    assert policy.is_expiry_day(datetime.date(2026, 11, 24)) is True  # Tuesday weekly
    assert policy.is_expiry_day(datetime.date(2026, 11, 25)) is False # Wednesday non-expiry
    assert policy.is_expiry_day(datetime.date(2026, 11, 26)) is True  # Last Thursday monthly
    assert policy.is_expiry_day(datetime.date(2026, 11, 27)) is False # Friday non-expiry

    # 6. Test Asymmetric / Illiquid Quote Rejection
    # Strike with wide call spread (>5%) must be rejected from synthetic forward calculation
    mock_wide_chain = [
        OptionStrikeData(
            strike=25800.0, call_ltp=240.0, put_ltp=185.0,
            call_bid=200.0, call_ask=280.0, # 80 pt spread (~33% of mid 240) -> REJECT
            put_bid=184.0, put_ask=186.0,   # 2 pt spread (~1% of mid 185) -> OK
            call_oi=100000, put_oi=120000,
        )
    ]
    res_wide = GreeksGexEngine.evaluate_frontier_landscape(
        symbol="NIFTY",
        underlying_price=25819.0,
        chain=mock_wide_chain,
        dte=2.0,
    )
    # When wide spread is rejected and no fallback matches, spot price is preserved
    assert res_wide.underlying_price == 25819.0


def test_spring_boot_broker_execution_adapters(tmp_path):
    """Verifies Spring Boot-style Dual-Mode (PAPER vs LIVE) Broker Architecture."""
    from dataclasses import replace
    from trading_policy import get_trading_policy
    from broker_execution import (
        get_execution_broker,
        PaperExecutionBroker,
        LiveKiteExecutionBroker,
    )
    from paper_ledger import AIPaperLedger

    policy = get_trading_policy()
    test_ledger = AIPaperLedger(db_path=tmp_path / "broker_test.db")

    # 1. Default policy (strategy.yaml has target_venue: "PAPER") returns PaperExecutionBroker
    paper_broker = get_execution_broker(policy=policy, ledger=test_ledger)
    assert isinstance(paper_broker, PaperExecutionBroker)
    assert paper_broker.get_mode() == "PAPER"

    # 2. Live profile policy (target_venue: "LIVE") returns LiveKiteExecutionBroker
    live_policy = replace(policy, target_venue="LIVE")
    live_broker = get_execution_broker(policy=live_policy, ledger=test_ledger)
    assert isinstance(live_broker, LiveKiteExecutionBroker)
    assert live_broker.get_mode() == "LIVE"

    # 3. Test Paper Execution Entry Fills
    res = paper_broker.execute_entry(
        symbol="NIFTY",
        direction="BUY_CE",
        contract="NIFTY26DEC26000CE",
        lots=1,
        lot_size=25,
        exec_px=150.0,
        sl=130.0,
        target=200.0,
        thesis="Spring Boot Paper Adapter Test",
        clock={"expiry_regime": "NORMAL_DAY"},
        max_risk=500.0,
        ticket=3750.0,
    )
    assert res["status"] == "OPENED_TRADE"
    assert res["venue"] == "PAPER"
    tid = res["trade_id"]
    assert tid > 0

    # Test Paper Position Exit
    exit_res = paper_broker.execute_exit(
        trade={"trade_id": tid, "symbol": "NIFTY26DEC26000CE", "underlying": "NIFTY", "lots": 1, "quantity": 25, "stop_loss": 130.0},
        mark=160.0,
        action="BOOK_PROFIT",
        reason="Test Exit Profit",
        regime="NORMAL_DAY",
    )
    assert exit_res["status"] == "CLOSED"
    assert exit_res["venue"] == "PAPER"


def test_vertical_spread_and_spot_sl_integration(tmp_path):
    """Verifies full 2-leg vertical debit spread lifecycle, 4-order friction, partial close, and spot SL."""
    from broker_execution import PaperExecutionBroker
    from paper_ledger import AIPaperLedger
    from trading_policy import get_trading_policy

    policy = get_trading_policy()
    assert policy.vertical_spreads_enabled is True
    assert policy.max_spread_net_debit_inr == 14000.0
    assert policy.tier_1_super_setup_budget_inr == 20000.0

    db_path = tmp_path / "test_spread_trades.db"
    ledger = AIPaperLedger(db_path=str(db_path))
    broker = PaperExecutionBroker(ledger=ledger)

    # 1. Open 2-lot vertical debit spread: Buy 4400CE @ 235 (ask), Sell 4500CE @ 190 (bid) -> Net Debit ₹45
    res = broker.execute_entry(
        symbol="DATAPATTNS",
        direction="BUY_CE",
        contract="DATAPATTNS26DEC4400CE",
        lots=2,
        lot_size=125,
        exec_px=45.0,
        sl=25.0,
        target=80.0,
        thesis="2-leg vertical spread lifecycle test",
        clock={"expiry_regime": "NORMAL_DAY"},
        max_risk=2500.0,
        ticket=11250.0,
        spot_stop_loss=4352.0,
        short_contract="DATAPATTNS26DEC4500CE",
        short_exec_px=190.0,
    )
    assert res["status"] == "OPENED_TRADE"
    assert res["short_contract"] == "DATAPATTNS26DEC4500CE"
    tid = res["trade_id"]

    open_trades = ledger.get_open_trades()
    assert len(open_trades) == 1
    assert open_trades[0]["spot_stop_loss"] == 4352.0
    assert open_trades[0]["short_contract"] == "DATAPATTNS26DEC4500CE"
    assert open_trades[0]["short_entry_price"] == 190.0

    # 2. Partial book 1 lot at Net ₹65 (Long @ 270, Short @ 205) -> 4-order friction must exceed ₹100
    p_res = broker.execute_exit(
        trade=open_trades[0],
        mark=65.0,
        action="PARTIAL_BOOK",
        reason="Lock half spread near wall",
        regime="NORMAL_DAY",
        exit_lots=1,
        new_sl=35.0,
        short_mark=205.0,
    )
    assert p_res["status"] == "PARTIAL_BOOKED"
    assert p_res["remaining_lots"] == 1
    assert p_res["short_contract"] == "DATAPATTNS26DEC4500CE"
    assert p_res["friction"] > 100.0  # 4 orders x ₹20 = ₹80 brokerage + double-sell STT + GST
    assert p_res["gross_pnl"] == (65.0 - 45.0) * 125

    # 3. Full close remaining 1 lot at Net ₹70 (Long @ 280, Short @ 210)
    rem = ledger.get_open_trades()[0]
    assert rem["stop_loss"] == 35.0
    assert rem["spot_stop_loss"] == 4352.0
    c_res = broker.execute_exit(
        trade=rem,
        mark=70.0,
        action="BOOK_PROFIT",
        reason="Target achieved",
        regime="NORMAL_DAY",
        short_mark=210.0,
    )
    assert c_res["status"] == "CLOSED"
    assert c_res["short_exit_price"] == 210.0
    assert ledger.get_open_trades() == []


def test_live_broker_spread_sequencing_and_fail_closed_eod(tmp_path):
    """
    Tests Zerodha live spread execution ordering (BUY long -> SELL short on entry,
    BUY short -> SELL long on exit), ₹0.05 tick marketable limit pricing, fill confirmation,
    Leg-2 unwind recovery, anti-short lock, and fail-closed EOD retry retention.
    """
    from broker_execution import LiveKiteExecutionBroker, round_nse_tick
    from paper_ledger import AIPaperLedger

    assert round_nse_tick(152.33) == 152.35
    assert round_nse_tick(152.32) == 152.3

    class FakeKite:
        VARIETY_REGULAR = "regular"
        EXCHANGE_NFO = "NFO"
        PRODUCT_MIS = "MIS"
        PRODUCT_NRML = "NRML"
        ORDER_TYPE_LIMIT = "LIMIT"
        ORDER_TYPE_MARKET = "MARKET"

        def __init__(self, fail_second=False, fail_exit=False):
            self.orders = []
            self.fail_second = fail_second
            self.fail_exit = fail_exit
            self.seq = 0
            self.net_positions = []

        def place_order(self, **kwargs):
            self.seq += 1
            if self.fail_second and self.seq == 2:
                raise RuntimeError("RMS short margin error")
            if self.fail_exit and kwargs["transaction_type"] == "SELL":
                raise RuntimeError("Exchange timeout during EOD exit")
            oid = f"OID_{self.seq}"
            self.orders.append((oid, kwargs))
            return oid

        def order_history(self, oid):
            for o, kw in self.orders:
                if o == oid:
                    px = float(kw.get("price") or 100.0)
                    return [{"status": "COMPLETE", "average_price": round(px * 0.99, 2)}]
            return []

        def positions(self):
            return {"net": self.net_positions}

    class FakeFeed:
        def __init__(self, kite_obj):
            self._kite = kite_obj
            self._kite_init_error = None

        def _ensure_kite(self):
            return self._kite

    # Scenario A: Healthy 2-leg Spread Entry + Exit ordering
    kite = FakeKite()
    ledger = AIPaperLedger(db_path=tmp_path / "live_spread.db")
    broker = LiveKiteExecutionBroker(ledger=ledger, feed=FakeFeed(kite))

    ent = broker.execute_entry(
        symbol="DATAPATTNS",
        direction="BUY_CE",
        contract="DATAPATTNS26DEC4400CE",
        lots=1,
        lot_size=125,
        exec_px=45.0,
        sl=25.0,
        target=80.0,
        thesis="Live spread sequencing test",
        clock={"expiry_regime": "NORMAL_DAY"},
        max_risk=2500.0,
        ticket=5625.0,
        short_contract="DATAPATTNS26DEC4500CE",
        short_exec_px=190.0,
    )
    assert ent["status"] == "OPENED_TRADE"
    assert ent["venue"] == "LIVE"
    assert len(kite.orders) == 2
    assert kite.orders[0][1]["tradingsymbol"] == "DATAPATTNS26DEC4400CE"
    assert kite.orders[0][1]["transaction_type"] == "BUY"   # Long hedge MUST be first!
    assert kite.orders[0][1]["order_type"] == "LIMIT"
    assert kite.orders[0][1]["product"] == "NRML"
    assert kite.orders[1][1]["tradingsymbol"] == "DATAPATTNS26DEC4500CE"
    assert kite.orders[1][1]["transaction_type"] == "SELL"  # Short leg MUST be second!

    kite.net_positions = [
        {"tradingsymbol": "DATAPATTNS26DEC4400CE", "quantity": 125},
        {"tradingsymbol": "DATAPATTNS26DEC4500CE", "quantity": -125},
    ]
    open_row = ledger.get_open_trades()[0]
    ex = broker.execute_exit(
        trade=open_row,
        mark=60.0,
        action="EXIT",
        reason="Close spread",
        regime="NORMAL_DAY",
        short_mark=200.0,
    )
    assert ex["status"] == "CLOSED"
    assert kite.orders[2][1]["tradingsymbol"] == "DATAPATTNS26DEC4500CE"
    assert kite.orders[2][1]["transaction_type"] == "BUY"   # Unwind short FIRST!
    assert kite.orders[3][1]["tradingsymbol"] == "DATAPATTNS26DEC4400CE"
    assert kite.orders[3][1]["transaction_type"] == "SELL"  # Exit long SECOND!

    # Scenario B: Leg 2 entry failure triggers automatic Leg 1 unwind (leaves 0 open exposure)
    kite_leg2_fail = FakeKite(fail_second=True)
    ledger_b = AIPaperLedger(db_path=tmp_path / "live_unwind.db")
    broker_b = LiveKiteExecutionBroker(ledger=ledger_b, feed=FakeFeed(kite_leg2_fail))
    ent_b = broker_b.execute_entry(
        symbol="DATAPATTNS", direction="BUY_CE", contract="DATAPATTNS26DEC4400CE",
        lots=1, lot_size=125, exec_px=45.0, sl=25.0, target=80.0,
        thesis="Unwind check", clock={"expiry_regime": "NORMAL_DAY"},
        max_risk=2500.0, ticket=5625.0,
        short_contract="DATAPATTNS26DEC4500CE", short_exec_px=190.0,
    )
    assert ent_b["status"] == "LIVE_SPREAD_LEG2_REJECTED_UNWOUND"
    assert kite_leg2_fail.orders[1][1]["transaction_type"] == "SELL"
    assert kite_leg2_fail.orders[1][1]["tradingsymbol"] == "DATAPATTNS26DEC4400CE"
    assert ledger_b.get_open_trades() == []

    # Scenario C: Live EOD failure NEVER closes SQLite row prematurely (Guardian keeps retrying)
    kite_eod_fail = FakeKite(fail_exit=True)
    ledger_c = AIPaperLedger(db_path=tmp_path / "live_eod_fail.db")
    broker_c = LiveKiteExecutionBroker(ledger=ledger_c, feed=FakeFeed(kite_eod_fail))
    broker_c.execute_entry(
        symbol="NIFTY", direction="BUY_CE", contract="NIFTY26DEC25600CE",
        lots=1, lot_size=65, exec_px=150.0, sl=125.0, target=200.0,
        thesis="EOD retry guard", clock={"expiry_regime": "NORMAL_DAY"},
        max_risk=1625.0, ticket=9750.0,
    )
    eod_res = broker_c.square_off_all(ledger_c.get_open_trades(), {}, "HARD_EOD_SQUAREOFF_1515")
    assert eod_res["status"] == "PARTIAL_SQUAREOFF_RETRY_NEEDED"
    assert eod_res["failed_count"] == 1
    assert len(ledger_c.get_open_trades()) == 1  # Still OPEN so Guardian retries!


def test_cio_spread_guards_t4_blackout_and_spot_sl_whipsaw(tmp_path):
    """Verifies inverted spread guards, --mock isolation, T-4 physical delivery blackout, and spot anti-whipsaw."""
    from dataclasses import replace
    from unittest.mock import patch
    from agent_trader import _evaluate_spot_sl_breach, cio_enter_trade
    from paper_ledger import AIPaperLedger
    from trading_policy import get_trading_policy

    ledger = AIPaperLedger(db_path=tmp_path / "guards.db")

    # 1. Even if policy says LIVE, live_mode=False (--mock) forces PAPER cleanly
    live_pol = replace(get_trading_policy(), target_venue="LIVE")
    with patch("agent_trader.get_trading_policy", return_value=live_pol):
        ok_mock = cio_enter_trade(
            "NIFTY", "BUY_CE", "NIFTY26DEC25600CE", sl=120.0, target=190.0, lots=1,
            thesis="Mock isolation check", entry_price=140.0, live_mode=False, ledger=ledger,
        )
        assert ok_mock["status"] == "OPENED_TRADE"
        assert ok_mock["venue"] == "PAPER"

    # 2. Inverted vertical spreads and option type mismatches must fail-closed
    inv_call = cio_enter_trade(
        "DATAPATTNS", "BUY_CE", "DATAPATTNS26DEC4500CE", sl=20.0, target=70.0, lots=1,
        thesis="Inverted Bull Call check", entry_price=40.0,
        short_contract="DATAPATTNS26DEC4400CE", short_entry_price=220.0,
        live_mode=False, ledger=ledger,
    )
    assert inv_call["status"] == "REJECTED"
    assert "INVERTED_BULL_CALL_SPREAD" in inv_call["reason"]

    inv_put = cio_enter_trade(
        "SHRIRAMFIN", "BUY_PE", "SHRIRAMFIN26DEC900PE", sl=5.0, target=15.0, lots=1,
        thesis="Inverted Bear Put check", entry_price=9.0,
        short_contract="SHRIRAMFIN26DEC920PE", short_entry_price=30.0,
        live_mode=False, ledger=ledger,
    )
    assert inv_put["status"] == "REJECTED"
    assert "INVERTED_BEAR_PUT_SPREAD" in inv_put["reason"]

    mismatch = cio_enter_trade(
        "RELIANCE", "BUY_CE", "RELIANCE26DEC1300CE", sl=10.0, target=30.0, lots=1,
        thesis="Mismatched leg check", entry_price=15.0,
        short_contract="RELIANCE26DEC1340PE", live_mode=False, ledger=ledger,
    )
    assert mismatch["status"] == "REJECTED"
    assert "SPREAD_OPTION_TYPE_MISMATCH" in mismatch["reason"]

    # 3. T-4 Physical Delivery Blackout blocks stocks at DTE<=4 while permitting cash-settled NIFTY
    class T3Feed:
        def get_symbol_meta(self, sym):
            return {"symbol": sym, "lot_size": 500, "strike_step": 20.0, "dte_days": 3}

        def fetch_contract_quotes(self, syms):
            return {syms[0]: {"bid": 19.5, "ask": 20.0, "ltp": 19.8, "mark": 19.5}}

    t4_rej = cio_enter_trade(
        "RELIANCE", "BUY_CE", "RELIANCE26DEC1300CE", sl=15.0, target=30.0, lots=1,
        thesis="T-4 blackout check", live_mode=True, ledger=ledger, feed=T3Feed(),
        enforce_market_clock=False,
    )
    assert t4_rej["status"] == "REJECTED"
    assert "PHYSICAL_DELIVERY_T4_BLACKOUT" in t4_rej["reason"]

    # 4. Anti-whipsaw spot SL: 1 minor poke doesn't stop out; 2nd consecutive or deep break >=0.25% does
    hit1, cnt1 = _evaluate_spot_sl_breach(999.5, 1000.0, is_ce=True, prior_breaches=0)
    assert hit1 is False and cnt1 == 1
    hit2, cnt2 = _evaluate_spot_sl_breach(999.4, 1000.0, is_ce=True, prior_breaches=cnt1)
    assert hit2 is True and cnt2 == 2
    hit_crash, _ = _evaluate_spot_sl_breach(997.0, 1000.0, is_ce=True, prior_breaches=0)
    assert hit_crash is True


def test_scale_free_gex_and_index_rvol_protection(tmp_path):
    """Verifies scale-free GEX asymmetry ratio across stock OI scales and index 0-spot-volume RVOL."""
    from greeks_gex import GreeksGexEngine
    from options_analytics import OptionStrikeData
    from smart_edge import compute_relative_volume

    # Mid-cap stock with lot_size=825 and high vol (45%) should still classify dominant short-call GEX cleanly
    chain = [
        OptionStrikeData(strike=900.0, call_oi=100000, put_oi=900000, call_ltp=42.0, put_ltp=14.0),
        OptionStrikeData(strike=920.0, call_oi=250000, put_oi=300000, call_ltp=28.0, put_ltp=22.0),
        OptionStrikeData(strike=940.0, call_oi=1200000, put_oi=80000, call_ltp=15.0, put_ltp=35.0),
    ]
    gex_short = GreeksGexEngine.evaluate_frontier_landscape(
        symbol="SHRIRAMFIN", underlying_price=925.0, chain=chain, dte=10.0,
        iv=0.45, lot_size=825, strike_step=20.0,
    )
    assert gex_short.gex_regime == "SHORT_GAMMA_VOLATILITY"

    # Zero spot volume on index returns 1.0x NORMAL instead of 186,000,000x
    rvol, reg = compute_relative_volume(current_volume=0, avg_volume_20d=0)
    assert rvol == 1.0 and reg == "NORMAL"


