# AI-Trading: Frontier External CIO Trading Cockpit

An institutional-grade, fail-closed local paper-trading cockpit for Indian Equity Derivatives (NSE/BSE F&O). The system combines live Zerodha Kite streaming, SmartTrading desk telemetry, rigorous mathematical options physics, and an external IDE Agent Discretionary Chief Investment Officer (CIO) operating under strict regulatory and risk guardrails.

---

## 📊 Live Trading Session Scorecard (Thursday, November 26, 2026)

### **Executive Summary**
- **Session Regime:** NSE Expiry Day (Monthly/Weekly Settlement & Gamma Session)
- **Account Budget:** ₹50,000 Demat Capital
- **Single-Trade Budget Ceiling:** ₹18,000 (`tier_1_super_setup_budget_inr`)
- **Single-Trade Risk Cap:** ₹3,500
- **Daily Circuit Breaker:** ₹3,500 maximum loss
- **EOD State:** **0 Open Positions (100% Cash Preserved)**
- **Total Realized Net P&L:** **+₹131.65 🟢 GREEN** (All statutory frictions & taxes accounted)

### **Trade-by-Trade Performance**

| Trade ID | Underlying & Contract | Side | Lots (Qty) | Entry Price | Exit Price | Net P&L (₹) | Result | Exit Rationale / Safety Action |
| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| **#23** | `HDFCBANK26DEC860CE` | BUY_CE | 1 (650) | ₹24.15 | ₹24.05 | **-₹148.65** | 🔴 SCRATCH | Trailed SL to breakeven (₹24.15); exited on 1-tick bid dip by Guardian (`GUARDIAN_HARD_SL_HIT`). Friction ₹83.65. |
| **#24** | `HDFCBANK26DEC870CE` | BUY_CE | 1 (650) | ₹22.35 | ₹24.60 | **+₹1,378.22** | 🟢 WIN | Peaked at ₹25.00 (+11.9%); discretionary profit booking executed at ₹24.60 via `cio_manage_position` (`CIO_BOOK_PROFIT`). |
| **#25** | `BHARTIARTL26DEC2160CE` | BUY_CE | 1 (475) | ₹36.55 | ₹33.50 | **-₹1,534.26** | 🔴 CONTROLLED | Spot pulled back to VWAP; aggressive SL trail from ₹29.65 up to ₹33.50 saved ₹1,740 in risk (`GUARDIAN_HARD_SL_HIT`). |
| **#26** | `TATASTEEL26DEC165CE` | BUY_CE | 1 (2750) | ₹5.82 | ₹6.01 | **+₹436.34** | 🟢 WIN | Spot broke above ₹163 resistance with 12M+ call short-covering; pre-15:00 profit booked (`CIO_BOOK_PROFIT`), turning day net green. |

### **Key Takeaways & Discipline Highlights**
1. **Capital Preservation:** Even with a controlled loss on Trade #25, aggressive stop-loss trailing prevented a full drawdown.
2. **Turnaround Execution:** Trade #26 leveraged high-conviction order flow and call short-covering on Tata Steel, recovering losses and closing the day in net profit.
3. **Mandatory EOD Flat:** All positions were squared off ahead of the mandatory 15:15 IST deadline, with zero overnight holding risk.

---

## 🏛️ System Architecture & Authority Split

As detailed in `AGENTS.md`, the repository enforces a strict, fail-closed separation of concerns:

- **The External CIO (Active IDE Agent):**
  - Discretionary decision-maker: `STAND_ASIDE`, instrument selection, direction (`BUY_CE` / `BUY_PE`), strike selection, entry timing, stop-loss adjustments, profit targets, trailing rules, and exits.
  - Authorized entry tool: `cio_authorize_entry` with mandatory thesis logging.
  - Active management tool: `cio_manage_position` (`TRAIL_SL`, `PARTIAL_BOOK`, `BOOK_PROFIT`, `EXIT`).
  - Compatibility legacy tools (`execute_paper_order`, `close_paper_order`) and deterministic auto-execution loops are permanently disabled.

- **The Local System & Reflex Guardian:**
  - Ingests live Kite Connect market feeds and SmartTrading desk telemetry.
  - Validates hard risk gates (Demat ticket budgets, spread checks, max risk limits, F&O ban checks).
  - Background Guardian daemon (`run_morning_ai_trader.sh guardian`) continuously monitors open trades to enforce emergency hard stop-loss breaches and execute the 15:15 IST EOD cash square-off.

---

## 📁 Repository Layout

```
.
├── AGENTS.md                             # Operating contract & authority split for External CIO
├── AI Trading Architecture Deep Audit.md # Institutional v2.0 deep audit & regulatory roadmap
├── MORNING_ROUTINE.md                    # Pre-market checklist and session launch sequence
├── README.md                             # Project overview and live trading records
├── ai/
│   ├── agent_trader.py                   # Core trading engine, session clock, and Guardian daemon
│   ├── ai_paper_trades.db                # SQLite ledger of paper trades and audit trail
│   ├── cockpit.py                        # Live market telemetry aggregator and briefing engine
│   ├── live_kite_feed.py                 # Live Zerodha Kite Connect WebSocket & REST feed
│   ├── mcp_server.py                     # Local MCP tool interface for IDE agents
│   ├── nse_institutional_intel.py        # Participant OI, institutional positioning & GEX models
│   ├── options_analytics.py              # Black-Scholes/Black-76 options math and Greeks
│   ├── strategy.yaml                     # Strategy parameters, risk caps, and session clock limits
│   ├── test_ai_agent.py                  # Full pytest regression suite (22 tests passing)
│   └── trading_policy.py                 # Policy engine enforcing hard risk and Demat limits
├── connector/
│   └── smart_trading_bridge.py           # Connector bridge to SmartTrading desk artifacts
└── project backup/                       # Offline project archives and git bundles
```

---

## 🚀 Running the Cockpit

1. **Verify Regression Test Suite:**
   ```bash
   pytest ai/test_ai_agent.py
   ```

2. **Launch Reflex Guardian Daemon:**
   ```bash
   python3 -u ai/agent_trader.py guardian
   ```

3. **Start Local MCP Server:**
   ```bash
   python3 ai/mcp_server.py
   ```

4. **Session Diagnostics & Inspection:**
   ```bash
   python3 ai/cockpit.py
   ```
