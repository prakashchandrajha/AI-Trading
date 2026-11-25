#!/usr/bin/env bash
# ==============================================================================
# ai/run_morning_ai_trader.sh — Autonomous AI Agent CIO Morning Paper Trading Launcher
# Dedicated Runner for Trader 3 (Antigravity Multi-Sector F&O Options)
#
# Usage:
#   ./ai/run_morning_ai_trader.sh start      # Boots live paper trading daemon
#   ./ai/run_morning_ai_trader.sh status     # Check active positions & ledger summary
#   ./ai/run_morning_ai_trader.sh stop       # Gracefully squares off all positions into cash
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
        echo "🦅 SMART TRADING: ANTIGRAVITY AI AGENT CIO (TRADER 3)"
        echo "   Demat Budget: ₹50,000.00 | Sizing: Dynamic 1 to N Lots | Mode: SHADOW"
        echo "   Strategy: VWAP + 9/40 EMA + OFI Z-Score + 6 Generals Composite"
        echo "   Acceleration: NVIDIA RTX 5050 GPU Monte Carlo (140k paths) + BS Greeks"
        echo "=============================================================================="
        cd "$ROOT_DIR"
        mkdir -p ai/logs ai/runtime

        if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            echo "⚠️ [AI AGENT CIO] Already running under PID $(cat "$PID_FILE")."
            exit 0
        fi

        nohup $PYTHON -u ai/agent_trader.py loop >> "$LOG_FILE" 2>&1 &
        AI_PID=$!
        echo "$AI_PID" > "$PID_FILE"
        echo "✅ [AI AGENT CIO] Successfully booted in background (PID: $AI_PID)"
        echo "   Logs streaming to: ${LOG_FILE}"
        echo "   Monitor status with: ./ai/run_morning_ai_trader.sh status"
        ;;
    status)
        echo "📊 [AI AGENT CIO STATUS]"
        cd "$ROOT_DIR"
        if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            echo "  • Daemon Process:   🟢 RUNNING (PID: $(cat "$PID_FILE"))"
        else
            echo "  • Daemon Process:   🔴 STOPPED"
        fi
        $PYTHON ai/agent_trader.py status
        if [ -f "$LOG_FILE" ]; then
            echo ""
            echo "📜 [RECENT LOGS (Last 10 lines)]:"
            tail -n 10 "$LOG_FILE" || true
        fi
        ;;
    stop)
        echo "🛑 [AI AGENT CIO STOP] Stopping AI Agent and squaring off all positions into cash..."
        cd "$ROOT_DIR"
        if [ -f "$PID_FILE" ]; then
            TARGET_PID=$(cat "$PID_FILE")
            if kill -0 "$TARGET_PID" 2>/dev/null; then
                kill "$TARGET_PID" 2>/dev/null || true
                echo "  • Terminated Daemon PID $TARGET_PID"
            fi
            rm -f "$PID_FILE"
        fi
        $PYTHON ai/agent_trader.py square-off
        echo "✅ [AI AGENT CIO] Fully stopped and squared off."
        ;;
    *)
        echo "Usage: $0 {start|status|stop}"
        exit 1
        ;;
esac
