"""
NSE Institutional Intelligence Engine
Fetches, processes, and audits official clearing & settlement datasets from NSE Archives:
1. Participant-Wise Open Interest (FII vs Client vs Pro positioning, Index Long/Short ratio, Option trap matrix)
2. Cash Market Deliverable Quantity & Delivery Percentage (Institutional Accumulation vs Speculative Churn)
3. F&O Ban List (Securities under trading ban)
"""
from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("NSEInstitutionalIntel")
IST = ZoneInfo("Asia/Kolkata")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Accept": "*/*",
}


def _get_date_strings(days_back: int = 5) -> List[tuple[str, str]]:
    """Returns list of (DDMMYYYY, YYYY-MM-DD) tuples starting from today."""
    now = datetime.now(IST)
    pairs = []
    for offset in range(days_back):
        d = now - timedelta(days=offset)
        pairs.append((d.strftime("%d%m%Y"), d.strftime("%Y-%m-%d")))
    return pairs


def fetch_url(url: str, timeout: float = 6.0) -> Optional[str]:
    """Fetches text content with proper headers and short timeout."""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return resp.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        logger.debug("Fetch failed for %s: %s", url, exc)
    return None


def parse_participant_oi_csv(raw_csv: str) -> Optional[Dict[str, Any]]:
    """Parses official NSE Clearing fao_participant_oi CSV."""
    try:
        reader = csv.reader(io.StringIO(raw_csv))
        parsed: Dict[str, Dict[str, int]] = {}
        for row in reader:
            if not row or len(row) < 9:
                continue
            name = row[0].strip().replace('"', "")
            if name in ("Client", "DII", "FII", "Pro"):
                try:
                    parsed[name] = {
                        "fut_idx_long": int(row[1].strip()),
                        "fut_idx_short": int(row[2].strip()),
                        "opt_call_long": int(row[5].strip()),
                        "opt_put_long": int(row[6].strip()),
                        "opt_call_short": int(row[7].strip()),
                        "opt_put_short": int(row[8].strip()),
                    }
                except (ValueError, IndexError):
                    continue

        if not parsed:
            return None

        client = parsed.get("Client", {})
        fii = parsed.get("FII", {})
        pro = parsed.get("Pro", {})
        dii = parsed.get("DII", {})

        def calc_ratio(l: int, s: int) -> float:
            tot = l + s
            return round((l / tot) * 100, 1) if tot > 0 else 50.0

        fii_long_ratio = calc_ratio(fii.get("fut_idx_long", 0), fii.get("fut_idx_short", 0))
        client_long_ratio = calc_ratio(client.get("fut_idx_long", 0), client.get("fut_idx_short", 0))

        fii_net_fut = fii.get("fut_idx_long", 0) - fii.get("fut_idx_short", 0)
        client_net_fut = client.get("fut_idx_long", 0) - client.get("fut_idx_short", 0)
        pro_net_fut = pro.get("fut_idx_long", 0) - pro.get("fut_idx_short", 0)
        dii_net_fut = dii.get("fut_idx_long", 0) - dii.get("fut_idx_short", 0)

        fii_net_calls = fii.get("opt_call_long", 0) - fii.get("opt_call_short", 0)
        fii_net_puts = fii.get("opt_put_long", 0) - fii.get("opt_put_short", 0)

        client_net_calls = client.get("opt_call_long", 0) - client.get("opt_call_short", 0)
        client_net_puts = client.get("opt_put_long", 0) - client.get("opt_put_short", 0)

        pro_net_calls = pro.get("opt_call_long", 0) - pro.get("opt_call_short", 0)
        pro_net_puts = pro.get("opt_put_long", 0) - pro.get("opt_put_short", 0)

        # Strategic Bias Synthesis
        if fii_long_ratio < 25.0:
            fii_regime = "HEAVILY_OVERSOLD_SHORT_COVER_FUEL"
        elif fii_long_ratio > 75.0:
            fii_regime = "OVERBOUGHT_LONG_UNWIND_RISK"
        elif fii_long_ratio < 40.0:
            fii_regime = "BEARISH_LEAN"
        elif fii_long_ratio > 60.0:
            fii_regime = "BULLISH_LEAN"
        else:
            fii_regime = "NEUTRAL_BALANCED"

        if client_net_puts < -250_000 and fii_net_puts > 150_000:
            retail_trap = "HIGH_RETAIL_PUT_WRITER_TRAP"
        elif client_net_calls > 150_000 and fii_net_calls < -80_000:
            retail_trap = "RETAIL_CALL_BUYER_DECAY_TRAP"
        else:
            retail_trap = "BALANCED"

        return {
            "fii_index_long_ratio": fii_long_ratio,
            "fii_net_futures": fii_net_fut,
            "fii_net_calls": fii_net_calls,
            "fii_net_puts": fii_net_puts,
            "fii_regime": fii_regime,
            "client_index_long_ratio": client_long_ratio,
            "client_net_futures": client_net_fut,
            "client_net_calls": client_net_calls,
            "client_net_puts": client_net_puts,
            "retail_trap": retail_trap,
            "pro_net_futures": pro_net_fut,
            "pro_net_calls": pro_net_calls,
            "pro_net_puts": pro_net_puts,
            "dii_net_futures": dii_net_fut,
        }
    except Exception as exc:
        logger.error("Error parsing participant OI: %s", exc)
        return None


def parse_sec_bhavdata(raw_csv: str) -> Dict[str, Dict[str, Any]]:
    """Parses official NSE sec_bhavdata_full CSV for delivery percentage."""
    results: Dict[str, Dict[str, Any]] = {}
    try:
        reader = csv.DictReader(io.StringIO(raw_csv))
        for row in reader:
            series = (row.get(" SERIES") or row.get("SERIES") or "").strip()
            if series != "EQ":
                continue
            sym = (row.get("SYMBOL") or row.get(" SYMBOL") or "").strip().upper()
            if not sym:
                continue

            try:
                close = float((row.get(" CLOSE_PRICE") or row.get("CLOSE_PRICE") or "0").strip())
                ttl_qty = int((row.get(" TTL_TRD_QNTY") or row.get("TTL_TRD_QNTY") or "0").strip())
                deliv_qty = int((row.get(" DELIV_QTY") or row.get("DELIV_QTY") or "0").strip())
                deliv_pct = float((row.get(" DELIV_PER") or row.get("DELIV_PER") or "0").strip())
                trades = int((row.get(" NO_OF_TRADES") or row.get("NO_OF_TRADES") or "0").strip())

                if deliv_pct >= 60.0:
                    quality = "INSTITUTIONAL_ACCUMULATION"
                elif deliv_pct >= 45.0:
                    quality = "HEALTHY_DELIVERY"
                elif deliv_pct < 25.0:
                    quality = "SPECULATIVE_DAY_CHURN"
                else:
                    quality = "NORMAL"

                results[sym] = {
                    "close_price": close,
                    "total_traded_qty": ttl_qty,
                    "deliverable_qty": deliv_qty,
                    "delivery_pct": deliv_pct,
                    "trades_count": trades,
                    "delivery_quality": quality,
                }
            except (ValueError, TypeError):
                continue
    except Exception as exc:
        logger.error("Error parsing sec_bhavdata: %s", exc)
    return results


def parse_fno_ban(raw_text: str) -> List[str]:
    """Parses fo_secban.csv for banned securities."""
    banned = []
    try:
        for line in raw_text.splitlines():
            line = line.strip()
            if not line or "Securities in Ban" in line:
                # E.g. "Securities in Ban For Trade Date 26-NOV-2026: 1,APLAPOLLO 2,MANAPPURAM"
                parts = line.split(":")
                if len(parts) > 1:
                    tokens = parts[1].strip().split()
                    for tok in tokens:
                        if "," in tok:
                            sym = tok.split(",")[1].strip().upper()
                            if sym:
                                banned.append(sym)
                continue
            for item in line.split(","):
                clean = item.strip().upper()
                if clean and clean.isalpha() and clean not in ("SYMBOL", "NAME"):
                    banned.append(clean)
    except Exception as exc:
        logger.error("Error parsing F&O ban: %s", exc)
    return sorted(set(banned))


def update_nse_institutional_intel() -> Dict[str, Any]:
    """Fetches latest available NSE Clearing and Bhavcopy files and saves locally."""
    date_pairs = _get_date_strings(5)
    
    # 1. Fetch Participant OI
    participant_data = None
    poi_date = None
    for d_str, iso_date in date_pairs:
        url = f"https://archives.nseindia.com/content/nsccl/fao_participant_oi_{d_str}.csv"
        raw = fetch_url(url)
        if raw and ("Client" in raw or "Future Index" in raw):
            participant_data = parse_participant_oi_csv(raw)
            if participant_data:
                poi_date = iso_date
                logger.info("Found Participant OI for date %s", iso_date)
                break

    # 2. Fetch Security Bhavdata Delivery
    delivery_map = {}
    deliv_date = None
    for d_str, iso_date in date_pairs:
        url = f"https://archives.nseindia.com/products/content/sec_bhavdata_full_{d_str}.csv"
        raw = fetch_url(url)
        if raw and "DELIV_PER" in raw:
            delivery_map = parse_sec_bhavdata(raw)
            if delivery_map:
                deliv_date = iso_date
                logger.info("Found Delivery data for %d stocks on date %s", len(delivery_map), iso_date)
                break

    # 3. Fetch F&O Ban List
    ban_url = "https://archives.nseindia.com/content/fo/fo_secban.csv"
    ban_raw = fetch_url(ban_url)
    ban_list = parse_fno_ban(ban_raw) if ban_raw else []

    today_iso = datetime.now(IST).strftime("%Y-%m-%d")
    payload: Dict[str, Any] = {
        "status": "SUCCESS" if (participant_data and delivery_map) else "PARTIAL",
        "generated_at": datetime.now(IST).isoformat(),
        "today_trading_date": today_iso,
        "participant_oi_date": poi_date,
        "delivery_data_date": deliv_date,
        "fno_ban_list": ban_list,
        "participant_positioning": participant_data or {},
        "top_delivery_leaders": sorted(
            [{"symbol": s, **d} for s, d in delivery_map.items() if d.get("delivery_pct", 0) >= 55.0 and d.get("total_traded_qty", 0) > 500_000],
            key=lambda x: x["delivery_pct"],
            reverse=True,
        )[:20],
        "all_delivery_data": delivery_map,
    }

    # Save to AI-Trading runtime
    runtime_dir = Path(__file__).resolve().parent / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    out_file = runtime_dir / "nse_institutional_intel.json"
    out_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Mirror to SmartTrading by_date if available
    try:
        st_root = Path("/home/vvd/prakash/SmartTrading")
        st_dir = st_root / "data" / "by_date" / today_iso / "nse"
        if st_root.exists():
            st_dir.mkdir(parents=True, exist_ok=True)
            (st_dir / "institutional_intel.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass

    return payload


def get_nse_intel(symbol: Optional[str] = None) -> Dict[str, Any]:
    """Reads cached NSE institutional intelligence or updates if absent."""
    p = Path(__file__).resolve().parent / "runtime" / "nse_institutional_intel.json"
    if not p.exists():
        data = update_nse_institutional_intel()
    else:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = update_nse_institutional_intel()

    part = data.get("participant_positioning") or {}
    ban_list = data.get("fno_ban_list") or []

    if symbol:
        sym_clean = symbol.upper().strip()
        all_deliv = data.get("all_delivery_data") or {}
        deliv = all_deliv.get(sym_clean)
        is_banned = sym_clean in ban_list

        return {
            "status": "MATCHED_SYMBOL",
            "symbol": sym_clean,
            "is_in_fno_ban": is_banned,
            "delivery_data": deliv or {
                "delivery_pct": None,
                "delivery_quality": "UNKNOWN",
                "message": f"No delivery record found for {sym_clean}",
            },
            "macro_fii_long_ratio": part.get("fii_index_long_ratio"),
            "macro_fii_regime": part.get("fii_regime"),
            "macro_retail_trap": part.get("retail_trap"),
        }

    return {
        "status": "SUMMARY",
        "participant_oi_date": data.get("participant_oi_date"),
        "delivery_data_date": data.get("delivery_data_date"),
        "fno_ban_count": len(ban_list),
        "fno_ban_list": ban_list,
        "participant_positioning": part,
        "top_delivery_accumulation": [
            {
                "symbol": item["symbol"],
                "delivery_pct": item["delivery_pct"],
                "delivery_quality": item["delivery_quality"],
                "close_price": item["close_price"],
            }
            for item in data.get("top_delivery_leaders", [])[:8]
        ],
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Fetching and computing NSE Institutional Intelligence...")
    res = update_nse_institutional_intel()
    print("SUCCESS!")
    print("Participant Positioning:", json.dumps(res.get("participant_positioning"), indent=2))
    print("F&O Ban List:", res.get("fno_ban_list"))
    print("Top Accumulation Leaders:", [x["symbol"] + f" ({x['delivery_pct']}%)" for x in res.get("top_delivery_leaders", [])[:5]])
