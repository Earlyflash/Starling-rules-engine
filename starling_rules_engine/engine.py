"""Orchestrates one poll cycle: fetch new feed items, match, safety-check, transfer.

Intended to be invoked once per run (see __main__.py) - all "since when"
state lives in state.py, not in this process, so runs can be scheduled via
cron/systemd timer with no in-memory state to lose between them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .config import Config
from .matcher import is_employer_payment
from .notifier import Notifier
from .reconciler import is_manual_card_payment, match_payments, parse_transaction_time
from .safety import SafetyRejection
from .safety import check as check_safety
from .starling_client import StarlingClient
from .state import ProcessedRecord, State

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_account(config: Config, client: StarlingClient):
    accounts = client.list_accounts()
    if not accounts:
        raise RuntimeError("Starling API returned no accounts for this token")

    if config.account_uid:
        account = next((a for a in accounts if a.account_uid == config.account_uid), None)
        if account is None:
            raise RuntimeError(f"account_uid {config.account_uid!r} not found among token's accounts")
        return account

    if len(accounts) > 1:
        raise RuntimeError(
            "token has access to multiple accounts; set account_uid explicitly in config.yaml "
            f"(candidates: {[a.account_uid for a in accounts]})"
        )
    return accounts[0]


def resolve_credit_card_payee(config: Config, client: StarlingClient):
    payees = client.list_payees()
    target = config.credit_card_payee_name.strip().casefold()
    payee = next((p for p in payees if p.name.strip().casefold() == target), None)
    if payee is None:
        raise RuntimeError(
            f"credit_card_payee_name {config.credit_card_payee_name!r} not found among saved payees "
            "- add it as a payee in the Starling app first"
        )
    if not payee.payee_account_uid:
        raise RuntimeError(
            f"payee {config.credit_card_payee_name!r} has no usable account on it - "
            "check it has a default account set in the Starling app"
        )
    return payee


def run_once(config: Config, client: StarlingClient, state: State, notifier: Notifier) -> None:
    account = resolve_account(config, client)
    payee = resolve_credit_card_payee(config, client)

    _recheck_pending_payments(client, state, notifier)

    now = datetime.now(timezone.utc)
    min_ts = _poll_window_start(config, state, now)
    max_ts = now.isoformat()

    items = client.list_feed_items_between(account.account_uid, account.default_category, min_ts, max_ts)
    log.info("fetched %d feed item(s) between %s and %s", len(items), min_ts, max_ts)

    today = now.date().isoformat()

    new_matches = [
        item
        for item in items
        if not state.is_processed(item.feed_item_uid) and is_employer_payment(item, config.employer_names)
    ]

    already_paid = _find_already_paid(config, client, state, account, payee, new_matches)

    for item in new_matches:
        pair = already_paid.get(item.feed_item_uid)
        if pair is not None:
            _handle_already_paid(state, notifier, item, pair)
            continue
        _handle_match(config, client, state, notifier, account, payee, item, today)

    state.set_last_poll_at(max_ts)
    state.save()


def _poll_window_start(config: Config, state: State, now: datetime) -> str:
    """Start of this run's feed fetch window.

    Deliberately reaches back `poll_overlap_minutes` before the previous
    run rather than starting exactly at it: a feed item's transactionTime
    can be earlier than when it actually appears in the feed (e.g. BACS
    credits are stamped 23:01 the day before but land hours later), and
    an item can still be PENDING at one poll and only SETTLED by a later
    one. Without the overlap, either case falls before the next window
    and is never seen again. Re-fetching is safe - state.is_processed
    stops any feed item being acted on twice.
    """
    if not state.last_poll_at:
        return (now - timedelta(minutes=config.poll_lookback_minutes)).isoformat()
    last_poll = datetime.fromisoformat(state.last_poll_at)
    return (last_poll - timedelta(minutes=config.poll_overlap_minutes)).isoformat()


def _check_settlement(client: StarlingClient, payment_order_uid: str) -> str:
    """Returns 'completed', 'rejected', or 'pending' for a payment order
    just created (or previously pending). Never assume a payment happened
    just because make_local_payment returned 200 - see get_payment_order_payments's
    docstring. If the status can't be determined (e.g. a transient API
    error on the follow-up check), returns 'pending' rather than risk a
    false 'completed'.
    """
    try:
        statuses = client.get_payment_order_payments(payment_order_uid)
    except Exception:
        log.exception("could not check settlement status for payment order %s", payment_order_uid)
        return "pending"

    if not statuses:
        return "pending"
    payment = statuses[0]  # a one-off local payment order has exactly one payment under it
    if payment.rejected_at or payment.payment_status == "REJECTED":
        return "rejected"
    if payment.completed_at:
        return "completed"
    return "pending"


def _recheck_pending_payments(client: StarlingClient, state: State, notifier: Notifier) -> None:
    """Follow up on payments from a previous run that were still pending
    (e.g. awaiting the account holder's approval) when we last checked -
    without this, a payment stuck pending forever would sit unresolved in
    state.json indefinitely with no record ever confirming or denying it.
    """
    for feed_item_uid, record in state.pending_review_records():
        payment_order_uid = record["detail"]
        if not payment_order_uid:
            continue
        status = _check_settlement(client, payment_order_uid)
        if status == "pending":
            continue  # still waiting - leave it, check again next run

        amount_minor_units = record["amount_minor_units"]
        if status == "completed":
            notifier.notify(
                "transferred",
                feed_item_uid=feed_item_uid,
                amount_minor_units=amount_minor_units,
                payment_order_uid=payment_order_uid,
                detail="resolved from pending_review",
            )
            state.record(
                ProcessedRecord(
                    feed_item_uid=feed_item_uid,
                    processed_at=_now_iso(),
                    outcome="transferred",
                    amount_minor_units=amount_minor_units,
                )
            )
        else:  # rejected
            notifier.notify(
                "payment_rejected",
                feed_item_uid=feed_item_uid,
                amount_minor_units=amount_minor_units,
                payment_order_uid=payment_order_uid,
                detail="resolved from pending_review",
            )
            state.record(
                ProcessedRecord(
                    feed_item_uid=feed_item_uid,
                    processed_at=_now_iso(),
                    outcome="rejected",
                    amount_minor_units=amount_minor_units,
                    detail=f"payment order {payment_order_uid} rejected",
                )
            )


def _find_already_paid(config, client, state, account, payee, new_matches) -> dict:
    """Check `new_matches` against recent settled card payments the user made
    by hand - intended to exclude ones this tool made itself, though see
    reconciler.py's "KNOWN GAP" docstring for why that exclusion currently
    doesn't reliably work - so a reimbursement someone already paid off
    manually doesn't also get auto-paid. Returns {inbound_feed_item_uid: MatchedPair}.
    """
    window_days = config.reconciliation_match_window_days
    if not new_matches or window_days <= 0:
        return {}

    earliest = min(parse_transaction_time(item.transaction_time) for item in new_matches)
    outbound_min_ts = (earliest - timedelta(days=window_days)).isoformat()
    outbound_max_ts = _now_iso()

    outbound_items = client.list_feed_items_between(
        account.account_uid, account.default_category, outbound_min_ts, outbound_max_ts
    )
    manual_payments = [o for o in outbound_items if is_manual_card_payment(o, payee.name)]

    result = match_payments(
        new_matches,
        manual_payments,
        window_days,
        excluded_outbound_uids=state.claimed_outbound_uids(),
    )
    return {pair.inbound.feed_item_uid: pair for pair in result.matched}


def _handle_already_paid(state, notifier, item, pair) -> None:
    notifier.notify(
        "skipped_already_paid",
        feed_item_uid=item.feed_item_uid,
        amount_minor_units=item.amount_minor_units,
        counterparty=item.counter_party_name,
        matched_outbound_feed_item_uid=pair.outbound.feed_item_uid,
        day_gap=round(pair.day_gap, 2),
    )
    state.record(
        ProcessedRecord(
            feed_item_uid=item.feed_item_uid,
            processed_at=_now_iso(),
            outcome="skipped_already_paid",
            amount_minor_units=item.amount_minor_units,
            detail=f"matched existing outbound payment {pair.outbound.feed_item_uid} ({pair.day_gap:.1f}d apart)",
        )
    )
    state.claim_outbound(pair.outbound.feed_item_uid, item.feed_item_uid, _now_iso())


def _handle_match(config, client, state, notifier, account, payee, item, today) -> None:
    already_today = state.transferred_total_today_minor_units(today)
    try:
        check_safety(config.safety, item.amount_minor_units, already_today)
    except SafetyRejection as exc:
        notifier.notify(
            "skipped_safety_cap",
            feed_item_uid=item.feed_item_uid,
            amount_minor_units=item.amount_minor_units,
            counterparty=item.counter_party_name,
            reason=exc.reason,
        )
        state.record(
            ProcessedRecord(
                feed_item_uid=item.feed_item_uid,
                processed_at=_now_iso(),
                outcome="skipped_cap",
                amount_minor_units=item.amount_minor_units,
                detail=exc.reason,
            )
        )
        return

    if config.dry_run:
        notifier.notify(
            "dry_run_would_transfer",
            feed_item_uid=item.feed_item_uid,
            amount_minor_units=item.amount_minor_units,
            counterparty=item.counter_party_name,
            payee=payee.name,
        )
        state.record(
            ProcessedRecord(
                feed_item_uid=item.feed_item_uid,
                processed_at=_now_iso(),
                outcome="skipped_dry_run",
                amount_minor_units=item.amount_minor_units,
                detail="dry_run enabled",
            )
        )
        return

    # Use the fixed reference the card issuer needs to apply the payment to
    # the right account (e.g. a card number) - NOT a per-payment generated
    # value. See reconciler.py's AUTO_PAYMENT_REFERENCE_PREFIX docstring:
    # this means "is this outbound payment one I made myself" can no longer
    # be told apart from a manual payment by reference alone.
    try:
        response = client.make_local_payment(
            account_uid=account.account_uid,
            category_uid=account.default_category,
            payee_account_uid=payee.payee_account_uid,
            amount_minor_units=item.amount_minor_units,
            currency=item.currency or config.currency,
            reference=config.credit_card_payment_reference,
            external_identifier=item.feed_item_uid,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced via notifier, never swallowed
        log.exception("payment failed for feed item %s", item.feed_item_uid)
        notifier.notify(
            "transfer_failed",
            feed_item_uid=item.feed_item_uid,
            amount_minor_units=item.amount_minor_units,
            error=str(exc),
        )
        state.record(
            ProcessedRecord(
                feed_item_uid=item.feed_item_uid,
                processed_at=_now_iso(),
                outcome="error",
                amount_minor_units=item.amount_minor_units,
                detail=str(exc),
            )
        )
        return

    # A 200 here only means Starling accepted the request, not that money
    # has moved - it can sit PENDING (e.g. awaiting approval in the app, or
    # a Faster Payments qualified-accept delay) for a while first. Check
    # before ever recording this as "transferred" - see _check_settlement.
    payment_order_uid = (response or {}).get("paymentOrderUid")
    status = _check_settlement(client, payment_order_uid) if payment_order_uid else "pending"

    if status == "pending":
        notifier.notify(
            "payment_pending_review",
            feed_item_uid=item.feed_item_uid,
            amount_minor_units=item.amount_minor_units,
            counterparty=item.counter_party_name,
            payment_order_uid=payment_order_uid,
            detail="Starling accepted the payment request but it has not settled yet - "
            "check the Starling app for an approval prompt; this will be re-checked next run",
        )
        state.record(
            ProcessedRecord(
                feed_item_uid=item.feed_item_uid,
                processed_at=_now_iso(),
                outcome="pending_review",
                amount_minor_units=item.amount_minor_units,
                detail=payment_order_uid or "",
            )
        )
        return

    if status == "rejected":
        notifier.notify(
            "payment_rejected",
            feed_item_uid=item.feed_item_uid,
            amount_minor_units=item.amount_minor_units,
            counterparty=item.counter_party_name,
            payment_order_uid=payment_order_uid,
        )
        state.record(
            ProcessedRecord(
                feed_item_uid=item.feed_item_uid,
                processed_at=_now_iso(),
                outcome="rejected",
                amount_minor_units=item.amount_minor_units,
                detail=f"payment order {payment_order_uid} rejected",
            )
        )
        return

    notifier.notify(
        "transferred",
        feed_item_uid=item.feed_item_uid,
        amount_minor_units=item.amount_minor_units,
        counterparty=item.counter_party_name,
        payee=payee.name,
    )
    state.record(
        ProcessedRecord(
            feed_item_uid=item.feed_item_uid,
            processed_at=_now_iso(),
            outcome="transferred",
            amount_minor_units=item.amount_minor_units,
        )
    )
