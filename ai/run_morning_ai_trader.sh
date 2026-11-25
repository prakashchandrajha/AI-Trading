#!/usr/bin/env bash
# ==============================================================================
# ai/run_morning_ai_trader.sh — AI CIO Reflex Guardian & Cockpit Refresher
# Companion to Antigravity AI Trader (Decisions stay strictly in Antigravity's hands)
#
# Usage:
#   ./ai/run_morning_ai_trader.sh start       # Starts Reflex Guardian + Cockpit Refresher
#   ./ai/run_morning_ai_trader.sh cockpit     # Prints fresh lightweight AI Cockpit view
#   ./ai/run_morning_ai_trader.sh logic-test  # Runs continuous anti-curve-fit Logic Test
#   ./ai/run_morning_ai_trader.sh status      # Check daemon status & LIVE_KITE ledger
#   ./ai/run_morning_ai_trader.sh stop        # Stops guardian and squares off into cash
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON="python3"

COMMAND="${1:-start}"

PID_FILE="${ROOT_DIR}/ai/runtime/agent_trader.pid"
LOG_FILE="${ROOT_DIR}/ai/logs/agent_trader.log"

case "$COMMAND" in
    start)
        echo "=============================================================================="
        echo "🛡️ SMART TRADING: ANTIGRAVITY AI CIO — REFLEX GUARDIAN + COCKPIT"
        echo "   Authority Split: Background guards hard SL & 15:15 EOD + refreshes Cockpit"
        echo "   All Entries, Trails, Profit Bookings & Exits: STRICTLY ANTIGRAVITY CIO"
        echo "=============================================================================="
        cd "$ROOT_DIR"
        mkdir -p ai/logs ai/runtime

        if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            echo "⚠️ [GUARDIAN] Already running under PID $(cat "$PID_FILE")."
            exit 0
        fi

        nohup $PYTHON -u ai/agent_trader.py guardian >> "$LOG_FILE" 2>&1 &
        AI_PID=$!
        echo "$AI_PID" > "$PID_FILE"
        echo "✅ [GUARDIAN] Successfully booted in background (PID: $AI_PID)"
        echo "   Logs streaming to: ${LOG_FILE}"
        echo "   Inspect cockpit: ./ai/run_morning_ai_trader.sh cockpit"
        ;;
    cockpit)
        cd "$ROOT_DIR"
        $PYTHON ai/agent_trader.py cockpit
        ;;
    logic-test)
        cd "$ROOT_DIR"
        $PYTHON ai/agent_trader.py logic-test
        ;;
    status)
        echo "📊 [AI CIO GUARDIAN STATUS]"
        cd "$ROOT_DIR"
        if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            echo "  • Guardian Process: 🟢 RUNNING (PID: $(cat "$PID_FILE"))"
        else
            echo "  • Guardian Process: 🔴 STOPPED"
        fi
        $PYTHON ai/agent_trader.py status
        if [ -f "$LOG_FILE" ]; then
            echo ""
            echo "📜 [RECENT LOGS (Last 10 lines)]:"
            tail -n 10 "$LOG_FILE" || true
        fi
        ;;
    stop)
        echo "🛑 [GUARDIAN STOP] Stopping Guardian and squaring off any open positions into cash..."
        cd "$ROOT_DIR"
        if [ -f "$PID_FILE" ]; then
            TARGET_PID=$(cat "$PID_FILE")
            if kill -0 "$TARGET_PID" 2>/dev/null; then
                kill "$TARGET_PID" 2>/dev/null || true
                echo "  • Terminated Guardian PID $TARGET_PID"
            fi
            rm -f "$PID_FILE"
        fi
        $PYTHON ai/agent_trader.py square-off
        echo "✅ [GUARDIAN] Fully stopped and squared off."
        ;;
    *)
        echo "Usage: $0 {start|cockpit|logic-test|status|stop}"
        exit 1
        ;;
esac
