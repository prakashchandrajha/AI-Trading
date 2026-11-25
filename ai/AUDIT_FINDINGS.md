# AI Agent Audit & Upgrade Log

## Current Live Status (15:02 IST)
- **Process**: Running genuine **LIVE_KITE** fail-closed daemon (`PID 29181`, streaming to `ai/logs/agent_trader.log`).
- **Ledger**: `ai/ai_paper_trades.db` (legacy synthetic open trades `#12`, `#14`, `#20` voided cleanly; `LIVE_KITE` separated from historical `SHADOW`/`REPLAY`).
- **Active Live Positions (2 / ₹50,000 budget, ₹44,050 deployed, ₹5,950 cash buffer)**:
  - `#21 [LIVE_KITE] HAL26DEC4900CE`: 1 Lot (150 qty) | Entry ₹188.80 (Ask) | SL ₹168.80 (-10.6%, max risk ₹3,000)
  - `#22 [LIVE_KITE] TATASTEEL26DEC157.5PE`: 1 Lot (2750 qty) | Entry ₹5.72 (Ask) | SL ₹4.69 (-18.0%)
- **Tests**: `pytest ai/test_ai_agent.py` — **11/11 passed**.
- **Mandatory EOD**: `15:15 IST` hard square-off at live market Bid/LTP.

---

## Completed Fixes (Implemented 14:54–15:02 IST)
1. **Real Live Market Feed (`ai/live_kite_feed.py`)**: Created read-only Zerodha Kite `quote()` + 5m `historical_data()` + `instruments.json` resolver (picks freshest `.kite_session`, maps real NFO contracts, real lot sizes e.g. HAL 150 / TATASTEEL 2750, real bid/ask depth, zero `desk` imports).
2. **Removed Synthetic `+2.5` Offset & Static Fallback (`ai/agent_trader.py`)**: `live_mode=True` fails closed (`STAND_ASIDE`) when live quotes are missing and marks open trades at real live Bid/LTP every 5s.
3. **20 EMA Triple Ribbon + Real VWAP (`ai/technical_lens.py`)**: Added real `ema_20`, `+10` score bonus on `9 > 20 > 40` / `9 < 20 < 40`, live VWAP `average_price` override, and 20-EMA profit lock defense.
4. **30-Min Cooldown & Dynamic Universe Sorting (`ai/agent_trader.py`)**: Sorts active `instruments.json` NFO symbols dynamically (`TATAMOTORS` replaced by `TMPV`); enforces 30m cooldown after 45m time-stops.
5. **Risk-Budgeted SL Cap (`ai/agent_trader.py`)**: Caps SL % so single-trade risk never exceeds `₹3,000` (protecting `-₹3,500` daily circuit breaker even on ₹28k 1-lot tickets).
6. **IST Ledger Filtering & Tee Logging (`ai/paper_ledger.py`, `ai/agent_trader.py`)**: Added `Asia/Kolkata` filtering, `void_synthetic_open_trades()`, `LIVE_KITE` status separation, and internal `_TeeStream` logging to `ai/logs/agent_trader.log`.

---

## Minor Remaining Refinements (Post-Market)
1. **Monte Carlo Horizon Calibration (`ai/gpu_engine.py`)**: Adjust `PoT Target` simulation horizon so `PoT` does not display `0.0%` against distant monthly Call/Put walls.
2. **CLI Scanner Subcommands**: Optionally pass `live_mode=True` to `scan-fo`, `super-setup`, `generals`, `sectors`, `radar`, `quant`.
3. **Cosmetic Print Sign**: Format negative unrealized MTM as `-₹345.00` instead of `₹-345.00`.
4. **Live Tick OI Delta**: `quote()` provides session `oi`; accumulate rolling 15-min delta snapshots cleanly as daemon stays up from 09:15 AM.
