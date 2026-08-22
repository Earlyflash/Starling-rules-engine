"""Read-only reconciliation report: pairs up inbound employer reimbursements
against outbound payments to the credit card payee over a period (both ones
this tool auto-paid and ones you paid manually), and flags anything left
over on either side - a reimbursement nothing seems to have paid off, or a
card payment with no reimbursement to explain it.

Doesn't touch state.json and never calls the payments API, so it doesn't
need `dry_run: false` or a signing key - safe to run any time:

    python -m starling_rules_engine.reconcile [--days 30]
    python -m starling_rules_engine.reconcile --months 3

`--months N` switches to a month-by-month breakdown instead of one flat
window - see build_monthly_report. Matching still runs once over the
*whole* fetched range before bucketing by month, so a reimbursement near a
month boundary can still be correctly paired with a manual payment that
landed in the neighbouring month; only pairs whose partner falls outside
the fetched range entirely (i.e. before window_start) can be missed - a
known edge effect of any windowed report, not specific to the monthly
mode.
"""

from __future__ import annotations

import argparse
import calendar
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from .config import Config, ConfigError, load_config
from .engine import resolve_account, resolve_credit_card_payee
from .matcher import is_employer_payment
from .reconciler import (
    AUTO_PAYMENT_REFERENCE_PREFIX,
    MatchedPair,
    is_card_payment,
    match_payments,
    parse_transaction_time,
)
from .starling_client import FeedItem, StarlingClient

log = logging.getLogger(__name__)


def _format_amount(minor_units: int, currency: str) -> str:
    symbol = {"GBP": "£", "EUR": "€", "USD": "$"}.get(currency, currency + " ")
    return f"{symbol}{minor_units / 100:,.2f}"


def months_ago(dt: datetime, months: int) -> datetime:
    """`dt` shifted back by `months` calendar months, clamping the day for
    shorter target months (e.g. 31 Mar minus 1 month -> 28/29 Feb)."""
    month_index = dt.month - 1 - months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


# --- flat (single-window) report -------------------------------------------------


@dataclass
class ReconciliationReport:
    window_start: str
    window_end: str
    matched: list[MatchedPair]
    unmatched_inbound: list[FeedItem]
    unmatched_outbound: list[FeedItem]
    total_inbound_minor_units: int
    total_outbound_minor_units: int


def build_report(
    inbound_items: list[FeedItem],
    outbound_items: list[FeedItem],
    window_days: int,
    window_start: str,
    window_end: str,
) -> ReconciliationReport:
    result = match_payments(inbound_items, outbound_items, window_days)
    return ReconciliationReport(
        window_start=window_start,
        window_end=window_end,
        matched=result.matched,
        unmatched_inbound=result.unmatched_inbound,
        unmatched_outbound=result.unmatched_outbound,
        total_inbound_minor_units=sum(i.amount_minor_units for i in inbound_items),
        total_outbound_minor_units=sum(o.amount_minor_units for o in outbound_items),
    )


def print_report(report: ReconciliationReport, currency: str) -> None:
    print(f"Reconciliation report: {report.window_start} -> {report.window_end}")
    print()

    print(f"Matched ({len(report.matched)}):")
    if not report.matched:
        print("  (none)")
    for pair in sorted(report.matched, key=lambda p: p.inbound.transaction_time):
        kind = "auto" if pair.outbound.reference.startswith(AUTO_PAYMENT_REFERENCE_PREFIX) else "manual"
        print(
            f"  {_format_amount(pair.inbound.amount_minor_units, pair.inbound.currency):>10}  "
            f"IN {pair.inbound.transaction_time}  <->  OUT {pair.outbound.transaction_time}  "
            f"[{kind}]  {pair.day_gap:.1f}d apart"
        )
    print()

    print(f"Unmatched inbound - reimbursement received, no matching card payment found ({len(report.unmatched_inbound)}):")
    if not report.unmatched_inbound:
        print("  (none)")
    for item in sorted(report.unmatched_inbound, key=lambda i: i.transaction_time):
        print(f"  {_format_amount(item.amount_minor_units, item.currency):>10}  {item.transaction_time}  {item.counter_party_name}")
    print()

    print(f"Unmatched outbound - card payment with no matching reimbursement ({len(report.unmatched_outbound)}):")
    if not report.unmatched_outbound:
        print("  (none)")
    for item in sorted(report.unmatched_outbound, key=lambda i: i.transaction_time):
        kind = "auto" if item.reference.startswith(AUTO_PAYMENT_REFERENCE_PREFIX) else "manual"
        print(f"  {_format_amount(item.amount_minor_units, item.currency):>10}  {item.transaction_time}  [{kind}]")
    print()

    diff = report.total_inbound_minor_units - report.total_outbound_minor_units
    print(
        f"Totals: inbound {_format_amount(report.total_inbound_minor_units, currency)}, "
        f"outbound {_format_amount(report.total_outbound_minor_units, currency)}, "
        f"difference {_format_amount(diff, currency)}"
    )


# --- monthly breakdown -------------------------------------------------


@dataclass
class MonthlyBucket:
    month: str  # "2026-08"
    inbound_total_minor_units: int = 0
    inbound_count: int = 0
    outbound_total_minor_units: int = 0
    outbound_count: int = 0
    matched_total_minor_units: int = 0  # how much of this month's inbound was matchable
    matched_count: int = 0
    unmatched_inbound_total_minor_units: int = 0
    unmatched_inbound_count: int = 0
    unmatched_outbound_total_minor_units: int = 0
    unmatched_outbound_count: int = 0


def _month_key(item: FeedItem) -> str:
    return parse_transaction_time(item.transaction_time).strftime("%Y-%m")


