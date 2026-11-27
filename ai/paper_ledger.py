"""
Independent AI Paper Trading Ledger
Calculates exact Indian F&O statutory friction:
- 0.15% STT on option sell turnover
- NSE exchange turnover fees
- SEBI turnover charges
- Stamp duty
- Brokerage & 18% GST
Logs all trades in ai/ai_paper_trades.db (100% separate from desk/).
"""
from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional


DB_PATH = Path(__file__).parent / "ai_paper_trades.db"


class AIPaperLedger:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_paper_trades (
                    trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_timestamp TEXT NOT NULL,
                    exit_timestamp TEXT,
                    underlying TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,         -- BUY_CE or BUY_PE
                    entry_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    target REAL NOT NULL,
                    lots INTEGER NOT NULL,
                    quantity INTEGER NOT NULL,
                    status TEXT NOT NULL,            -- OPEN, CLOSED
                    exit_price REAL,
                    gross_pnl REAL,
                    statutory_friction REAL,
                    net_pnl REAL,
                    exit_reason TEXT,
                    thesis TEXT,
                    source_tag TEXT DEFAULT 'REPLAY' -- REPLAY, SHADOW, LIVE
                )
                """
            )
            cols = [c[1] for c in conn.execute("PRAGMA table_info(ai_paper_trades)").fetchall()]
            if "source_tag" not in cols:
                conn.execute("ALTER TABLE ai_paper_trades ADD COLUMN source_tag TEXT DEFAULT 'REPLAY'")
            if "spot_stop_loss" not in cols:
                conn.execute("ALTER TABLE ai_paper_trades ADD COLUMN spot_stop_loss REAL")
            if "short_contract" not in cols:
                conn.execute("ALTER TABLE ai_paper_trades ADD COLUMN short_contract TEXT")
            if "short_entry_price" not in cols:
                conn.execute("ALTER TABLE ai_paper_trades ADD COLUMN short_entry_price REAL")
            if "short_exit_price" not in cols:
                conn.execute("ALTER TABLE ai_paper_trades ADD COLUMN short_exit_price REAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_cio_decisions (
                    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_ist TEXT NOT NULL,
                    action TEXT NOT NULL,         -- ENTER, STAND_ASIDE, TRAIL_SL, PARTIAL_BOOK, BOOK_PROFIT, EXIT
                    trade_id INTEGER,
                    underlying TEXT,
                    contract TEXT,
                    regime TEXT,
                    reasoning TEXT NOT NULL,
                    context_json TEXT
                )
                """
            )
            conn.commit()

    @staticmethod
    def _compute_statutory_fees(
        buy_turnover: float,
        sell_turnover: float,
        order_count: int = 2,
    ) -> float:
        """Single source of truth for Indian NSE NFO option statutory fees & taxes."""
        total_turnover = max(0.0, buy_turnover) + max(0.0, sell_turnover)
        stt = float(round(0.0015 * max(0.0, sell_turnover)))
        exch_turnover_fee = 0.0003553 * total_turnover
        stamp_duty = 0.00003 * max(0.0, buy_turnover)
        sebi_fee = 0.000001 * total_turnover
        brokerage = 20.0 * max(1, order_count)
        gst = 0.18 * (brokerage + exch_turnover_fee)
        return round(stt + exch_turnover_fee + stamp_duty + sebi_fee + brokerage + gst, 2)

    @staticmethod
    def calculate_indian_friction(
        entry_price: float,
        exit_price: float,
        quantity: int,
        direction: str = "BUY",
    ) -> float:
        """Calculates exact Indian F&O Option round-trip transaction charges (2 orders)."""
        if quantity <= 0:
            return 0.0

        buy_price = max(0.0, entry_price if direction.startswith("BUY") else exit_price)
        sell_price = max(0.0, exit_price if direction.startswith("BUY") else entry_price)
        return AIPaperLedger._compute_statutory_fees(
            buy_turnover=buy_price * quantity,
            sell_turnover=sell_price * quantity,
            order_count=2,
        )

    @staticmethod
    def calculate_vertical_spread_friction(
        long_entry: float,
        long_exit: float,
        short_entry: float,
        short_exit: float,
        quantity: int,
    ) -> float:
        """
        Calculates exact 4-order statutory friction for a vertical debit spread:
        - Buy Long entry + Buy Short exit -> Stamp duty applies to both buy turnovers.
        - Sell Short entry + Sell Long exit -> 0.15% STT applies to BOTH sell turnovers.
        - 4 orders x ₹20 = ₹80 brokerage + 18% GST + NSE turnover on full 4-leg value.
        """
        if quantity <= 0:
            return 0.0
        buy_turnover = (max(0.0, long_entry) + max(0.0, short_exit)) * quantity
        sell_turnover = (max(0.0, short_entry) + max(0.0, long_exit)) * quantity
        return AIPaperLedger._compute_statutory_fees(
            buy_turnover=buy_turnover,
            sell_turnover=sell_turnover,
            order_count=4,
        )

    def open_trade(
        self,
        underlying: str,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        target: float,
        lots: int,
        lot_size: int = 25,
        thesis: str = "",
        source_tag: str = "SHADOW",
        spot_stop_loss: Optional[float] = None,
        short_contract: Optional[str] = None,
        short_entry_price: Optional[float] = None,
    ) -> int:
        quantity = lots * lot_size
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO ai_paper_trades (
                    entry_timestamp, underlying, symbol, direction, entry_price,
                    stop_loss, target, lots, quantity, status, thesis, source_tag,
                    spot_stop_loss, short_contract, short_entry_price
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?)
                """,
                (
                    now_iso,
                    underlying,
                    symbol,
                    direction,
                    entry_price,
                    stop_loss,
                    target,
                    lots,
                    quantity,
                    thesis,
                    source_tag,
                    spot_stop_loss,
                    short_contract,
                    short_entry_price,
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def update_stop_loss(self, trade_id: int, new_stop_loss: float) -> bool:
        """Trails or tightens stop loss for an open trade."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE ai_paper_trades SET stop_loss = ? WHERE trade_id = ? AND status = 'OPEN'",
                (new_stop_loss, trade_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def detach_short_leg(
        self,
        trade_id: int,
        new_entry_price: float,
        new_stop_loss: Optional[float] = None,
    ) -> bool:
        """Converts an open spread row to single-leg long after short hedge leg was bought back separately."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            if new_stop_loss is not None:
                cursor.execute(
                    """
                    UPDATE ai_paper_trades
                    SET short_contract = NULL, short_entry_price = NULL, entry_price = ?, stop_loss = ?
                    WHERE trade_id = ? AND status = 'OPEN'
                    """,
                    (new_entry_price, new_stop_loss, trade_id),
                )
            else:
                cursor.execute(
                    """
                    UPDATE ai_paper_trades
                    SET short_contract = NULL, short_entry_price = NULL, entry_price = ?
                    WHERE trade_id = ? AND status = 'OPEN'
                    """,
                    (new_entry_price, trade_id),
                )
            conn.commit()
            return cursor.rowcount > 0

    def close_trade(
        self,
        trade_id: int,
        exit_price: float,
        exit_reason: str,
        short_exit_price: Optional[float] = None,
    ) -> Dict:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM ai_paper_trades WHERE trade_id = ?", (trade_id,))
            row = cursor.fetchone()
            if not row or row["status"] != "OPEN":
                return {}

            entry_price = float(row["entry_price"])
            quantity = int(row["quantity"])
            direction = row["direction"]
            short_contract = row["short_contract"] if "short_contract" in row.keys() else None
            short_entry = float(row["short_entry_price"] or 0.0) if "short_entry_price" in row.keys() else 0.0

            gross_pnl = round((exit_price - entry_price) * quantity, 2)
            recorded_short_exit = None
            if short_contract:
                short_ex = float(short_exit_price) if short_exit_price is not None else short_entry
                long_entry = entry_price + short_entry
                long_exit = exit_price + short_ex
                friction = self.calculate_vertical_spread_friction(
                    long_entry=long_entry,
                    long_exit=long_exit,
                    short_entry=short_entry,
                    short_exit=short_ex,
                    quantity=quantity,
                )
                recorded_short_exit = round(short_ex, 2)
            else:
                friction = self.calculate_indian_friction(entry_price, exit_price, quantity, direction)
            net_pnl = round(gross_pnl - friction, 2)

            cursor.execute(
                """
                UPDATE ai_paper_trades
                SET exit_timestamp = ?, exit_price = ?, gross_pnl = ?,
                    statutory_friction = ?, net_pnl = ?, exit_reason = ?, status = 'CLOSED',
                    short_exit_price = ?
                WHERE trade_id = ?
                """,
                (now_iso, exit_price, gross_pnl, friction, net_pnl, exit_reason, recorded_short_exit, trade_id),
            )
            conn.commit()

            return {
                "trade_id": trade_id,
                "symbol": row["symbol"],
                "short_contract": short_contract,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "short_exit_price": recorded_short_exit,
                "gross_pnl": gross_pnl,
                "friction": friction,
                "net_pnl": net_pnl,
                "exit_reason": exit_reason,
            }

    def get_open_trades(self, underlying: Optional[str] = None) -> List[Dict]:
        with self._get_conn() as conn:
            cursor = conn.cursor()
            if underlying:
                cursor.execute("SELECT * FROM ai_paper_trades WHERE status = 'OPEN' AND underlying = ?", (underlying,))
            else:
                cursor.execute("SELECT * FROM ai_paper_trades WHERE status = 'OPEN'")
            return [dict(r) for r in cursor.fetchall()]

    def get_all_trades(self) -> List[Dict]:
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM ai_paper_trades ORDER BY trade_id DESC")
            return [dict(r) for r in cursor.fetchall()]

    def get_summary(self) -> Dict:
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT 
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN net_pnl <= 0 AND status = 'CLOSED' THEN 1 ELSE 0 END) as losses,
                    ROUND(SUM(gross_pnl), 2) as total_gross_pnl,
                    ROUND(SUM(statutory_friction), 2) as total_friction,
                    ROUND(SUM(net_pnl), 2) as total_net_pnl
                FROM ai_paper_trades
                WHERE status = 'CLOSED'
                """
            )
            row = cursor.fetchone()
            if not row or row["total_trades"] == 0:
                return {
                    "total_trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "win_rate": 0.0,
                    "net_pnl": 0.0,
                }

            total = row["total_trades"]
            wins = row["wins"] or 0
            win_rate = round((wins / total) * 100, 1) if total > 0 else 0.0

            return {
                "total_trades": total,
                "wins": wins,
                "losses": row["losses"] or 0,
                "win_rate": win_rate,
                "gross_pnl": row["total_gross_pnl"] or 0.0,
                "total_friction": row["total_friction"] or 0.0,
                "net_pnl": row["total_net_pnl"] or 0.0,
            }

    def get_today_pnl(self, date_str: Optional[str] = None, source_tag: Optional[str] = None) -> float:
        """Calculates total net realized PnL for today's trading session in IST."""
        from zoneinfo import ZoneInfo
        ist = ZoneInfo("Asia/Kolkata")
        target_date = date_str or datetime.datetime.now(ist).strftime("%Y-%m-%d")

        with self._get_conn() as conn:
            cursor = conn.cursor()
            if source_tag:
                cursor.execute(
                    "SELECT entry_timestamp, net_pnl FROM ai_paper_trades WHERE status = 'CLOSED' AND source_tag = ?",
                    (source_tag,),
                )
            else:
                cursor.execute(
                    "SELECT entry_timestamp, net_pnl FROM ai_paper_trades WHERE status = 'CLOSED'"
                )
            total = 0.0
            for row in cursor.fetchall():
                try:
                    dt = datetime.datetime.fromisoformat(row["entry_timestamp"])
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=datetime.timezone.utc)
                    if dt.astimezone(ist).strftime("%Y-%m-%d") == target_date:
                        total += float(row["net_pnl"] or 0.0)
                except Exception:
                    continue
            return round(total, 2)

    def get_recent_consecutive_losses(self, limit: int = 5, source_tag: Optional[str] = None) -> int:
        """Returns the number of consecutive losses in the most recent closed trades."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            if source_tag:
                cursor.execute(
                    """
                    SELECT net_pnl FROM ai_paper_trades
                    WHERE status = 'CLOSED' AND source_tag = ?
                    ORDER BY trade_id DESC LIMIT ?
                    """,
                    (source_tag, limit),
                )
            else:
                cursor.execute(
                    """
                    SELECT net_pnl FROM ai_paper_trades
                    WHERE status = 'CLOSED'
                    ORDER BY trade_id DESC LIMIT ?
                    """,
                    (limit,),
                )
            rows = cursor.fetchall()
            consecutive = 0
            for r in rows:
                if r["net_pnl"] <= 0:
                    consecutive += 1
                else:
                    break
            return consecutive

    def void_synthetic_open_trades(self) -> int:
        """
        Marks any open non-live synthetic trades (e.g. legacy *_CURRENT_* dummy runs)
        as VOIDED so they do not block real LIVE_KITE position slots or contaminate PnL.
        """
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE ai_paper_trades
                SET status = 'VOIDED',
                    exit_timestamp = ?,
                    exit_price = entry_price,
                    gross_pnl = 0.0,
                    statutory_friction = 0.0,
                    net_pnl = 0.0,
                    exit_reason = 'VOIDED_SYNTHETIC_NO_LIVE_FEED'
                WHERE status = 'OPEN' AND (source_tag NOT LIKE 'LIVE_%' OR symbol LIKE '%_CURRENT_%')
                """,
                (now_iso,),
            )
            count = cursor.rowcount
            conn.commit()
            return count

    def partial_close_trade(
        self,
        trade_id: int,
        exit_lots: int,
        exit_price: float,
        exit_reason: str = "PARTIAL_PROFIT_BOOK_CIO",
        short_exit_price: Optional[float] = None,
    ) -> Dict:
        """
        Books profit/loss on `exit_lots` while leaving remaining runner lots OPEN.
        If `exit_lots >= open_lots`, performs full close.
        """
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._get_conn() as conn:
            cursor = conn.cursor()
            row = cursor.execute("SELECT * FROM ai_paper_trades WHERE trade_id = ?", (trade_id,)).fetchone()
            if not row or row["status"] != "OPEN":
                return {}

            open_lots = int(row["lots"])
            open_qty = int(row["quantity"])
            if exit_lots <= 0 or open_lots <= 0:
                return {}
            if exit_lots >= open_lots:
                return self.close_trade(trade_id, exit_price, exit_reason, short_exit_price=short_exit_price)

            lot_size = max(1, open_qty // open_lots)
            close_qty = exit_lots * lot_size
            rem_lots = open_lots - exit_lots
            rem_qty = open_qty - close_qty
            entry_price = float(row["entry_price"])
            direction = row["direction"]
            row_keys = set(row.keys())
            short_contract = row["short_contract"] if "short_contract" in row_keys else None
            short_entry = float(row["short_entry_price"] or 0.0) if "short_entry_price" in row_keys else 0.0
            spot_sl = row["spot_stop_loss"] if "spot_stop_loss" in row_keys else None

            gross_pnl = round((exit_price - entry_price) * close_qty, 2)
            recorded_short_exit = None
            if short_contract:
                short_ex = float(short_exit_price) if short_exit_price is not None else short_entry
                friction = self.calculate_vertical_spread_friction(
                    long_entry=entry_price + short_entry,
                    long_exit=exit_price + short_ex,
                    short_entry=short_entry,
                    short_exit=short_ex,
                    quantity=close_qty,
                )
                recorded_short_exit = round(short_ex, 2)
            else:
                friction = self.calculate_indian_friction(entry_price, exit_price, close_qty, direction)
            net_pnl = round(gross_pnl - friction, 2)

            # Reduce active open runner size in place
            cursor.execute(
                "UPDATE ai_paper_trades SET lots = ?, quantity = ? WHERE trade_id = ?",
                (rem_lots, rem_qty, trade_id),
            )
            # Record closed booked leg separately for exact accounting
            cursor.execute(
                """
                INSERT INTO ai_paper_trades (
                    entry_timestamp, exit_timestamp, underlying, symbol, direction,
                    entry_price, stop_loss, target, lots, quantity, status,
                    exit_price, gross_pnl, statutory_friction, net_pnl, exit_reason,
                    thesis, source_tag, spot_stop_loss, short_contract, short_entry_price, short_exit_price
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CLOSED', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["entry_timestamp"],
                    now_iso,
                    row["underlying"],
                    row["symbol"],
                    direction,
                    entry_price,
                    row["stop_loss"],
                    row["target"],
                    exit_lots,
                    close_qty,
                    exit_price,
                    gross_pnl,
                    friction,
                    net_pnl,
                    exit_reason,
                    row["thesis"],
                    row["source_tag"],
                    spot_sl,
                    short_contract,
                    short_entry if short_contract else None,
                    recorded_short_exit,
                ),
            )
            booked_id = cursor.lastrowid
            conn.commit()

            return {
                "trade_id": trade_id,
                "booked_trade_id": booked_id,
                "symbol": row["symbol"],
                "short_contract": short_contract,
                "closed_lots": exit_lots,
                "remaining_lots": rem_lots,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "short_exit_price": recorded_short_exit,
                "gross_pnl": gross_pnl,
                "friction": friction,
                "net_pnl": net_pnl,
                "exit_reason": exit_reason,
            }

    def log_cio_decision(
        self,
        action: str,
        reasoning: str,
        trade_id: Optional[int] = None,
        underlying: Optional[str] = None,
        contract: Optional[str] = None,
        regime: Optional[str] = None,
        context: Optional[Dict] = None,
    ) -> int:
        """Persists explicit AI CIO decisions (`ENTER`, `STAND_ASIDE`, `TRAIL_SL`, `PARTIAL_BOOK`, `BOOK_PROFIT`, `EXIT`)."""
        from zoneinfo import ZoneInfo
        import json
        ts_ist = datetime.datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO ai_cio_decisions (
                    timestamp_ist, action, trade_id, underlying, contract, regime, reasoning, context_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts_ist,
                    action.upper(),
                    trade_id,
                    underlying,
                    contract,
                    regime,
                    reasoning,
                    json.dumps(context or {}),
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def get_recent_cio_decisions(self, limit: int = 15) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM ai_cio_decisions ORDER BY decision_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
