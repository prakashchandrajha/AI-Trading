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

    @property
    def external_cio_only(self) -> bool:
        return self.execution_mode == "EXTERNAL_CIO_ONLY"


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
    )
