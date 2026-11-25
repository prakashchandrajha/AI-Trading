import sqlite3
import json
from pathlib import Path
from loguru import logger
from datetime import datetime
import pytz

from config import DATA_DIR
from models.schema import ScreenCandidate, CompanySnapshot

ROOT_DIR = Path(__file__).resolve().parent.parent.parent.parent
DB_PATH = ROOT_DIR / "database" / "sovereign_warehouse.db"

def _get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn

def init_db():
    """Initializes the SQLite database schema for the Screener bot."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = _get_conn()
    cursor = conn.cursor()

    # Table 1: screen_universe
    # Stores candidates extracted from pre-market and post-market screens.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS screen_universe (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            name TEXT NOT NULL,
            symbol TEXT,
            bot_score INTEGER,
            risk_bucket TEXT,
            intraday_use TEXT,
            position_size_multiplier REAL,
            allowed_bot_actions TEXT,  -- JSON list
            reasons TEXT,              -- JSON list
            warnings TEXT,             -- JSON list
            blocked_reasons TEXT,      -- JSON list
            watchlist_tags TEXT,       -- JSON list
            metrics TEXT               -- JSON dict
            ,capture_mode TEXT
            ,captured_at TEXT
            ,source_url TEXT
            ,adapter_version TEXT
        )
    """)

    # Table 2: company_snapshots
    # Stores deep-dive risk assessments from specific company pages.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS company_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT,
            bot_score INTEGER,
            risk_bucket TEXT,
            position_size_hint TEXT,
            position_size_multiplier REAL,
            risk_flags TEXT,           -- JSON list
            allowed_bot_actions TEXT,  -- JSON list
            blocked_reasons TEXT,      -- JSON list
            ratios TEXT,               -- JSON dict
            financial_tables TEXT      -- JSON dict
            ,capture_mode TEXT
            ,captured_at TEXT
            ,source_url TEXT
            ,adapter_version TEXT
        )
    """)

    # Indexes for fast querying by the trading bot
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_universe_symbol ON screen_universe (symbol)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_universe_timestamp ON screen_universe (timestamp DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_snapshot_symbol ON company_snapshots (symbol)")
    for table in ("screen_universe", "company_snapshots"):
        for column, definition in {
            "capture_mode": "TEXT",
            "captured_at": "TEXT",
            "source_url": "TEXT",
            "adapter_version": "TEXT",
        }.items():
            try:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError:
                pass

    conn.commit()
    conn.close()

def insert_screen_candidate(
    candidate: ScreenCandidate,
    timestamp: str = None,
    *,
    capture_mode: str = "",
    captured_at: str = "",
    source_url: str = "",
    adapter_version: str = "",
):
    """Inserts a ScreenCandidate into the screen_universe table."""
    if not timestamp:
        timestamp = datetime.now(pytz.timezone('Asia/Kolkata')).isoformat()

    conn = _get_conn()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO screen_universe (
            timestamp, name, symbol, bot_score, risk_bucket, intraday_use,
            position_size_multiplier, allowed_bot_actions, reasons, warnings,
            blocked_reasons, watchlist_tags, metrics,
            capture_mode, captured_at, source_url, adapter_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        timestamp,
        candidate.name,
        candidate.symbol,
        candidate.bot_score,
        candidate.risk_bucket,
        candidate.intraday_use,
        candidate.position_size_multiplier,
        json.dumps(candidate.allowed_bot_actions),
        json.dumps(candidate.reasons),
        json.dumps(candidate.warnings),
        json.dumps(candidate.blocked_reasons),
        json.dumps(candidate.watchlist_tags),
        json.dumps(candidate.metrics),
        capture_mode,
        captured_at,
        source_url,
        adapter_version,
    ))

    conn.commit()
    conn.close()

def insert_company_snapshot(
    snapshot: CompanySnapshot,
    timestamp: str = None,
    *,
    capture_mode: str = "",
    captured_at: str = "",
    source_url: str = "",
    adapter_version: str = "",
):
    """Inserts a CompanySnapshot into the company_snapshots table."""
    if not timestamp:
        timestamp = datetime.now(pytz.timezone('Asia/Kolkata')).isoformat()

    conn = _get_conn()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO company_snapshots (
            timestamp, symbol, name, bot_score, risk_bucket, position_size_hint,
            position_size_multiplier, risk_flags, allowed_bot_actions,
            blocked_reasons, ratios, financial_tables,
            capture_mode, captured_at, source_url, adapter_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        timestamp,
        snapshot.symbol,
        snapshot.name,
        snapshot.bot_score,
        snapshot.risk_bucket,
        snapshot.position_size_hint,
        snapshot.position_size_multiplier,
        json.dumps(snapshot.risk_flags),
        json.dumps(snapshot.allowed_bot_actions),
        json.dumps(snapshot.blocked_reasons),
        json.dumps(snapshot.ratios),
        json.dumps(snapshot.financial_tables) if hasattr(snapshot, 'financial_tables') else "{}",
        capture_mode,
        captured_at,
        source_url,
        adapter_version,
    ))

    conn.commit()
    conn.close()

# Initialize DB on module import
init_db()
