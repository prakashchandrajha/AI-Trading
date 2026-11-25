from __future__ import annotations

import re
from typing import Any

from models.schema import CompanySnapshot, RawScreenerResult, ScreenCandidate


def parse_number(value: str | None) -> float | None:
    if not value:
        return None
    raw = str(value).strip()
    # Detect magnitude multipliers BEFORE stripping them
    multiplier = 1
    raw_lower = raw.lower()
    if "cr" in raw_lower:
        multiplier = 10_000_000  # 1 Crore = 10 million
    elif "lac" in raw_lower:
        multiplier = 100_000  # 1 Lac = 100 thousand
    is_pct = "%" in raw
    cleaned = raw.replace(",", "").replace("%", "").replace("Cr.", "").replace("cr", "").replace("₹", "").replace("Rs.", "").replace("Lacs", "").replace(" ", "")
    match = re.search(r"-?\d+(\.\d+)?", cleaned)
    if not match:
        return None
    result = float(match.group(0)) * multiplier
    # Note: We do NOT divide by 100 for percentages here because callers
    # already expect raw percentage values (e.g., ROCE=15.0 means 15%).
    return result


def first_metric(row: dict[str, Any], names: list[str]) -> str | None:
    lowered = {str(key).strip().lower(): value for key, value in row.items()}
    for name in names:
        target = name.lower()
        if target in lowered:
            return str(lowered[target])
    for name in names:
        target = name.lower()
        for key, value in lowered.items():
            if target == "debt" and "days" in key:
                continue
            if target == "debt" and "net debt" in key:
                continue
            if target in ("pe", "p/e") and "peg" in key:
                continue
            if re.search(r'\b' + re.escape(target) + r'\b', key):
                return str(value)
    return None


def symbol_from_href(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"/company/([^/]+)/?", value)
    return match.group(1).upper() if match else None


def parse_screen_candidates(raw: RawScreenerResult) -> list[ScreenCandidate]:
    if not raw.tables:
        return []

    rows = raw.tables[0].get("rows", [])
    candidates: list[ScreenCandidate] = []
    
    for row in rows[:50]:
        name = first_metric(row, ["name", "company"]) or "Unknown"
        # Skip header rows that leaked through DOM extraction
        if name.strip().lower() in ("name", "company", "unknown", "s.no."):
            continue
        symbol = first_metric(row, ["symbol", "nse code", "bse code"]) or symbol_from_href(row.get("_href"))
        
        candidate = ScreenCandidate(
            name=name,
            symbol=symbol,
            metrics={str(key): str(value) for key, value in row.items()},
        )
        candidates.append(candidate)
    return candidates


def parse_company_snapshot(raw: RawScreenerResult, symbol: str) -> CompanySnapshot:
    return CompanySnapshot(
        symbol=symbol.upper(),
        name=raw.title,
        ratios=raw.ratios,
        announcements=raw.announcements,
        financial_tables=raw.tables,
    )
