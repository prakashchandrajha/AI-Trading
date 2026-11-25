"""
Read-Only Live Zerodha Kite Market Feed & Instrument Master Resolver for AI Trading CIO.

Strictly self-contained wrt cross-repo imports (connects read-only via `connector`).
Uses `SmartTradingBridge` to resolve shared Zerodha session credentials & `instruments.json`,
reads RAM tick cache if Desk is active, and batches live Kite REST `quote()` + `historical_data()`
for stocks, options, 5-level depth, and true exchange Advances/Declines breadth.
Fails closed (returns None / {}) whenever live market data is unavailable.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

AI_DIR = Path(__file__).resolve().parent
ROOT_DIR = AI_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))

from connector.smart_trading_bridge import SmartTradingBridge
from options_analytics import OptionStrikeData

IST = ZoneInfo("Asia/Kolkata")

INDEX_SPOT_MAP: Dict[str, Tuple[str, int]] = {
    "NIFTY": ("NSE:NIFTY 50", 256265),
    "BANKNIFTY": ("NSE:NIFTY BANK", 260105),
    "FINNIFTY": ("NSE:NIFTY FIN SERVICE", 257801),
    "MIDCPNIFTY": ("NSE:NIFTY MID SELECT", 288009),
}


@dataclass
class LiveMarketSnapshot:
    symbol: str
    spot_symbol: str
    spot_price: float
    vwap_live: float
    prev_close: float
    lot_size: int
    strike_step: float
    expiry: str
    chain: List[OptionStrikeData]
    contract_map: Dict[Tuple[float, str], str]
    price_history: List[float]
    volume_history: List[int]
    fetched_at: float
    high_history: List[float] = field(default_factory=list)
    low_history: List[float] = field(default_factory=list)
    spot_bid_depth: List[Tuple[float, int]] = field(default_factory=list)
    spot_ask_depth: List[Tuple[float, int]] = field(default_factory=list)
    prev_spot_bid_depth: List[Tuple[float, int]] = field(default_factory=list)
    prev_spot_ask_depth: List[Tuple[float, int]] = field(default_factory=list)
    spot_volume_today: int = 0
    spot_buy_qty: int = 0
    spot_sell_qty: int = 0


def _extract_5level_depth(depth_dict: Optional[Dict[str, Any]]) -> Tuple[List[Tuple[float, int]], List[Tuple[float, int]]]:
    if not depth_dict:
        return [], []
    buys = [
        (float(x.get("price", 0.0) or 0.0), int(x.get("quantity", 0) or 0))
        for x in (depth_dict.get("buy") or [])[:5]
        if float(x.get("price", 0.0) or 0.0) > 0
    ]
    sells = [
        (float(x.get("price", 0.0) or 0.0), int(x.get("quantity", 0) or 0))
        for x in (depth_dict.get("sell") or [])[:5]
        if float(x.get("price", 0.0) or 0.0) > 0
    ]
    return buys, sells


class LiveKiteFeed:
    """
    Batched, fail-closed Zerodha Kite live option chain, 5-level depth, 5m bars, and market breadth feed.
    Persists intraday baseline OI cleanly to `ai/runtime/oi_baseline_<date>.json` across restarts.
    """

    def __init__(self, bridge: Optional[SmartTradingBridge] = None) -> None:
        self.bridge = bridge or SmartTradingBridge()
        self._kite = None
        self._kite_init_error: Optional[str] = None
        self._instruments_loaded = False
        self._spot_tokens: Dict[str, int] = {}
        self._nfo_opts: Dict[str, Dict[str, List[dict]]] = {}
        self._lot_sizes: Dict[str, int] = {}
        self._strike_steps: Dict[str, float] = {}
        self._nearest_expiry: Dict[str, str] = {}
        self._baseline_oi: Dict[str, int] = {}
        self._baseline_oi_date: str = ""
        self._prev_spot_depth: Dict[str, Tuple[List[Tuple[float, int]], List[Tuple[float, int]]]] = {}
        self._prev_opt_depth: Dict[str, Tuple[List[Tuple[float, int]], List[Tuple[float, int]]]] = {}
        self._bars_cache: Dict[str, Tuple[float, Dict[str, List[Any]]]] = {}
        self._load_baseline_oi()

    def _baseline_oi_path(self) -> Path:
        today_str = datetime.datetime.now(IST).strftime("%Y-%m-%d")
        return AI_DIR / "runtime" / f"oi_baseline_{today_str}.json"

    def _load_baseline_oi(self) -> None:
        today_str = datetime.datetime.now(IST).strftime("%Y-%m-%d")
        self._baseline_oi_date = today_str
        p = self._baseline_oi_path()
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._baseline_oi = {str(k): int(v) for k, v in data.items()}
            except Exception:
                self._baseline_oi = {}

    def _save_baseline_oi(self) -> None:
        try:
            p = self._baseline_oi_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self._baseline_oi), encoding="utf-8")
        except Exception:
            pass

    def _load_credentials(self) -> Tuple[str, str]:
        api_key, access_token, _ = self.bridge.get_session_credentials()
        return api_key, access_token

    def _ensure_kite(self):
        if self._kite is not None:
            return self._kite
        if self._kite_init_error:
            return None

        try:
            from kiteconnect import KiteConnect
        except Exception as exc:
            self._kite_init_error = f"kiteconnect import error: {exc}"
            return None

        api_key, access_token = self._load_credentials()
        if not api_key or not access_token:
            self._kite_init_error = "Missing Zerodha Kite credentials"
            return None

        try:
            kite = KiteConnect(api_key=api_key, timeout=6)
            kite.set_access_token(access_token)
            self._kite = kite
            return self._kite
        except Exception as exc:
            self._kite_init_error = f"KiteConnect init failed: {exc}"
            return None

    def _ensure_instruments(self) -> bool:
        if self._instruments_loaded:
            return bool(self._nfo_opts)

        today_str = datetime.datetime.now(IST).strftime("%Y-%m-%d")
        inst_path = self.bridge.get_instruments_path(today_str)
        if inst_path is None or not inst_path.exists():
            return False

        try:
            payload = json.loads(inst_path.read_text(encoding="utf-8"))
            raw_list = payload.get("raw", payload) if isinstance(payload, dict) else payload
            if not isinstance(raw_list, list):
                return False

            by_name_exp: Dict[str, Dict[str, List[dict]]] = {}
            for item in raw_list:
                seg = item.get("segment")
                if seg == "NSE" and item.get("instrument_type") == "EQ":
                    tsym = item.get("tradingsymbol", "")
                    if tsym:
                        self._spot_tokens[f"NSE:{tsym}"] = int(item["instrument_token"])
                elif seg == "INDICES":
                    tsym = item.get("tradingsymbol", "")
                    if tsym:
                        self._spot_tokens[f"NSE:{tsym}"] = int(item["instrument_token"])
                elif seg == "NFO-OPT" and item.get("instrument_type") in ("CE", "PE"):
                    name = str(item.get("name", "")).upper()
                    exp = str(item.get("expiry", ""))
                    if not name or not exp or exp < today_str:
                        continue
                    by_name_exp.setdefault(name, {}).setdefault(exp, []).append(item)

            for sym, exp_dict in by_name_exp.items():
                nearest = min(exp_dict.keys())
                contracts = exp_dict[nearest]
                self._nfo_opts[sym] = exp_dict
                self._nearest_expiry[sym] = nearest
                self._lot_sizes[sym] = int(contracts[0].get("lot_size", 1))
                strikes = sorted({float(c["strike"]) for c in contracts})
                if len(strikes) >= 2:
                    mid_idx = len(strikes) // 2
                    local_diffs = [
                        round(strikes[i + 1] - strikes[i], 2)
                        for i in range(max(0, mid_idx - 5), min(len(strikes) - 1, mid_idx + 5))
                        if strikes[i + 1] > strikes[i]
                    ]
                    self._strike_steps[sym] = min(local_diffs) if local_diffs else round(strikes[1] - strikes[0], 2)
                else:
                    self._strike_steps[sym] = 10.0

            self._instruments_loaded = True
            return bool(self._nfo_opts)
        except Exception:
            self._instruments_loaded = True
            return False

    def get_symbol_meta(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Returns real expiry, lot size, strike step, DTE, and spot metadata from instruments.json."""
        sym = symbol.upper()
        if not self._ensure_instruments() or sym not in self._nfo_opts:
            return None

        spot_sym = INDEX_SPOT_MAP[sym][0] if sym in INDEX_SPOT_MAP else f"NSE:{sym}"
        spot_tok = self._spot_tokens.get(spot_sym) or (INDEX_SPOT_MAP[sym][1] if sym in INDEX_SPOT_MAP else None)
        expiry_str = self._nearest_expiry[sym]
        today_d = datetime.datetime.now(IST).date()
        try:
            exp_d = datetime.date.fromisoformat(expiry_str)
            dte_days = max(0, (exp_d - today_d).days)
        except Exception:
            dte_days = 7

        return {
            "symbol": sym,
            "spot_symbol": spot_sym,
            "spot_token": spot_tok,
            "expiry": expiry_str,
            "dte_days": dte_days,
            "lot_size": self._lot_sizes[sym],
            "strike_step": self._strike_steps[sym],
        }

    def get_available_universe(self, candidates: List[str]) -> List[str]:
        """Filters candidate symbols to those with active NFO-OPT contracts in instruments.json."""
        if not self._ensure_instruments():
            return []
        return [s for s in candidates if s.upper() in self._nfo_opts]

    def get_full_fno_universe(self, include_indices: bool = False) -> List[str]:
        """Returns ALL active NSE F&O optionable underlyings from instruments.json (~224 symbols)."""
        if not self._ensure_instruments():
            return []
        if include_indices:
            return sorted(self._nfo_opts.keys())
        return sorted(s for s in self._nfo_opts.keys() if s not in INDEX_SPOT_MAP and f"NSE:{s}" in self._spot_tokens)

    def _fetch_spot_5m_ohlcv(self, spot_symbol: str, spot_token: Optional[int], days: int = 2) -> Dict[str, List[Any]]:
        """Fetches recent 5-minute OHLCV bars (cached 60s per symbol)."""
        cache_key = f"{spot_symbol}:{days}"
        now_ts = time.time()
        cached = self._bars_cache.get(cache_key)
        if cached and (now_ts - cached[0]) < 60.0:
            return cached[1]

        kite = self._ensure_kite()
        if kite is None or not spot_token:
            return {"closes": [], "highs": [], "lows": [], "volumes": [], "timestamps": []}

        try:
            now_ist = datetime.datetime.now(IST)
            from_dt = now_ist - datetime.timedelta(days=days)
            bars = kite.historical_data(spot_token, from_dt, now_ist, "5minute")
            parsed = {
                "closes": [float(b["close"]) for b in bars if b.get("close")],
                "highs": [float(b.get("high") or b["close"]) for b in bars if b.get("close")],
                "lows": [float(b.get("low") or b["close"]) for b in bars if b.get("close")],
                "volumes": [int(b.get("volume", 0)) for b in bars if b.get("close")],
                "timestamps": [b["date"].isoformat() if hasattr(b["date"], "isoformat") else str(b["date"]) for b in bars if b.get("close")],
            }
            if parsed["closes"]:
                self._bars_cache[cache_key] = (now_ts, parsed)
            return parsed
        except Exception:
            return {"closes": [], "highs": [], "lows": [], "volumes": [], "timestamps": []}

    def _fetch_spot_5m_bars(self, spot_symbol: str, spot_token: Optional[int]) -> Tuple[List[float], List[int]]:
        """Backward-compatible wrapper returning `(closes, volumes)`."""
        ohlcv = self._fetch_spot_5m_ohlcv(spot_symbol, spot_token, days=2)
        return ohlcv["closes"], ohlcv["volumes"]

    def fetch_historical_5m_bars(self, symbol: str, days: int = 25, use_cache: bool = True) -> List[Dict[str, Any]]:
        """Fetches multi-day 5m OHLCV bars for non-curve-fit out-of-sample Logic Testing."""
        meta = self.get_symbol_meta(symbol)
        if not meta or not meta.get("spot_token"):
            return []

        cache_dir = AI_DIR / "runtime" / "history_cache"
        today_str = datetime.datetime.now(IST).strftime("%Y-%m-%d")
        cache_file = cache_dir / f"{meta['symbol']}_{days}d_{today_str}.json"
        if use_cache and cache_file.exists():
            try:
                return json.loads(cache_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        kite = self._ensure_kite()
        if kite is None:
            return []

        try:
            now_ist = datetime.datetime.now(IST)
            from_dt = now_ist - datetime.timedelta(days=days)
            bars = kite.historical_data(meta["spot_token"], from_dt, now_ist, "5minute")
            out = [
                {
                    "date": b["date"].isoformat() if hasattr(b["date"], "isoformat") else str(b["date"]),
                    "open": float(b["open"]),
                    "high": float(b["high"]),
                    "low": float(b["low"]),
                    "close": float(b["close"]),
                    "volume": int(b.get("volume", 0)),
                }
                for b in bars
                if b.get("close")
            ]
            if out and use_cache:
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps(out), encoding="utf-8")
            return out
        except Exception:
            return []

    def fetch_live_market_breadth(self, universe: List[str]) -> Dict[str, Any]:
        """
        Queries all universe stocks + benchmark indices in ONE batched `kite.quote()` call (~80ms)
        to compute real live NSE Advances/Declines, Sector Moves, and Volume Shockers.
        """
        kite = self._ensure_kite()
        if kite is None or not self._ensure_instruments():
            return {"status": "UNAVAILABLE"}

        req_keys = ["NSE:NIFTY 50", "NSE:NIFTY BANK", "NSE:INDIA VIX"]
        sym_to_key: Dict[str, str] = {}
        for sym in universe:
            up = sym.upper()
            if up in INDEX_SPOT_MAP:
                continue
            key = f"NSE:{up}"
            sym_to_key[up] = key
            req_keys.append(key)

        quotes: Dict[str, Any] = {}
        try:
            for start in range(0, len(req_keys), 350):
                chunk = req_keys[start : start + 350]
                quotes.update(kite.quote(chunk) or {})
        except Exception:
            if not quotes:
                return {"status": "UNAVAILABLE"}

        advances = 0
        declines = 0
        unchanged = 0
        stock_moves: List[Dict[str, Any]] = []

        for up, key in sym_to_key.items():
            q = quotes.get(key) or {}
            ltp = float(q.get("last_price", 0.0) or 0.0)
            prev_c = float((q.get("ohlc") or {}).get("close", 0.0) or 0.0)
            vol = int(q.get("volume", 0) or 0)
            vwap = float(q.get("average_price") or ltp)
            buy_q = int(q.get("buy_quantity", 0) or 0)
            sell_q = int(q.get("sell_quantity", 0) or 0)
            if ltp <= 0 or prev_c <= 0:
                continue
            chg_pct = ((ltp - prev_c) / prev_c) * 100.0
            turnover_cr = round((vol * ltp) / 1e7, 2)
            l1_imb = round((buy_q - sell_q) / max(1, buy_q + sell_q), 3) if (buy_q + sell_q) > 0 else 0.0
            if chg_pct > 0.10:
                advances += 1
            elif chg_pct < -0.10:
                declines += 1
            else:
                unchanged += 1
            stock_moves.append({
                "symbol": up,
                "ltp": round(ltp, 2),
                "prev_close": round(prev_c, 2),
                "chg_pct": round(chg_pct, 2),
                "vwap": round(vwap, 2),
                "above_vwap": ltp > vwap,
                "volume": vol,
                "turnover_cr": turnover_cr,
                "spot_l1_imb": l1_imb,
            })

        def _idx_summary(k: str) -> Dict[str, float]:
            q = quotes.get(k) or {}
            ltp = float(q.get("last_price", 0.0) or 0.0)
            pc = float((q.get("ohlc") or {}).get("close", 0.0) or 0.0)
            chg = ((ltp - pc) / pc * 100.0) if (ltp > 0 and pc > 0) else 0.0
            return {"ltp": round(ltp, 2), "prev_close": round(pc, 2), "chg_pct": round(chg, 2)}

        stock_moves.sort(key=lambda x: x["chg_pct"], reverse=True)
        ad_ratio = round(advances / max(1, declines), 2) if (advances + declines) > 0 else 1.0

        return {
            "status": "LIVE_KITE_BATCH",
            "advances": advances,
            "declines": declines,
            "unchanged": unchanged,
            "ad_ratio": ad_ratio,
            "indices": {
                "NIFTY 50": _idx_summary("NSE:NIFTY 50"),
                "NIFTY BANK": _idx_summary("NSE:NIFTY BANK"),
                "INDIA VIX": _idx_summary("NSE:INDIA VIX"),
            },
            "stock_moves": stock_moves,
        }

    def fetch_spot_and_option_chain(
        self,
        symbol: str,
        num_strikes: int = 6,
    ) -> Optional[LiveMarketSnapshot]:
        """
        Fetches genuine live spot quote, 5-level depth, 5m OHLCV bars, and nearest-expiry NFO chain.
        Returns None on any failure (strict fail-closed).
        """
        kite = self._ensure_kite()
        meta = self.get_symbol_meta(symbol)
        if kite is None or meta is None:
            return None

        today_str = datetime.datetime.now(IST).strftime("%Y-%m-%d")
        if today_str != self._baseline_oi_date:
            self._baseline_oi.clear()
            self._load_baseline_oi()

        sym = meta["symbol"]
        spot_sym = meta["spot_symbol"]
        expiry = meta["expiry"]

        try:
            spot_quotes = kite.quote([spot_sym])
            sq = spot_quotes.get(spot_sym)
            if not sq or float(sq.get("last_price", 0.0)) <= 0.0:
                return None

            spot_price = float(sq["last_price"])
            vwap_live = float(sq.get("average_price") or spot_price)
            prev_close = float((sq.get("ohlc") or {}).get("close") or spot_price)
            spot_vol_today = int(sq.get("volume", 0) or 0)
            spot_buy_q = int(sq.get("buy_quantity", 0) or 0)
            spot_sell_q = int(sq.get("sell_quantity", 0) or 0)
            bid_d, ask_d = _extract_5level_depth(sq.get("depth"))
            prev_bid_d, prev_ask_d = self._prev_spot_depth.get(spot_sym, ([], []))
            if bid_d or ask_d:
                self._prev_spot_depth[spot_sym] = (bid_d, ask_d)
        except Exception:
            return None

        contracts = self._nfo_opts[sym][expiry]
        all_strikes = sorted({float(c["strike"]) for c in contracts})
        if not all_strikes:
            return None

        nearest_ranked = sorted(all_strikes, key=lambda k: abs(k - spot_price))
        selected_strikes = sorted(nearest_ranked[: max(3, num_strikes * 2 + 1)])
        strike_set = set(selected_strikes)

        strike_contracts: Dict[Tuple[float, str], str] = {}
        quote_keys: List[str] = []
        for c in contracts:
            k = float(c["strike"])
            otype = c["instrument_type"]
            if k in strike_set and otype in ("CE", "PE"):
                tsym = c["tradingsymbol"]
                strike_contracts[(k, otype)] = tsym
                quote_keys.append(f"NFO:{tsym}")

        if not quote_keys:
            return None

        try:
            opt_quotes = kite.quote(quote_keys)
        except Exception:
            return None

        baseline_updated = False
        chain: List[OptionStrikeData] = []
        for k in selected_strikes:
            ce_tsym = strike_contracts.get((k, "CE"))
            pe_tsym = strike_contracts.get((k, "PE"))
            ce_q = opt_quotes.get(f"NFO:{ce_tsym}", {}) if ce_tsym else {}
            pe_q = opt_quotes.get(f"NFO:{pe_tsym}", {}) if pe_tsym else {}

            ce_ltp = float(ce_q.get("last_price", 0.0) or 0.0)
            pe_ltp = float(pe_q.get("last_price", 0.0) or 0.0)
            ce_oi = int(ce_q.get("oi", 0) or 0)
            pe_oi = int(pe_q.get("oi", 0) or 0)

            ce_chg = 0
            if ce_tsym and ce_oi > 0:
                if ce_tsym not in self._baseline_oi:
                    self._baseline_oi[ce_tsym] = ce_oi
                    baseline_updated = True
                ce_chg = ce_oi - self._baseline_oi[ce_tsym]

            pe_chg = 0
            if pe_tsym and pe_oi > 0:
                if pe_tsym not in self._baseline_oi:
                    self._baseline_oi[pe_tsym] = pe_oi
                    baseline_updated = True
                pe_chg = pe_oi - self._baseline_oi[pe_tsym]

            ce_bid_d, ce_ask_d = _extract_5level_depth(ce_q.get("depth"))
            pe_bid_d, pe_ask_d = _extract_5level_depth(pe_q.get("depth"))
            ce_bid = ce_bid_d[0][0] if ce_bid_d else 0.0
            ce_ask = ce_ask_d[0][0] if ce_ask_d else 0.0
            pe_bid = pe_bid_d[0][0] if pe_bid_d else 0.0
            pe_ask = pe_ask_d[0][0] if pe_ask_d else 0.0

            chain.append(
                OptionStrikeData(
                    strike=k,
                    call_oi=ce_oi,
                    put_oi=pe_oi,
                    call_ltp=ce_ltp,
                    put_ltp=pe_ltp,
                    call_change_oi=ce_chg,
                    put_change_oi=pe_chg,
                    call_volume=int(ce_q.get("volume", 0) or 0),
                    put_volume=int(pe_q.get("volume", 0) or 0),
                    call_bid=ce_bid,
                    call_ask=ce_ask,
                    put_bid=pe_bid,
                    put_ask=pe_ask,
                )
            )

        if baseline_updated:
            self._save_baseline_oi()

        ohlcv = self._fetch_spot_5m_ohlcv(spot_sym, meta.get("spot_token"), days=2)
        return LiveMarketSnapshot(
            symbol=sym,
            spot_symbol=spot_sym,
            spot_price=spot_price,
            vwap_live=vwap_live,
            prev_close=prev_close,
            lot_size=meta["lot_size"],
            strike_step=meta["strike_step"],
            expiry=expiry,
            chain=chain,
            contract_map=strike_contracts,
            price_history=ohlcv["closes"],
            volume_history=ohlcv["volumes"],
            fetched_at=time.time(),
            high_history=ohlcv["highs"],
            low_history=ohlcv["lows"],
            spot_bid_depth=bid_d,
            spot_ask_depth=ask_d,
            prev_spot_bid_depth=prev_bid_d,
            prev_spot_ask_depth=prev_ask_d,
            spot_volume_today=spot_vol_today,
            spot_buy_qty=spot_buy_q,
            spot_sell_qty=spot_sell_q,
        )

    def fetch_contract_quotes(self, tradingsymbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Fetches real live NFO quotes (ltp, bid, ask, 5-level depth, oi, volume) for option contracts.
        Returns {} if Kite unavailable.
        """
        kite = self._ensure_kite()
        if kite is None or not tradingsymbols:
            return {}

        nfo_keys = [f"NFO:{s}" if not s.startswith("NFO:") else s for s in tradingsymbols]
        try:
            raw = kite.quote(nfo_keys)
        except Exception:
            return {}

        parsed: Dict[str, Dict[str, Any]] = {}
        for full_key, q in raw.items():
            short = full_key.replace("NFO:", "", 1)
            bid_d, ask_d = _extract_5level_depth(q.get("depth"))
            prev_bd, prev_ad = self._prev_opt_depth.get(short, ([], []))
            if bid_d or ask_d:
                self._prev_opt_depth[short] = (bid_d, ask_d)
            bid = bid_d[0][0] if bid_d else 0.0
            ask = ask_d[0][0] if ask_d else 0.0
            ltp = float(q.get("last_price", 0.0) or 0.0)
            parsed[short] = {
                "ltp": ltp,
                "bid": bid,
                "ask": ask,
                "oi": int(q.get("oi", 0) or 0),
                "volume": int(q.get("volume", 0) or 0),
                "mark": bid if bid > 0 else ltp,
                "bid_depth": bid_d,
                "ask_depth": ask_d,
                "prev_bid_depth": prev_bd,
                "prev_ask_depth": prev_ad,
            }
        return parsed
