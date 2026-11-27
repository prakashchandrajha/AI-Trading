"""Fail-closed loading of the external-CIO trading authority and risk policy."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Tuple

import yaml


POLICY_PATH = Path(__file__).with_name("strategy.yaml")


class PolicyError(RuntimeError):
    """Raised when the trading authority policy is missing or unsafe."""


def _time_pair(raw: Any, field: str) -> Tuple[int, int]:
    try:
        hour, minute = (int(part) for part in str(raw).strip().split(":", 1))
    except (TypeError, ValueError) as exc:
        raise PolicyError(f"Invalid {field!r}; expected HH:MM") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise PolicyError(f"Invalid {field!r}; expected a valid 24-hour time")
    return hour, minute


_CAS_CUTOVER_YEAR, _CAS_CUTOVER_MONTH, _CAS_CUTOVER_DAY = 2026, 12, 7


@dataclass(frozen=True)
class TradingPolicy:
    execution_mode: str
    authorized_decision_makers: Tuple[str, ...]
    require_written_thesis: bool
    decision_journal_required: bool
    demat_budget_inr: float
    max_position_deployment_inr: float
    max_concurrent_positions: int
    max_single_trade_risk_inr: float
    daily_loss_limit_inr: float
    entry_cutoff: Tuple[int, int]
    eod_square_off: Tuple[int, int]
    target_venue: str = "PAPER"
    exchange_product: str = "NRML"
    vertical_spreads_enabled: bool = True
    max_spread_net_debit_inr: float = 14000.0
    physical_delivery_t4_blackout: bool = True

    @property
    def external_cio_only(self) -> bool:
        return self.execution_mode == "EXTERNAL_CIO_ONLY"

    @property
    def tier_1_super_setup_budget_inr(self) -> float:
        return self.max_position_deployment_inr

    @staticmethod
    def _coerce_ist_date(on_date: Any = None) -> "datetime.date":
        import datetime
        from zoneinfo import ZoneInfo

        d = on_date or datetime.datetime.now(ZoneInfo("Asia/Kolkata")).date()
        return d.date() if isinstance(d, datetime.datetime) else d

    @staticmethod
    def is_expiry_day(target_date: Any = None) -> bool:
        """
        Determines whether the given date is an NSE expiry day.
        Checks Tuesday (NIFTY weekly options per SEBI 2025 rule)
        and Last Thursday of Month (Monthly F&O).
        """
        import calendar
        import datetime

        d = TradingPolicy._coerce_ist_date(target_date)
        is_tue = d.weekday() == 1
        last_day = calendar.monthrange(d.year, d.month)[1]
        last_thu = last_day
        while datetime.date(d.year, d.month, last_thu).weekday() != 3:
            last_thu -= 1
        return is_tue or (d.day == last_thu)

    def get_market_close(self, on_date: Any = None) -> Tuple[int, int]:
        """Returns regular NSE F&O market close time (15:30 pre-CAS, 15:45 post-CAS cutover)."""
        import datetime

        target_date = self._coerce_ist_date(on_date)
        if target_date >= datetime.date(_CAS_CUTOVER_YEAR, _CAS_CUTOVER_MONTH, _CAS_CUTOVER_DAY):
            return (15, 45)
        return (15, 30)

    def get_eod_square_off(self, on_date: Any = None, is_expiry: Any = None) -> Tuple[int, int]:
        """
        Determines the EOD square-off cutoff time.
        Before 2026-12-07 (SEBI CAS cutover): returns (15, 15) per current schedule.
        From 2026-12-07 onward: returns (15, 20) on expiry day, or (15, 25) on non-expiry day,
        aligning with regular cash close at 15:30 and F&O close at 15:45.
        Respects stricter user-configured limits in strategy.yaml if set earlier than 15:15.
        """
        import datetime

        target_date = self._coerce_ist_date(on_date)
        cas_cutover_date = datetime.date(_CAS_CUTOVER_YEAR, _CAS_CUTOVER_MONTH, _CAS_CUTOVER_DAY)
        if target_date >= cas_cutover_date:
            if is_expiry is None:
                is_expiry = self.is_expiry_day(target_date)
            cas_time = (15, 20) if is_expiry else (15, 25)
            # If user explicitly configured an earlier square-off in YAML (e.g. 15:10), respect it
            return min(self.eod_square_off, cas_time) if self.eod_square_off < (15, 15) else cas_time
        return self.eod_square_off


@lru_cache(maxsize=1)
def get_trading_policy() -> TradingPolicy:
    """Load the repository policy and reject any autonomous-execution configuration."""
    try:
        raw: Dict[str, Any] = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise PolicyError(f"Cannot read trading policy: {POLICY_PATH}") from exc
    except yaml.YAMLError as exc:
        raise PolicyError(f"Invalid YAML in trading policy: {exc}") from exc

    authority = raw.get("external_cio_authority") or {}
    risk = raw.get("position_sizing_and_risk") or {}
    schedule = ((raw.get("active_trade_management") or {}).get("eod_square_off_schedule") or {})
    sebi_rules = ((raw.get("three_trader_zero_collision") or {}).get("collision_firewall") or {}).get("sebi_april_2026_rules") or {}

    if authority.get("execution_mode") != "EXTERNAL_CIO_ONLY":
        raise PolicyError("Trading is disabled unless execution_mode is EXTERNAL_CIO_ONLY")
    if authority.get("autonomous_entry_enabled") is not False:
        raise PolicyError("Trading is disabled unless autonomous_entry_enabled is false")
    if authority.get("autonomous_management_enabled") is not False:
        raise PolicyError("Trading is disabled unless autonomous_management_enabled is false")
    if authority.get("direct_order_mcp_enabled") is not False:
        raise PolicyError("Trading is disabled unless direct_order_mcp_enabled is false")

    try:
        budget = float(risk["demat_budget_inr"])
        max_positions = int(risk["max_concurrent_positions"])
        max_position_deployment = float(risk["tier_1_super_setup_budget_inr"])
        max_trade_risk = float(risk["max_single_trade_risk_inr"])
        daily_drawdown = float(risk["daily_drawdown_hard_halt_pct"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyError("Trading policy is missing a required risk limit") from exc
    if budget <= 0 or max_positions < 1 or max_position_deployment <= 0 or max_trade_risk <= 0 or not 0 < daily_drawdown < 1:
        raise PolicyError("Trading policy contains an invalid risk limit")

    daily_loss_limit = round(budget * daily_drawdown, 2)
    if max_trade_risk > daily_loss_limit:
        raise PolicyError("max_single_trade_risk_inr cannot exceed the daily loss limit")
    if max_position_deployment > budget:
        raise PolicyError("tier_1_super_setup_budget_inr cannot exceed the Demat budget")

    max_spread_debit = float(risk.get("max_spread_net_debit_inr", 14000.0))
    if max_spread_debit <= 0:
        raise PolicyError("max_spread_net_debit_inr must be positive")

    target_venue = str(authority.get("target_venue") or "PAPER").strip().upper()
    if target_venue not in ("PAPER", "LIVE"):
        raise PolicyError(f"Invalid target_venue {target_venue!r}; expected 'PAPER' or 'LIVE'")

    exchange_product = str(authority.get("exchange_product") or "NRML").strip().upper()
    if exchange_product not in ("NRML", "MIS"):
        raise PolicyError(f"Invalid exchange_product {exchange_product!r}; expected 'NRML' or 'MIS'")

    authorized = tuple(str(item).upper() for item in authority.get("authorized_decision_makers") or ())
    if not authorized:
        raise PolicyError("Trading policy must name at least one external decision maker")

    return TradingPolicy(
        execution_mode="EXTERNAL_CIO_ONLY",
        authorized_decision_makers=authorized,
        require_written_thesis=bool(authority.get("require_written_thesis")),
        decision_journal_required=bool(authority.get("decision_journal_required")),
        demat_budget_inr=budget,
        max_position_deployment_inr=max_position_deployment,
        max_concurrent_positions=max_positions,
        max_single_trade_risk_inr=max_trade_risk,
        daily_loss_limit_inr=daily_loss_limit,
        entry_cutoff=_time_pair(schedule.get("cio_entry_cutoff"), "cio_entry_cutoff"),
        eod_square_off=_time_pair(schedule.get("hard_eod_square_off"), "hard_eod_square_off"),
        target_venue=target_venue,
        exchange_product=exchange_product,
        vertical_spreads_enabled=bool(risk.get("vertical_spreads_enabled", True)),
        max_spread_net_debit_inr=max_spread_debit,
        physical_delivery_t4_blackout=bool(sebi_rules.get("physical_delivery_t4_blackout", True)),
    )
