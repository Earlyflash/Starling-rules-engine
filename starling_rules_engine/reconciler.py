"""Matches inbound employer reimbursements against outbound payments to the
credit card payee - used two ways:

1. By engine.py, before auto-paying a matched reimbursement, to check
   whether it's already been settled by a manual payment (so it isn't
   duplicated).
2. By reconcile.py, as a full-window report of what paired up over a
   period and what didn't.

Matching is by (currency, amount) plus closest transaction date within a
configurable window - not just "does an outbound payment of this amount
exist", because several reimbursements can share the same amount (e.g.
three identical mileage claims), and each must only ever be paired with
one outbound payment. Once an outbound item is used to match one inbound
item, it's removed from the pool and can't be reused for another.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .matcher import SETTLED_STATUSES
from .starling_client import FeedItem

# Reference prefix engine.py puts on every payment it makes itself (see
# engine.py's _handle_match). Used here to tell "this outbound payment is
# one I already made automatically" apart from "this is a manual payment
# the user made by hand" - the two must be treated differently: an
# already-auto-paid item must never be re-matched as if it were a manual
# payment covering a *different* reimbursement (that would wrongly let a
# second reimbursement of the same amount go unpaid).
AUTO_PAYMENT_REFERENCE_PREFIX = "REIMB "


def parse_transaction_time(ts: str) -> datetime:
    """Parse a Starling `transactionTime` value ("2019-10-25T12:34:56.789Z" etc)."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def is_card_payment(item: FeedItem, credit_card_payee_name: str) -> bool:
    """True if `item` is a settled outbound payment to the configured credit card payee.

    Doesn't distinguish auto-paid from manual - see `is_manual_card_payment`
    for that.
    """
    if item.direction != "OUT":
        return False
    if item.status not in SETTLED_STATUSES:
        return False
    return item.counter_party_name.strip().casefold() == credit_card_payee_name.strip().casefold()


def is_manual_card_payment(item: FeedItem, credit_card_payee_name: str) -> bool:
    """True if `item` is a settled outbound card payment *not* made by this tool."""
    return is_card_payment(item, credit_card_payee_name) and not item.reference.startswith(
        AUTO_PAYMENT_REFERENCE_PREFIX
    )


@dataclass
class MatchedPair:
    inbound: FeedItem
    outbound: FeedItem
    day_gap: float  # absolute days between the two transaction times


@dataclass
class MatchResult:
    matched: list[MatchedPair]
    unmatched_inbound: list[FeedItem]
    unmatched_outbound: list[FeedItem]


def match_payments(
    inbound: list[FeedItem],
    outbound: list[FeedItem],
    window_days: int,
    *,
    excluded_outbound_uids: frozenset[str] = frozenset(),
) -> MatchResult:
    """Greedily pair each inbound item with the closest-in-time outbound item
    of the same (currency, amount) within `window_days`, each outbound item
    usable at most once. Inbound items are matched in chronological order so
    results are deterministic.

    `excluded_outbound_uids` lets a caller exclude outbound items already
    claimed by a previous match (see state.py's claimed_outbound ledger) -
    otherwise the same manual payment could be matched against two different
    reimbursements of the same amount across separate runs.
    """
    pool: dict[tuple[str, int], list[FeedItem]] = {}
    for o in outbound:
        if o.feed_item_uid in excluded_outbound_uids:
            continue
        pool.setdefault((o.currency, o.amount_minor_units), []).append(o)

    matched: list[MatchedPair] = []
    unmatched_inbound: list[FeedItem] = []
    used_outbound_uids: set[str] = set()

    for item in sorted(inbound, key=lambda i: i.transaction_time):
        candidates = pool.get((item.currency, item.amount_minor_units), [])
        item_dt = parse_transaction_time(item.transaction_time)

        best: FeedItem | None = None
        best_gap: float | None = None
        for candidate in candidates:
            if candidate.feed_item_uid in used_outbound_uids:
                continue
            gap_days = abs((parse_transaction_time(candidate.transaction_time) - item_dt).total_seconds()) / 86400
            if gap_days <= window_days and (best is None or gap_days < best_gap):
                best, best_gap = candidate, gap_days

        if best is not None:
            used_outbound_uids.add(best.feed_item_uid)
            matched.append(MatchedPair(inbound=item, outbound=best, day_gap=best_gap))
        else:
            unmatched_inbound.append(item)

    unmatched_outbound = [
        o for uids in pool.values() for o in uids if o.feed_item_uid not in used_outbound_uids
    ]
    return MatchResult(matched=matched, unmatched_inbound=unmatched_inbound, unmatched_outbound=unmatched_outbound)
