"""Safety-net checks applied before any real money moves.

Kept deliberately separate from engine.py so the caps are easy to unit
test in isolation and easy to audit as the one place that can veto a
transfer.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SafetyLimits:
    max_transfer_minor_units: int
    max_daily_total_minor_units: int


class SafetyRejection(Exception):
    """Raised when a matched payment must NOT be transferred as-is.

    The engine never transfers a partial amount to stay under a cap - a
    rejection always means "skip this one and alert", never "transfer
    less".
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def check(
    limits: SafetyLimits,
    amount_minor_units: int,
    already_transferred_today_minor_units: int,
) -> None:
    if amount_minor_units <= 0:
        raise SafetyRejection(f"non-positive amount {amount_minor_units}")
    if amount_minor_units > limits.max_transfer_minor_units:
        raise SafetyRejection(
            f"amount {amount_minor_units} exceeds max_transfer_minor_units "
            f"{limits.max_transfer_minor_units}"
        )
    if already_transferred_today_minor_units + amount_minor_units > limits.max_daily_total_minor_units:
        raise SafetyRejection(
            f"would exceed max_daily_total_minor_units {limits.max_daily_total_minor_units} "
            f"(already transferred {already_transferred_today_minor_units} today)"
        )
