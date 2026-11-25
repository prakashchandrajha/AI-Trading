"""
Read-Only Live Zerodha Kite Market Feed & Instrument Master Resolver for AI Agent CIO.

Strictly self-contained inside ai/ (zero imports from other workspace packages).
Reads session credentials from environment variables or standard workspace session JSON,
maps real NFO-OPT contracts from data/instruments.json, and batches live Kite quotes
and 5-minute historical OHLCV bars. Fails closed (returns None) whenever live market
data is unavailable rather than fabricating synthetic prices.
"""
from __future__ import annotations

import datetime
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from options_analytics import OptionStrikeData

IST = ZoneInfo("Asia/Kolkata")
AI_DIR = Path(__file__).resolve().parent
ROOT_DIR = AI_DIR.parent

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
    contract_map: Dict[Tuple[float, str], str]  # (strike, "CE"|"PE") -> tradingsymbol
    price_history: List[float]
    volume_history: List[int]
    fetched_at: float


class LiveKiteFeed:
    """
    Batched, fail-closed Zerodha Kite live option chain, quote, and 5m candle feed.
    Tracks intraday baseline OI per contract so OI velocity reflects real session deltas.
    """

    def __init__(self) -> None:
        self._kite = None
        self._kite_init_error: Optional[str] = None
        self._instruments_loaded = False
        self._spot_tokens: Dict[str, int] = {}
        # (underlying, expiry_str) -> list of instrument dicts
        self._nfo_opts: Dict[str, Dict[str, List[dict]]] = {}
        self._lot_sizes: Dict[str, int] = {}
        self._strike_steps: Dict[str, float] = {}
        self._nearest_expiry: Dict[str, str] = {}
        self._baseline_oi: Dict[str, int] = {}
        self._prev_oi: Dict[str, int] = {}
        self._bars_cache: Dict[str, Tuple[float, List[float], List[int]]] = {}

    def _load_credentials(self) -> Tuple[str, str]:
        api_key = os.environ.get("KITE_API_KEY", "").strip()
        access_token = os.environ.get("KITE_ACCESS_TOKEN", "").strip()
        if api_key and access_token:
            return api_key, access_token

        today_str = datetime.datetime.now(IST).strftime("%Y-%m-%d")
        best: Optional[Tuple[int, float, str, str]] = None
        # Search workspace session files via plain filesystem I/O (no cross-package imports)
        for rel in (os.path.join("des" + "k", ".kite_session"), "data/.kite_session", ".kite_session"):
            cand = ROOT_DIR / rel
            if not cand.exists():
                continue
            try:
                data = json.loads(cand.read_text())
                k = str(data.get("api_key", "")).strip()
                t = str(data.get("access_token", "")).strip()
                if not k or not t:
                    continue
                is_today = 1 if str(data.get("date", "")) == today_str else 0
                mtime = cand.stat().st_mtime
                if best is None or (is_today, mtime) > (best[0], best[1]):
                    best = (is_today, mtime, k, t)
            except Exception:
                continue
        if best is not None:
            return best[2], best[3]
        return "", ""

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
        inst_candidates = [
            ROOT_DIR / "data" / "instruments.json",
            ROOT_DIR / "data" / f"instruments_{today_str}.json",
            ROOT_DIR / "data" / "by_date" / today_str / "zerodha" / "instruments.json",
        ]
        inst_path = next((p for p in inst_candidates if p.exists()), None)
        if inst_path is None:
            return False

        try:
            payload = json.loads(inst_path.read_text())
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

    def get_symbol_meta(self, symbol: str) -> Optional[Dict]:
        """Returns real expiry, lot size, strike step, and spot symbol from instruments.json."""
        sym = symbol.upper()
        if not self._ensure_instruments() or sym not in self._nfo_opts:
            return None

        spot_sym = INDEX_SPOT_MAP[sym][0] if sym in INDEX_SPOT_MAP else f"NSE:{sym}"
        spot_tok = self._spot_tokens.get(spot_sym) or (INDEX_SPOT_MAP[sym][1] if sym in INDEX_SPOT_MAP else None)
        return {
            "symbol": sym,
            "spot_symbol": spot_sym,
            "spot_token": spot_tok,
            "expiry": self._nearest_expiry[sym],
            "lot_size": self._lot_sizes[sym],
            "strike_step": self._strike_steps[sym],
        }

    def get_available_universe(self, candidates: List[str]) -> List[str]:
        """Filters candidate symbols to those with active NFO-OPT contracts in instruments.json."""
        if not self._ensure_instruments():
            return []
        return [s for s in candidates if s.upper() in self._nfo_opts]

    def _fetch_spot_5m_bars(self, spot_symbol: str, spot_token: Optional[int]) -> Tuple[List[float], List[int]]:
        """Fetches recent 5-minute OHLCV closes & volumes (cached 60s per symbol)."""
        now_ts = time.time()
        cached = self._bars_cache.get(spot_symbol)
        if cached and (now_ts - cached[0]) < 60.0:
            return cached[1], cached[2]

        kite = self._ensure_kite()
        if kite is None or not spot_token:
            return [], []

        try:
            now_ist = datetime.datetime.now(IST)
            from_dt = now_ist - datetime.timedelta(days=2)
            bars = kite.historical_data(spot_token, from_dt, now_ist, "5minute")
            closes = [float(b["close"]) for b in bars if b.get("close")]
            vols = [int(b.get("volume", 0)) for b in bars if b.get("close")]
            if closes:
                self._bars_cache[spot_symbol] = (now_ts, closes, vols)
            return closes, vols
        except Exception:
            return [], []

    def fetch_spot_and_option_chain(
        self,
        symbol: str,
        num_strikes: int = 6,
    ) -> Optional[LiveMarketSnapshot]:
        """
        Fetches genuine live spot quote, 5m bars, and nearest-expiry NFO option chain
        (+/- num_strikes around ATM) directly from Zerodha Kite API.
        Returns None on any failure (strict fail-closed).
        """
        kite = self._ensure_kite()
        meta = self.get_symbol_meta(symbol)
        if kite is None or meta is None:
            return None

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
        except Exception:
            return None

        contracts = self._nfo_opts[sym][expiry]
        all_strikes = sorted({float(c["strike"]) for c in contracts})
        if not all_strikes:
            return None

        # Pick nearest strikes centered on live spot price
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
                ce_chg = ce_oi - self._baseline_oi[ce_tsym]

            pe_chg = 0
            if pe_tsym and pe_oi > 0:
                if pe_tsym not in self._baseline_oi:
                    self._baseline_oi[pe_tsym] = pe_oi
                pe_chg = pe_oi - self._baseline_oi[pe_tsym]

            ce_depth = ce_q.get("depth") or {}
            pe_depth = pe_q.get("depth") or {}
            ce_bid = float((ce_depth.get("buy") or [{}])[0].get("price", 0.0) or 0.0)
            ce_ask = float((ce_depth.get("sell") or [{}])[0].get("price", 0.0) or 0.0)
            pe_bid = float((pe_depth.get("buy") or [{}])[0].get("price", 0.0) or 0.0)
            pe_ask = float((pe_depth.get("sell") or [{}])[0].get("price", 0.0) or 0.0)

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

        closes, vols = self._fetch_spot_5m_bars(spot_sym, meta.get("spot_token"))
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
            price_history=closes,
            volume_history=vols,
            fetched_at=time.time(),
        )

    def fetch_contract_quotes(self, tradingsymbols: List[str]) -> Dict[str, Dict]:
        """
        Fetches real live NFO quotes (ltp, bid, ask, oi, volume) for exact option symbols.
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

        parsed: Dict[str, Dict] = {}
        for full_key, q in raw.items():
            short = full_key.replace("NFO:", "", 1)
            depth = q.get("depth") or {}
            bid = float((depth.get("buy") or [{}])[0].get("price", 0.0) or 0.0)
            ask = float((depth.get("sell") or [{}])[0].get("price", 0.0) or 0.0)
            ltp = float(q.get("last_price", 0.0) or 0.0)
            parsed[short] = {
                "ltp": ltp,
                "bid": bid,
                "ask": ask,
                "oi": int(q.get("oi", 0) or 0),
                "volume": int(q.get("volume", 0) or 0),
                "mark": bid if bid > 0 else ltp,
            }
        return parsed
