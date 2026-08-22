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
from .safety import SafetyRejection
from .safety import check as check_safety
from .starling_client import StarlingClient
from .state import ProcessedRecord, State

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_account(config: Config, client: StarlingClient):
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


def _resolve_credit_card_payee(config: Config, client: StarlingClient):
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
    account = _resolve_account(config, client)
    payee = _resolve_credit_card_payee(config, client)

    now = datetime.now(timezone.utc)
    min_ts = state.last_poll_at or (now - timedelta(minutes=config.poll_lookback_minutes)).isoformat()
    max_ts = now.isoformat()

    items = client.list_feed_items_between(account.account_uid, account.default_category, min_ts, max_ts)
    log.info("fetched %d feed item(s) between %s and %s", len(items), min_ts, max_ts)

    today = now.date().isoformat()

    for item in items:
        if state.is_processed(item.feed_item_uid):
            continue
        if not is_employer_payment(item, config.employer_names):
            continue

        _handle_match(config, client, state, notifier, account, payee, item, today)

    state.set_last_poll_at(max_ts)
    state.save()


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

    reference = f"REIMB {item.feed_item_uid[:8]}"
    try:
        client.make_local_payment(
            account_uid=account.account_uid,
            category_uid=account.default_category,
            payee_uid=payee.payee_uid,
            payee_account_uid=payee.payee_account_uid,
            amount_minor_units=item.amount_minor_units,
            currency=item.currency or config.currency,
            reference=reference,
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
