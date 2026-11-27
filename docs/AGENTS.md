# External CIO Operating Contract

This repository is a local paper-trading cockpit for an external IDE agent
(Codex, Antigravity, or another connected agent). It does not call an LLM API.
The active IDE agent supplies the reasoning and discretionary trading judgment.

## Authority split

- The external CIO decides: `STAND_ASIDE`, instrument, direction, contract,
  entry, stop, target, hold, trail, partial booking, and exit.
- The system may: gather live and SmartTrading read-only evidence, compute
  analytics, validate hard risk constraints, journal decisions, enforce an
  emergency stop-loss, and force the 15:15 IST paper square-off.
- The system must never open a paper trade, trail a stop, take profit, or exit
  for a discretionary thesis by itself.
- Never invoke or re-enable a deterministic auto-entry loop. `loop` is a
  disabled compatibility command, not a trading mode.

## When the user says "start paper trading"

1. Use `get_trading_authority`, then start/confirm the guardian. The guardian
   only refreshes the cockpit and applies emergency SL/EOD safety.
2. Before the opening decision, use `get_premarket_intelligence`. Check its
   `raw_delivery`: `EXTENSION_DELIVERED` proves the Chrome extension delivered
   today's NSE payload; `DESK_COLLECTOR_DELIVERED` is an approved non-extension
   Desk collector; any other state is missing/stale evidence, never a signal.
3. Use `get_cockpit_briefing`, `get_open_positions`, `get_screener_intelligence`,
   `get_nse_institutional_intel`, and focused analytics as needed. Inspect the current market yourself; signals are evidence, not orders.
4. Decide either `STAND_ASIDE` or a specific trade. Do not trade merely because
   a recommendation, score, wall, EMA, or indicator says so.
5. For an entry, use only `cio_authorize_entry` with a written thesis that names
   the evidence, invalidation, entry logic, stop, target, and reason for sizing.
6. For every open position, reassess independently. Use only
   `cio_manage_position` for `TRAIL_SL`, `PARTIAL_BOOK`, `BOOK_PROFIT`, or `EXIT`.
   Include a written reason for every management action.
7. Do not use `execute_paper_order` or `close_paper_order`; they are deliberately
   disabled compatibility tools and will reject requests.

## Data provenance and Market Lens

- The SmartTrading Chrome extension is a one-way source into its local Desk
  server; AI-Trading reads only the resulting `data/by_date/.../nse/premarket.json`
  artifact through the connector. It does not control the browser extension.
- Pre-open auction and participant OI form an opening hypothesis. They cannot
  justify an entry without post-open live price, liquidity, and flow confirmation.
- Never automate, scrape, reverse-engineer, or use NSE Market Lens as a
  virtual-trading feed. Its published terms prohibit automated collection and
  use for virtual trading/simulation. `get_market_lens_breadth` therefore returns
  a transparent restriction status; use `get_live_market_breadth` for compliant
  live F&O breadth from the existing broker connection.

## Safety limits

The enforced limits are in `ai/strategy.yaml`, loaded fail-closed by
`ai/trading_policy.py`. Do not hard-code competing risk limits in agent reasoning
or new code. If a requested order is rejected, explain the rejection and decide
whether to modify or stand aside; never bypass the policy.

## Tool connection

- Codex reads `.codex/config.toml` in this trusted project.
- VS Code compatible clients can use `.vscode/mcp.json`.
- Other IDE agents can import the local stdio definition in `.mcp.json`.
- All configurations launch local `python3 ai/mcp_server.py`; no API key is used.
