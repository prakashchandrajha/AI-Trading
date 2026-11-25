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
            conn.commit()

    @staticmethod
    def calculate_indian_friction(
        entry_price: float,
        exit_price: float,
        quantity: int,
        direction: str = "BUY",
    ) -> float:
        """
        Calculates exact Indian F&O Option transaction charges:
        1. STT: 0.15% on SELL turnover (premium * qty)
        2. Exchange Turnover: ~0.05% of turnover (NSE)
        3. Stamp Duty: 0.003% on BUY turnover
        4. SEBI Fees: ₹10 per crore (0.0001%)
        5. Brokerage: ₹20 per executed order (₹40 round-trip)
        6. GST: 18% on (Brokerage + Exchange turnover fees)
        """
        if quantity <= 0:
            return 0.0

        buy_price = max(0.0, entry_price if direction.startswith("BUY") else exit_price)
        sell_price = max(0.0, exit_price if direction.startswith("BUY") else entry_price)

        buy_turnover = buy_price * quantity
        sell_turnover = sell_price * quantity
        total_turnover = buy_turnover + sell_turnover

        # 1. STT: 0.15% on sell turnover (rounded to nearest integer rupee per Indian tax law)
        stt = float(round(0.0015 * sell_turnover))

        # 2. Exchange turnover charge: 0.03553% (NSE options rate inclusive of IPFT, post-Oct 2024 / 2026 schedule)
        exch_turnover_fee = 0.0003553 * total_turnover

        # 3. Stamp duty: 0.003% on buy
        stamp_duty = 0.00003 * buy_turnover

        # 4. SEBI charge: ₹10 / crore
        sebi_fee = 0.000001 * total_turnover

        # 5. Brokerage: ₹20 per executed order (₹40 round-trip)
        brokerage = 40.0

        # 6. GST: 18% on brokerage and exchange fee
        gst = 0.18 * (brokerage + exch_turnover_fee)

        total_friction = round(stt + exch_turnover_fee + stamp_duty + sebi_fee + brokerage + gst, 2)
        return total_friction

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
    ) -> int:
        quantity = lots * lot_size
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO ai_paper_trades (
                    entry_timestamp, underlying, symbol, direction, entry_price,
                    stop_loss, target, lots, quantity, status, thesis, source_tag
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
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

    def close_trade(
        self,
        trade_id: int,
        exit_price: float,
        exit_reason: str,
    ) -> Dict:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM ai_paper_trades WHERE trade_id = ?", (trade_id,))
            row = cursor.fetchone()
            if not row or row["status"] != "OPEN":
                return {}

            entry_price = row["entry_price"]
            quantity = row["quantity"]
            direction = row["direction"]

            # Gross PnL (for option buying)
            gross_pnl = round((exit_price - entry_price) * quantity, 2)
            friction = self.calculate_indian_friction(entry_price, exit_price, quantity, direction)
            net_pnl = round(gross_pnl - friction, 2)

            cursor.execute(
                """
                UPDATE ai_paper_trades
                SET exit_timestamp = ?, exit_price = ?, gross_pnl = ?,
                    statutory_friction = ?, net_pnl = ?, exit_reason = ?, status = 'CLOSED'
                WHERE trade_id = ?
                """,
                (now_iso, exit_price, gross_pnl, friction, net_pnl, exit_reason, trade_id),
            )
            conn.commit()

            return {
                "trade_id": trade_id,
                "symbol": row["symbol"],
                "entry_price": entry_price,
                "exit_price": exit_price,
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
                WHERE status = 'OPEN' AND (source_tag != 'LIVE_KITE' OR symbol LIKE '%_CURRENT_%')
                """,
                (now_iso,),
            )
            count = cursor.rowcount
            conn.commit()
            return count