def build_monthly_report(
    inbound_items: list[FeedItem], outbound_items: list[FeedItem], window_days: int
) -> list[MonthlyBucket]:
    """Groups inbound/outbound totals, and how much of each month's inbound
    was matchable against a card payment, by calendar month. Matching runs
    once over the full item set first (see module docstring on why), then
    results are bucketed - a matched pair is attributed to the *inbound*
    item's month, since "how much of this month's reimbursements got
    reconciled" is usually the more useful question than the outbound
    payment's month.
    """
    result = match_payments(inbound_items, outbound_items, window_days)

    months = sorted({_month_key(i) for i in inbound_items} | {_month_key(o) for o in outbound_items})
    buckets = {m: MonthlyBucket(month=m) for m in months}

    for item in inbound_items:
        b = buckets[_month_key(item)]
        b.inbound_total_minor_units += item.amount_minor_units
        b.inbound_count += 1

    for item in outbound_items:
        b = buckets[_month_key(item)]
        b.outbound_total_minor_units += item.amount_minor_units
        b.outbound_count += 1

    for pair in result.matched:
        b = buckets[_month_key(pair.inbound)]
        b.matched_total_minor_units += pair.inbound.amount_minor_units
        b.matched_count += 1

    for item in result.unmatched_inbound:
        b = buckets[_month_key(item)]
        b.unmatched_inbound_total_minor_units += item.amount_minor_units
        b.unmatched_inbound_count += 1

    for item in result.unmatched_outbound:
        b = buckets[_month_key(item)]
        b.unmatched_outbound_total_minor_units += item.amount_minor_units
        b.unmatched_outbound_count += 1

    return [buckets[m] for m in months]


def _bucket_row(b: MonthlyBucket, currency: str) -> str:
    def cell(total: int, count: int) -> str:
        return f"{_format_amount(total, currency)} ({count})"

    return (
        f"{b.month:<9} "
        f"{cell(b.inbound_total_minor_units, b.inbound_count):>16} "
        f"{cell(b.outbound_total_minor_units, b.outbound_count):>16} "
        f"{cell(b.matched_total_minor_units, b.matched_count):>16} "
        f"{cell(b.unmatched_inbound_total_minor_units, b.unmatched_inbound_count):>16} "
        f"{cell(b.unmatched_outbound_total_minor_units, b.unmatched_outbound_count):>16}"
    )


def print_monthly_report(buckets: list[MonthlyBucket], currency: str) -> None:
    header = (
        f"{'Month':<9} {'Inbound':>16} {'Outbound':>16} {'Matched':>16} "
        f"{'Unmatched In':>16} {'Unmatched Out':>16}"
    )
    print(header)
    print("-" * len(header))

    if not buckets:
        print("  (no matching feed items in range)")
        return

    for b in buckets:
        print(_bucket_row(b, currency))

    total = MonthlyBucket(month="Total")
    for b in buckets:
        total.inbound_total_minor_units += b.inbound_total_minor_units
        total.inbound_count += b.inbound_count
        total.outbound_total_minor_units += b.outbound_total_minor_units
        total.outbound_count += b.outbound_count
        total.matched_total_minor_units += b.matched_total_minor_units
        total.matched_count += b.matched_count
        total.unmatched_inbound_total_minor_units += b.unmatched_inbound_total_minor_units
        total.unmatched_inbound_count += b.unmatched_inbound_count
        total.unmatched_outbound_total_minor_units += b.unmatched_outbound_total_minor_units
        total.unmatched_outbound_count += b.unmatched_outbound_count

    print("-" * len(header))
    print(_bucket_row(total, currency))
    print()
    print("Matched = this month's reimbursements paired with a card payment (auto or manual) within the")
    print("reconciliation window. Unmatched In = reimbursements with no card payment found yet - these are")
    print("either pending auto-payment or need a look. Unmatched Out = card payments with no reimbursement")
    print("behind them - may just be unrelated spending.")


# --- fetching + CLI -------------------------------------------------


def _fetch_items(config: Config, client: StarlingClient, window_start: str, window_end: str):
    account = resolve_account(config, client)
    payee = resolve_credit_card_payee(config, client)
    items = client.list_feed_items_between(account.account_uid, account.default_category, window_start, window_end)
    inbound = [i for i in items if is_employer_payment(i, config.employer_names)]
    outbound = [i for i in items if is_card_payment(i, payee.name)]
    return inbound, outbound


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile employer reimbursements against credit card payments over a period. Read-only."
    )
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    window_group = parser.add_mutually_exclusive_group()
    window_group.add_argument("--days", type=int, default=None, help="how many days back to look (default 30)")
    window_group.add_argument(
        "--months", type=int, default=None, help="show a month-by-month breakdown over this many months instead"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.env_file.exists():
        load_dotenv(args.env_file)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    client = StarlingClient(config.starling_token, sandbox=config.sandbox)

    now = datetime.now(timezone.utc)
    if args.months is not None:
        window_start_dt = months_ago(now, args.months)
    else:
        window_start_dt = now - timedelta(days=args.days if args.days is not None else 30)
    window_start = window_start_dt.isoformat()
    window_end = now.isoformat()

    try:
        inbound, outbound = _fetch_items(config, client, window_start, window_end)
    except Exception as exc:  # noqa: BLE001 - this is a report tool, always show what broke
        log.exception("reconciliation failed")
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.months is not None:
        buckets = build_monthly_report(inbound, outbound, config.reconciliation_match_window_days)
        print(f"Monthly reconciliation report: {window_start} -> {window_end}")
        print()
        print_monthly_report(buckets, config.currency)
    else:
        report = build_report(inbound, outbound, config.reconciliation_match_window_days, window_start, window_end)
        print_report(report, config.currency)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
