"""
Strictly Read-Only Connector for SmartTrading (`desk/` & `data/`).

Rules enforced by design:
1. ZERO imports from SmartTrading Python modules (no cross-repo side effects).
2. ZERO writes, ZERO POST endpoints, ZERO lock contention (SQLite `?mode=ro`, short timeouts).
3. ZERO competing Kite WebSocket connections (passively reads `/dev/shm`, HTTP GET, ZMQ SUB).
4. Graceful offline degradation whenever `desk` daemons are stopped.
"""
from __future__ import annotations

import csv
import datetime
import json
import math
import os
import socket
import sqlite3
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def resolve_smart_trading_root() -> Optional[Path]:
    """Locates the sibling SmartTrading workspace cleanly without hardcoding assumptions."""
    candidates: List[Path] = []
    env_root = os.environ.get("SMARTTRADING_ROOT", "").strip()
    if env_root:
        candidates.append(Path(env_root))
    here = Path(__file__).resolve()
    candidates.append(here.parent.parent.parent / "SmartTrading")
    candidates.append(Path("/home/vvd/prakash/SmartTrading"))

    for cand in candidates:
        if cand.exists() and (cand / "data").exists():
            return cand
    return None


def _is_tcp_listening(host: str, port: int, timeout: float = 0.03) -> bool:
    """Fast loopback probe so offline services add <30ms overhead."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_get_json(url: str, timeout: float = 0.4) -> Optional[Dict[str, Any]]:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def compute_book_telemetry(
    spot_or_mid: float,
    bid_depth: List[Tuple[float, int]],
    ask_depth: List[Tuple[float, int]],
    prev_bid_depth: Optional[List[Tuple[float, int]]] = None,
    prev_ask_depth: Optional[List[Tuple[float, int]]] = None,
    lot_size: int = 1,
) -> Dict[str, Any]:
    """
    Computes Desk-grade order-book microstructure telemetry directly from 5-level L2 depth:
    - Micro-Price & predictive lead over midpoint
    - Spatial Kinetic Order Book Pressure (KOBP with 1/d^2 weighting)
    - Lot-normalized 5-level Z_OFI estimate
    - Ask/Bid Iceberg Reload Warning (passive seller/buyer absorption check)
    """
    if not bid_depth or not ask_depth:
        return {
            "best_bid": 0.0,
            "best_ask": 0.0,
            "spread_pct": 0.0,
            "micro_price": spot_or_mid,
            "micro_lead": 0.0,
            "kobp": 0.0,
            "z_ofi": 0.0,
            "bid_vol_5l": 0,
            "ask_vol_5l": 0,
            "ask_replenish_warning": False,
            "bid_replenish_support": False,
        }

    best_bid, best_bid_q = float(bid_depth[0][0]), int(bid_depth[0][1])
    best_ask, best_ask_q = float(ask_depth[0][0]), int(ask_depth[0][1])
    mid = (best_bid + best_ask) / 2.0 if (best_bid > 0 and best_ask > 0) else spot_or_mid
    spread_pct = ((best_ask - best_bid) / mid * 100.0) if (mid > 0 and best_ask >= best_bid > 0) else 0.0

    tot_l1 = best_bid_q + best_ask_q
    micro_px = ((best_bid_q * best_ask + best_ask_q * best_bid) / tot_l1) if tot_l1 > 0 else mid

    # Spatial 1/d^2 KOBP pressure across all depth levels
    tick_ref = max(0.05, mid * 0.0005)
    bid_pressure = 0.0
    ask_pressure = 0.0
    bid_vol_sum = 0
    ask_vol_sum = 0
    for px, qty in bid_depth[:5]:
        if px <= 0 or qty <= 0:
            continue
        bid_vol_sum += int(qty)
        dist = max(tick_ref, abs(mid - float(px)))
        bid_pressure += float(qty) / ((dist / tick_ref) ** 2)

    for px, qty in ask_depth[:5]:
        if px <= 0 or qty <= 0:
            continue
        ask_vol_sum += int(qty)
        dist = max(tick_ref, abs(float(px) - mid))
        ask_pressure += float(qty) / ((dist / tick_ref) ** 2)

    tot_pressure = bid_pressure + ask_pressure
    kobp = ((bid_pressure - ask_pressure) / tot_pressure) if tot_pressure > 0 else 0.0

    # 5-level decayed OFI (lot normalized)
    weights = (0.40, 0.25, 0.15, 0.12, 0.08)
    n_levels = min(len(bid_depth), len(ask_depth), len(weights))
    delta_ofi = 0.0
    ask_refills = 0
    bid_refills = 0
    for i in range(n_levels):
        bp, bq = float(bid_depth[i][0]), int(bid_depth[i][1])
        ap, aq = float(ask_depth[i][0]), int(ask_depth[i][1])
        if prev_bid_depth and prev_ask_depth and i < len(prev_bid_depth) and i < len(prev_ask_depth):
            pbp, pbq = float(prev_bid_depth[i][0]), int(prev_bid_depth[i][1])
            pap, paq = float(prev_ask_depth[i][0]), int(prev_ask_depth[i][1])
            db = bq if bp > pbp else (bq - pbq if bp == pbp else -pbq)
            da = aq if ap < pap else (aq - paq if ap == pap else -paq)
            level_ofi = db - da
            if ap == pap and aq > paq * 1.35 and aq > bq:
                ask_refills += 1
            if bp == pbp and bq > pbq * 1.35 and bq > aq:
                bid_refills += 1
        else:
            level_ofi = float(bq - aq)
        delta_ofi += weights[i] * level_ofi

    # Scale relative to active queue size or lot size so both stocks & options stay comparable
    norm_scale = max(float(lot_size), (bid_vol_sum + ask_vol_sum) * 0.12, 1.0)
    z_ofi = max(-6.0, min(6.0, (delta_ofi / norm_scale) * 2.5))

    return {
        "best_bid": round(best_bid, 2),
        "best_ask": round(best_ask, 2),
        "spread_pct": round(spread_pct, 2),
        "micro_price": round(micro_px, 2),
        "micro_lead": round(micro_px - mid, 2),
        "kobp": round(kobp, 3),
        "z_ofi": round(z_ofi, 2),
        "bid_vol_5l": bid_vol_sum,
        "ask_vol_5l": ask_vol_sum,
        "ask_replenish_warning": ask_refills > 0 or (ask_vol_sum > bid_vol_sum * 2.2 and kobp < -0.35),
        "bid_replenish_support": bid_refills > 0 or (bid_vol_sum > ask_vol_sum * 1.8 and kobp > 0.30),
    }


class SmartTradingBridge:
    """Read-only connector bridging SmartTrading's desk runtime & data into AI-Trading."""

    def __init__(
        self,
        smart_trading_root: Optional[Path] = None,
        desk_http_url: str = "http://127.0.0.1:9192",
    ) -> None:
        self.st_root = smart_trading_root or resolve_smart_trading_root()
        self.desk_http_url = desk_http_url.rstrip("/")
        self.ai_root = Path(__file__).resolve().parent.parent
        shm_dir = os.environ.get("SMART_TRADING_LTP_CACHE_DIR", "/dev/shm/smart_trading")
        self.shm_ltp_db = Path(shm_dir) / "ltp_cache.db"

    def get_session_credentials(self) -> Tuple[str, str, str]:
        """Finds freshest Zerodha Kite credentials from env or read-only session files."""
        api_key = os.environ.get("KITE_API_KEY", "").strip()
        access_token = os.environ.get("KITE_ACCESS_TOKEN", "").strip()
        if api_key and access_token:
            return api_key, access_token, "ENV"

        today_str = datetime.datetime.now(IST).strftime("%Y-%m-%d")
        search_roots: List[Path] = [self.ai_root]
        if self.st_root:
            search_roots.append(self.st_root)

        candidates: List[Path] = []
        for root in search_roots:
            candidates.extend([
                root / "desk" / ".kite_session",
                root / "data" / ".kite_session",
                root / ".kite_session",
            ])

        best: Optional[Tuple[int, float, str, str, str]] = None
        for cand in candidates:
            if not cand.exists():
                continue
            try:
                data = json.loads(cand.read_text(encoding="utf-8"))
                k = str(data.get("api_key", "")).strip()
                t = str(data.get("access_token", "")).strip()
                if not k or not t:
                    continue
                is_today = 1 if str(data.get("date", "")) == today_str else 0
                mtime = cand.stat().st_mtime
                if best is None or (is_today, mtime) > (best[0], best[1]):
                    best = (is_today, mtime, k, t, str(cand))
            except Exception:
                continue

        if best is not None:
            return best[2], best[3], best[4]
        return "", "", "NONE"

    def get_instruments_path(self, date_str: Optional[str] = None) -> Optional[Path]:
        """Returns path to Zerodha instruments.json without copying 30MB files."""
        today_str = date_str or datetime.datetime.now(IST).strftime("%Y-%m-%d")
        roots: List[Path] = [self.ai_root]
        if self.st_root:
            roots.append(self.st_root)

        for root in roots:
            for rel in (
                Path("data") / "instruments.json",
                Path("data") / f"instruments_{today_str}.json",
                Path("data") / "by_date" / today_str / "zerodha" / "instruments.json",
            ):
                cand = root / rel
                if cand.exists():
                    return cand
        return None

    def get_http_telemetry(self) -> Dict[str, Any]:
        """Queries desk_server HTTP GET endpoints safely. Never invokes POST mutations."""
        host_port = self.desk_http_url.replace("http://", "").replace("https://", "")
        host, _, port_s = host_port.partition(":")
        port = int(port_s) if port_s.isdigit() else 9192
        if not _is_tcp_listening(host or "127.0.0.1", port):
            return {"desk_status": "OFFLINE", "health": None, "macro": None, "positions": None, "signals": []}

        health = _http_get_json(f"{self.desk_http_url}/health")
        macro = _http_get_json(f"{self.desk_http_url}/macro_state")
        positions = _http_get_json(f"{self.desk_http_url}/positions")
        sig_payload = _http_get_json(f"{self.desk_http_url}/signals") or {}
        signals = sig_payload.get("signals", []) if isinstance(sig_payload, dict) else []

        return {
            "desk_status": "ONLINE" if (health or macro) else "DEGRADED",
            "health": health,
            "macro": macro,
            "positions": positions,
            "signals": signals[-15:],
        }

    def read_shm_ltp_cache(
        self, symbols: Optional[List[str]] = None, max_age_sec: float = 15.0
    ) -> Dict[str, Dict[str, Any]]:
        """Reads RAM SQLite cache `/dev/shm/smart_trading/ltp_cache.db` via read-only URI."""
        if not self.shm_ltp_db.exists():
            return {}
        try:
            conn = sqlite3.connect(f"file:{self.shm_ltp_db}?mode=ro", uri=True, timeout=0.2)
            conn.row_factory = sqlite3.Row
            ohlc_rows = conn.execute("SELECT symbol, previous_close FROM ltp_cache_ohlc").fetchall()
            prev_closes = {r["symbol"]: float(r["previous_close"]) for r in ohlc_rows if r["previous_close"] is not None}

            if symbols:
                placeholders = ",".join("?" for _ in symbols)
                rows = conn.execute(
                    f"SELECT symbol, last_price, depth_buy, depth_sell, last_update FROM ltp_cache WHERE symbol IN ({placeholders})",
                    symbols,
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT symbol, last_price, depth_buy, depth_sell, last_update FROM ltp_cache"
                ).fetchall()
            conn.close()
        except Exception:
            return {}

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            sym = r["symbol"]
            age_sec = 9999.0
            try:
                upd = str(r["last_update"] or "")
                dt = datetime.datetime.fromisoformat(upd)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.timezone.utc)
                age_sec = max(0.0, (now_utc - dt).total_seconds())
            except Exception:
                pass

            bids: List[Tuple[float, int]] = []
            asks: List[Tuple[float, int]] = []
            try:
                raw_b = json.loads(r["depth_buy"]) if r["depth_buy"] else []
                raw_a = json.loads(r["depth_sell"]) if r["depth_sell"] else []
                bids = [(float(x.get("price", 0.0)), int(x.get("quantity", 0))) for x in raw_b if float(x.get("price", 0.0)) > 0]
                asks = [(float(x.get("price", 0.0)), int(x.get("quantity", 0))) for x in raw_a if float(x.get("price", 0.0)) > 0]
            except Exception:
                pass

            ltp = float(r["last_price"] or 0.0)
            book = compute_book_telemetry(ltp, bids, asks)
            out[sym] = {
                "last_price": ltp,
                "prev_close": prev_closes.get(sym, ltp),
                "age_sec": round(age_sec, 1),
                "stale": age_sec > max_age_sec,
                "bid_depth": bids,
                "ask_depth": asks,
                **book,
            }
        return out

    def sample_zmq_alerts(self, port: int = 9899, timeout_ms: int = 120, max_events: int = 20) -> List[Dict[str, Any]]:
        """Passively samples pending `SovereignScanner` alerts without blocking Desk."""
        if not _is_tcp_listening("127.0.0.1", port):
            return []
        try:
            import zmq
        except ImportError:
            return []

        alerts: List[Dict[str, Any]] = []
        ctx = zmq.Context()
        sock = ctx.socket(zmq.SUB)
        sock.setsockopt(zmq.RCVHWM, 200)
        sock.setsockopt_string(zmq.SUBSCRIBE, "")
        try:
            sock.connect(f"tcp://127.0.0.1:{port}")
            deadline = time.monotonic() + (timeout_ms / 1000.0)
            while len(alerts) < max_events and time.monotonic() < deadline:
                try:
                    raw = sock.recv_string(flags=zmq.NOBLOCK)
                    alerts.append(json.loads(raw))
                except zmq.Again:
                    time.sleep(0.015)
                except Exception:
                    break
        except Exception:
            pass
        finally:
            sock.close(linger=0)
            ctx.term()
        return alerts

    def _latest_by_date_dir(self, target_date: Optional[str] = None) -> Optional[Path]:
        if not self.st_root:
            return None
        by_date = self.st_root / "data" / "by_date"
        if not by_date.exists():
            return None
        if target_date and (by_date / target_date).exists():
            return by_date / target_date
        dirs = sorted([d for d in by_date.iterdir() if d.is_dir()], key=lambda d: d.name, reverse=True)
        return dirs[0] if dirs else None

    def read_nse_premarket_and_oi(self, target_date: Optional[str] = None) -> Dict[str, Any]:
        """Reads NSE pre-open auction breadth, participant FII/DII/Client OI, and Desk regime files."""
        today_str = target_date or datetime.datetime.now(IST).strftime("%Y-%m-%d")
        result: Dict[str, Any] = {
            "trading_date": today_str,
            "is_today_fresh": False,
            "premarket_source": "UNAVAILABLE",
            "auction_breadth": {},
            "participant_positioning": {},
            "premarket_bias": {},
            "daily_regime": {},
            "frontier_policy": {},
            "fno_ban_list": [],
        }
        if not self.st_root:
            return result

        data_dir = self.st_root / "data"
        day_dir = self._latest_by_date_dir(today_str)

        # Read synthesized NSE premarket_analysis.json
        pm_candidates = []
        if day_dir:
            pm_candidates.append(day_dir / "nse" / "premarket_analysis.json")
        pm_candidates.append(data_dir / "by_date" / today_str / "nse" / "premarket_analysis.json")

        for pm_path in pm_candidates:
            if pm_path.exists():
                try:
                    pm = json.loads(pm_path.read_text(encoding="utf-8"))
                    pm_date = str(pm.get("trading_date", ""))
                    result["is_today_fresh"] = (pm_date == today_str)
                    result["premarket_source"] = f"NSE_PREMARKET:{pm_date or pm_path.parent.parent.name}"
                    result["auction_breadth"] = pm.get("auction_breadth", {})
                    result["participant_positioning"] = pm.get("participant_positioning", {})
                    result["premarket_bias"] = pm.get("bias", {})
                    break
                except Exception:
                    pass

        # Read optional raw participant_oi.csv if positioning wasn't populated
        if not result["participant_positioning"] and day_dir:
            csv_path = day_dir / "nse" / "participant_oi.csv"
            if csv_path.exists():
                try:
                    rows = list(csv.DictReader(csv_path.read_text(encoding="utf-8").splitlines()))
                    result["participant_positioning"] = {"raw_rows": rows[:6]}
                except Exception:
                    pass

        # Read Desk regime, frontier policy, trader health, and morning intel (F&O ban list)
        for key, filename in (
            ("daily_regime", "daily_regime.json"),
            ("frontier_policy", "frontier_policy.json"),
            ("trader_health", "trader_health.json"),
        ):
            p = data_dir / filename
            if p.exists():
                try:
                    result[key] = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    pass

        ban_set: Set[str] = set()
        mi = data_dir / "morning_intel.json"
        if mi.exists():
            try:
                mi_json = json.loads(mi.read_text(encoding="utf-8"))
                mi_data = mi_json.get("data") if isinstance(mi_json.get("data"), dict) else mi_json
                for x in (mi_data.get("fno_ban_list") or []):
                    ban_set.add(str(x).upper())
                result["gift_nifty"] = mi_data.get("gift_nifty") or {}
                result["global_macro"] = mi_data.get("global_macro") or {}
            except Exception:
                pass

        mc = data_dir / "market_controls.json"
        if mc.exists():
            try:
                mc_json = json.loads(mc.read_text(encoding="utf-8"))
                secs = mc_json.get("sections") or {}
                for x in ((secs.get("fno_ban") or {}).get("symbols") or []):
                    ban_set.add(str(x).upper())
                result["mwpl_utilization"] = (secs.get("mwpl") or {}).get("utilization_pct") or {}
            except Exception:
                pass

        result["fno_ban_list"] = sorted(ban_set)
        return result

    def get_active_external_positions(self, http_telemetry: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Reads currently OPEN Desk positions cleanly so AI enforces 0% symbol collision."""
        positions: List[Dict[str, Any]] = []
        if http_telemetry and isinstance(http_telemetry.get("positions"), dict):
            for p in http_telemetry["positions"].get("open_positions", []):
                positions.append({
                    "source": "DESK_HTTP",
                    "symbol": str(p.get("symbol", "")),
                    "direction": str(p.get("direction", "")),
                    "unrealized_pnl": p.get("unrealized_pnl"),
                })

        if not positions and self.st_root:
            db_path = self.st_root / "data" / "database" / "smart_trading.db"
            if db_path.exists():
                try:
                    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.2)
                    conn.row_factory = sqlite3.Row
                    for tbl in ("live_trades", "paper_trades"):
                        try:
                            rows = conn.execute(
                                f"SELECT ticker, direction, entry_price, quantity FROM {tbl} WHERE status = 'OPEN'"
                            ).fetchall()
                            for r in rows:
                                positions.append({
                                    "source": f"DESK_DB:{tbl}",
                                    "symbol": str(r["ticker"]),
                                    "direction": str(r["direction"]),
                                    "entry_price": r["entry_price"],
                                })
                        except Exception:
                            continue
                    conn.close()
                except Exception:
                    pass
        return positions

    @staticmethod
    def extract_underlying_from_ticker(ticker: str) -> str:
        """Extracts root underlying (e.g., NIFTY26DEC25000CE -> NIFTY, BANKNIFTY... -> BANKNIFTY)."""
        up = ticker.upper().strip()
        if "NIFTY" in up:
            if up.startswith("BANKNIFTY"):
                return "BANKNIFTY"
            if up.startswith("MIDCPNIFTY"):
                return "MIDCPNIFTY"
            if up.startswith("FINNIFTY"):
                return "FINNIFTY"
            return "NIFTY"
        letters = []
        for ch in up:
            if ch.isalpha() or ch == "&":
                letters.append(ch)
            else:
                break
        return "".join(letters) or up

    def get_archived_tick_dbs(self, limit: int = 12) -> List[Tuple[str, Path]]:
        """Lists archived Zerodha tick_history.db files across days for non-curve-fit logic tests."""
        if not self.st_root:
            return []
        by_date = self.st_root / "data" / "by_date"
        if not by_date.exists():
            return []

        found: List[Tuple[str, Path]] = []
        for day_dir in sorted([d for d in by_date.iterdir() if d.is_dir()], key=lambda d: d.name, reverse=True):
            db = day_dir / "zerodha" / "tick_history.db"
            if db.exists() and db.stat().st_size > 4096:
                found.append((day_dir.name, db))
                if len(found) >= limit:
                    break
        return found

    def read_latest_decision_journal(self, max_lines: int = 12) -> Dict[str, Any]:
        """Tails latest JSONL decisions written by Desk (fut_ofi_z, aggressor flow, climax exhaustion vetoes)."""
        if not self.st_root:
            return {}
        today_str = datetime.datetime.now(IST).strftime("%Y-%m-%d")
        jdir = self.st_root / "data" / "decision_journal"
        cand = jdir / f"{today_str}.jsonl"
        if not cand.exists() and jdir.exists():
            files = sorted(jdir.glob("*.jsonl"), reverse=True)
            cand = files[0] if files else cand
        if not cand.exists():
            return {}

        try:
            with cand.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 16384))
                tail_lines = f.read().decode("utf-8", errors="ignore").splitlines()[-max_lines:]
            parsed = [json.loads(ln) for ln in tail_lines if ln.strip().startswith("{")]
            if not parsed:
                return {}
            last = parsed[-1]
            tf = last.get("trade_flow") or {}
            oi_ctx = last.get("oi_ctx") or {}
            return {
                "file": cand.name,
                "underlying": last.get("underlying"),
                "fut_ofi_z": round(float(last.get("fut_ofi_z", 0.0) or 0.0), 2),
                "aggressor_imb_z": round(float(tf.get("aggressor_imb_z", 0.0) or 0.0), 2),
                "oi_delta_z": round(float(oi_ctx.get("oi_delta_z", 0.0) or 0.0), 2),
                "reason": str(last.get("reason", ""))[:90],
            }
        except Exception:
            return {}

    def get_unified_desk_context(self) -> Dict[str, Any]:
        """Single read-only snapshot combining Desk HTTP, RAM cache, ZMQ signals, Macro Regime, and Collision Mutex."""
        http_info = self.get_http_telemetry()
        ext_positions = self.get_active_external_positions(http_info)
        collision_underlyings = sorted({
            self.extract_underlying_from_ticker(p["symbol"])
            for p in ext_positions
            if p.get("symbol")
        })
        shm_macro = self.read_shm_ltp_cache(["NIFTY 50", "NIFTY BANK", "INDIA VIX", "CRUDEOIL", "USDINR"])
        zmq_alerts = self.sample_zmq_alerts() if http_info["desk_status"] == "ONLINE" else []
        nse_pm = self.read_nse_premarket_and_oi()
        journal = self.read_latest_decision_journal()

        # Synthesize Desk Macro Regime & Futures Index Tide (NIFTY / BANKNIFTY / VIX / CRUDE / USDINR)
        http_macro = http_info.get("macro") or {}
        fp = nse_pm.get("frontier_policy") or {}
        th = nse_pm.get("trader_health") or {}
        upols = fp.get("underlying_policies") or {}
        nifty_pol = upols.get("NIFTY") or {}
        bnf_pol = upols.get("BANKNIFTY") or {}

        fp_age_min = None
        if fp.get("iso_time"):
            try:
                fdt = datetime.datetime.fromisoformat(fp["iso_time"])
                if fdt.tzinfo is None:
                    fdt = fdt.replace(tzinfo=IST)
                fp_age_min = round(max(0.0, (datetime.datetime.now(IST) - fdt).total_seconds() / 60.0), 1)
            except Exception:
                pass

        http_inds = http_macro.get("indicators") or {}
        desk_macro_regime = {
            "http_regime": http_macro.get("regime"),
            "http_bias": http_macro.get("market_bias"),
            "is_panic": bool(http_macro.get("is_panic", False)),
            "crude_status": (http_inds.get("CRUDEOIL") or {}).get("status"),
            "crude_chg_pct": (http_inds.get("CRUDEOIL") or {}).get("change_pct"),
            "usdinr_status": (http_inds.get("USDINR") or {}).get("status"),
            "vix_status": (http_inds.get("INDIA VIX") or {}).get("status"),
            "frontier_regime": fp.get("regime") or th.get("macro_regime"),
            "frontier_age_min": fp_age_min,
            "global_risk_scalar": fp.get("global_risk_scalar"),
            "nifty_fut_ofi_z": (nifty_pol.get("macro") or {}).get("ofi_z"),
            "nifty_vwap_bps": (nifty_pol.get("macro") or {}).get("dist_vwap_bps"),
            "nifty_permitted_side": nifty_pol.get("permitted_side"),
            "bnf_permitted_side": bnf_pol.get("permitted_side"),
            "ambient_ofi_scale": th.get("ambient_ofi_scale"),
            "gift_nifty": nse_pm.get("gift_nifty") or {},
            "latest_journal": journal,
        }

        recent_signals = list(http_info.get("signals") or [])
        for al in zmq_alerts[-5:]:
            recent_signals.append(
                f"[ZMQ] {al.get('symbol')} {al.get('direction')} {al.get('reason')} "
                f"(ofi_z={al.get('ofi_z')}, ltp={al.get('ltp')})"
            )

        return {
            "desk_root": str(self.st_root) if self.st_root else "NOT_FOUND",
            "desk_status": http_info["desk_status"],
            "desk_open_positions": ext_positions,
            "collision_blocked_underlyings": collision_underlyings,
            "shm_macro_quotes": shm_macro,
            "desk_macro_regime": desk_macro_regime,
            "recent_desk_signals": recent_signals[-10:],
            "nse_premarket": nse_pm,
            "archived_tick_days": len(self.get_archived_tick_dbs(limit=30)),
        }
