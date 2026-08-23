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

KNOWN GAP - auto vs manual payments are not reliably distinguishable
--------------------------------------------------------------------
`is_manual_card_payment` was designed to exclude payments this tool made
itself from the "manual payment" pool engine.py checks before auto-paying
a match (an already-auto-paid outbound item must never be re-matched as
if it covered a *different*, later reimbursement of the same amount - that
would wrongly leave the second one unpaid). It did this via a reference
prefix (`AUTO_PAYMENT_REFERENCE_PREFIX`) engine.py used to put on every
payment it made. That stopped being possible once payments need to carry
a fixed, card-issuer-required reference (see config.yaml's
credit_card_payment_reference) instead of a per-payment generated one -
engine.py no longer sets this prefix, so `AUTO_PAYMENT_REFERENCE_PREFIX`
below is currently unused for its original purpose and kept only for
`reconcile.py`'s "auto vs manual" labelling of *historical* payments that
still carry it.

Practical effect: as of this comment, `is_manual_card_payment` cannot
reliably tell an auto-payment from a manual one. The failure mode this
causes is safe-but-annoying, not dangerous: if two reimbursements share
an amount, the engine may wrongly treat its own earlier auto-payment as
manual cover for the second one and skip it (recorded as
`skipped_already_paid`) - the second reimbursement then just sits unpaid
rather than being duplicated, visible via `reconcile.py`'s report as an
unexpected "unmatched inbound" or a "matched" pair with an implausible
day_gap. It does NOT risk a double-payment. Before relying on this for
multiple same-amount reimbursements in a given match_window_days, this
needs a real fix.

RULED OUT (tested against one real auto-payment vs one real manual
payment, both PUT to the same payee): `transactingApplicationUserUid`,
`source`, `sourceSubType`, and `counterPartyType` are all identical
between the two - `transactingApplicationUserUid` in particular is tied
to the account holder, not the initiating channel, so it can't be used to
tell "made via this tool's Personal Access Token" apart from "made
through the Starling app". None of the fields returned by
`GET .../feed/account/.../category/.../{feedItemUid}` looked like a
viable signal in this sample. A real fix likely means tracking our own
payments locally instead - e.g. recording the `paymentOrderUid` returned
by `make_local_payment` and correlating it forward - since the public API
doesn't expose a `feedItemUid` on `GET .../payment-order/{uid}/payments`
either, so there's no direct API-side link available to resolve this by
one extra lookup.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .matcher import SETTLED_STATUSES
from .starling_client import FeedItem

# See "KNOWN GAP" above - no longer set by engine.py, kept for
# reconcile.py's auto/manual labelling of payments made before this gap
# existed.
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
    """Pair inbound and outbound items of the same (currency, amount) within
    `window_days`, each side usable at most once - globally, by smallest
    date gap first, not by iterating inbound items in date order and
    letting each one grab whatever's available at the time.

    That distinction matters whenever more than one inbound item shares an
    amount: processing oldest-inbound-first lets an early item claim a
    "good enough" outbound candidate before a *later* inbound item that
    would actually have been the closer, more plausible match ever gets a
    turn - e.g. three identical £60.80 reimbursements and one manual card
    payment landing right after the second one should match the second,
    not the first just because it happened to be considered first. Sorting
    every valid candidate pair by gap before accepting any avoids that.

    `excluded_outbound_uids` lets a caller exclude outbound items already
    claimed by a previous match (see state.py's claimed_outbound ledger) -
    otherwise the same manual payment could be matched against two different
    reimbursements of the same amount across separate runs.
    """
    pool = [o for o in outbound if o.feed_item_uid not in excluded_outbound_uids]

    candidates: list[tuple[float, FeedItem, FeedItem]] = []
    for item in inbound:
        item_dt = parse_transaction_time(item.transaction_time)
        for candidate in pool:
            if (candidate.currency, candidate.amount_minor_units) != (item.currency, item.amount_minor_units):
                continue
            gap_days = abs((parse_transaction_time(candidate.transaction_time) - item_dt).total_seconds()) / 86400
            if gap_days <= window_days:
                candidates.append((gap_days, item, candidate))
    candidates.sort(key=lambda c: c[0])

    used_inbound_uids: set[str] = set()
    used_outbound_uids: set[str] = set()
    matched: list[MatchedPair] = []
    for gap_days, item, candidate in candidates:
        if item.feed_item_uid in used_inbound_uids or candidate.feed_item_uid in used_outbound_uids:
            continue
        used_inbound_uids.add(item.feed_item_uid)
        used_outbound_uids.add(candidate.feed_item_uid)
        matched.append(MatchedPair(inbound=item, outbound=candidate, day_gap=gap_days))

    matched.sort(key=lambda p: p.inbound.transaction_time)
    unmatched_inbound = sorted(
        (item for item in inbound if item.feed_item_uid not in used_inbound_uids),
        key=lambda i: i.transaction_time,
    )
    unmatched_outbound = [o for o in pool if o.feed_item_uid not in used_outbound_uids]
    return MatchResult(matched=matched, unmatched_inbound=unmatched_inbound, unmatched_outbound=unmatched_outbound)
