"""Decides whether an incoming Starling feed item is an employer expense payment."""

from __future__ import annotations

from .starling_client import FeedItem

# VERIFY: confirm these are still the current Starling feed item status
# values for a payment that has actually landed (see starling_client.py
# module docstring). Historically settled inbound Faster Payments report
# as SETTLED; UPCOMING/PENDING items haven't cleared yet and shouldn't be
# acted on.
SETTLED_STATUSES = {"SETTLED"}


def is_employer_payment(item: FeedItem, employer_names: list[str]) -> bool:
    """True if `item` looks like money in from one of `employer_names`.

    Only matches inbound (direction == "IN"), settled feed items - never
    acts on something still pending, declined, or reversed.
    """
    if item.direction != "IN":
        return False
    if item.status not in SETTLED_STATUSES:
        return False

    counterparty = item.counter_party_name.strip().casefold()
    if not counterparty:
        return False

    for name in employer_names:
        needle = name.strip().casefold()
        if not needle:
            continue
        if needle == counterparty or needle in counterparty:
            return True
    return False
