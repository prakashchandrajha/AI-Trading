# External CIO Morning Routine & Master Runbook

This runbook is the permanent operating procedure for **AI-Trading** and **SmartTrading**. 
Even if the chat session is deleted, a fresh agent reading this document and `AGENTS.md` will know **exactly** what to do.

---

## 1. Terminal Startup (08:50 AM – 09:05 AM IST)

Open two terminal tabs:

### Terminal 1: SmartTrading (Autonomous Scalper Desk)
```bash
cd /home/vvd/prakash/SmartTrading/desk
./run_scalper.sh
```
* **What it does**: Verifies Zerodha Kite authentication, clears volatile LTP cache, launches Kite WebSocket ticker into `/dev/shm/smart_trading`, and opens Port `9192` for the Desk UI and Pre-Market Extension.

### Terminal 2: AI-Trading (External CIO Guardian & Screener Bridge)
```bash
cd /home/vvd/prakash/AI-Trading
# Ensure screener bridge is running in background:
./ai/run_morning_ai_trader.sh screener-server &

# Start the risk guardian:
./ai/run_morning_ai_trader.sh start
```
* **What it does**: Ensures Port `8776` Screener bridge is up, starts the background risk guardian to enforce emergency Stop-Loss and the 15:15 IST EOD paper square-off.

---

## 2. In Chrome Browser (09:05 AM – 09:08 AM IST)

1. **NSE Pre-Market Extension** (Port `9192`):
   - Click the extension icon and click **"Force Fetch Pre-Market Now"** (or let it auto-poll).
   - It captures the 09:00–09:08 AM pre-open call auction and participant OI.
2. **Screener Extension** (Port `8776`):
   - Click the extension icon and click **"Queue F&O Leaders Screen"** (or run `./ai/run_morning_ai_trader.sh screener-scan`).

---

## 3. In a FRESH AI Chat (09:08 AM – 09:15 AM IST)

In this chat window (or ANY fresh chat window), type just ONE sentence:

> **`Start paper trading as External CIO for today`**

---

## 4. What the AI Agent Will Automatically Do (Step-by-Step)

1. **Step 1: Authority & Guardian Verification**:
   - Calls `get_trading_authority()` to confirm trading mode is `ACTIVE` and guardian is alive.
2. **Step 2: Pre-Market Market Intelligence**:
   - Calls `get_premarket_intelligence()`:
     - Verifies `EXTENSION_DELIVERED` status from today's pre-open call auction.
     - Checks advances vs declines breadth and Indicative Equilibrium Prices (IEP).
3. **Step 3: Institutional Clearing & Option Trap Check**:
   - Calls `get_nse_institutional_intel()`:
     - Audits **FII Index Long Ratio** (oversold vs overbought).
     - Checks **Retail Put-Writer Trap** (Client Net Puts vs FII Net Puts).
     - Checks **F&O Ban List** to avoid banned symbols.
4. **Step 4: Fundamental Quality & Earnings Blackout Check**:
   - Calls `get_screener_intelligence()`:
     - Validates Debt/Equity < 1.0 and ROCE > 15%.
     - Verifies candidate stock does NOT have imminent earnings results (preventing IV crush).
5. **Step 5: Cockpit Briefing & Live Market Lens**:
   - Calls `get_cockpit_briefing()` and `get_live_market_breadth()` at 09:15 AM to inspect live opening flow.
6. **Step 6: Opening Decision**:
   - Decides either **`STAND_ASIDE`** (if market is choppy/conflicted) OR authorises a high-conviction trade.
   - For any entry, calls **`cio_authorize_entry`** with written thesis, contract, stop-loss, target, and position sizing.
7. **Step 7: Trade Management**:
   - Reassesses open positions continuously and calls **`cio_manage_position`** to trail stops, take partial profits, or exit.
