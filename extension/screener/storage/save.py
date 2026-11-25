from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from loguru import logger

from config import DATA_DIR
from models.schema import PhaseOutput
from storage.db import insert_screen_candidate, insert_company_snapshot


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower())
    return re.sub(r"_+", "_", slug).strip("_")[:80] or "screener_output"


def save_phase_output(output: PhaseOutput, name: str) -> Path:
    day_dir = DATA_DIR / datetime.now().strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%H%M%S")
    path = day_dir / f"{output.phase}_{safe_slug(name)}_{timestamp}.json"
    path.write_text(output.model_dump_json(indent=2), encoding="utf-8")
    logger.success("[Storage] Saved JSON → {}", path)
    
    # Push into SQLite Warehouse
    try:
        # Save screen candidates if present
        if output.screen_candidates:
            for candidate in output.screen_candidates:
                insert_screen_candidate(
                    candidate,
                    output.timestamp,
                    capture_mode=output.raw_result.capture_mode,
                    captured_at=output.raw_result.captured_at,
                    source_url=output.raw_result.url,
                    adapter_version=output.raw_result.adapter_version,
                )
            logger.success(f"[Storage] Saved {len(output.screen_candidates)} candidates to SQLite.")
            
        # Save company snapshot if present
        if output.company_snapshot:
            insert_company_snapshot(
                output.company_snapshot,
                output.timestamp,
                capture_mode=output.raw_result.capture_mode,
                captured_at=output.raw_result.captured_at,
                source_url=output.raw_result.url,
                adapter_version=output.raw_result.adapter_version,
            )
            logger.success("[Storage] Saved company snapshot to SQLite.")
            
    except Exception as e:
        logger.error(f"[Storage] SQLite Insertion Failed: {e}")

    # Mirror latest to AI-Trading runtime for Cockpit & MCP server
    try:
        ai_root = Path(__file__).resolve().parent.parent.parent.parent
        ai_runtime = ai_root / "ai" / "runtime"
        ai_runtime.mkdir(parents=True, exist_ok=True)
        latest_path = ai_runtime / "screener_intelligence.json"
        latest_path.write_text(output.model_dump_json(indent=2), encoding="utf-8")
        logger.info(f"[Storage] Synced to AI-Trading runtime → {latest_path}")
    except Exception as exc:
        logger.debug(f"[Storage] AI runtime sync skipped: {exc}")

    # Mirror to SmartTrading data/by_date if present
    try:
        st_root = Path("/home/vvd/prakash/SmartTrading")
        today_str = datetime.now().strftime("%Y-%m-%d")
        st_today_dir = st_root / "data" / "by_date" / today_str / "screener"
        if st_root.exists():
            st_today_dir.mkdir(parents=True, exist_ok=True)
            st_path = st_today_dir / "screener_intelligence.json"
            st_path.write_text(output.model_dump_json(indent=2), encoding="utf-8")
            logger.info(f"[Storage] Synced to SmartTrading by_date → {st_path}")
    except Exception as exc:
        logger.debug(f"[Storage] SmartTrading sync skipped: {exc}")

    return path
