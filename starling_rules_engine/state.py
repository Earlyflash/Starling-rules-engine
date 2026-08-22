"""Local on-disk state: last poll time + a ledger of processed feed items.

This is what makes the engine idempotent across runs (each cron
invocation) and lets it enforce a rolling daily transfer cap. It
intentionally does not use a database - a single JSON file is enough for
a personal, single-account tool, and it's easy to inspect or hand-edit
for an audit trail.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class ProcessedRecord:
    feed_item_uid: str
    processed_at: str  # ISO 8601, UTC
    outcome: str  # "transferred" | "skipped_cap" | "skipped_dry_run" | "error"
    amount_minor_units: int
    detail: str = ""


class State:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._data = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        else:
            data = {}
        # setdefault rather than a fixed literal so state.json files written
        # before a given key existed (e.g. claimed_outbound) still load fine.
        data.setdefault("last_poll_at", None)
        data.setdefault("processed", {})
        data.setdefault("claimed_outbound", {})
        return data

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(self._path.parent), prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, sort_keys=True)
            os.replace(tmp_path, self._path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    @property
    def last_poll_at(self) -> Optional[str]:
        return self._data.get("last_poll_at")

    def set_last_poll_at(self, ts: str) -> None:
        self._data["last_poll_at"] = ts

    def is_processed(self, feed_item_uid: str) -> bool:
        return feed_item_uid in self._data["processed"]

    def record(self, record: ProcessedRecord) -> None:
        self._data["processed"][record.feed_item_uid] = {
            "processed_at": record.processed_at,
            "outcome": record.outcome,
            "amount_minor_units": record.amount_minor_units,
            "detail": record.detail,
        }

    def transferred_total_today_minor_units(self, today: str) -> int:
        """Sum of amounts already transferred (outcome == 'transferred')
        whose `processed_at` falls on the given ISO date (YYYY-MM-DD)."""
        total = 0
        for rec in self._data["processed"].values():
            if rec["outcome"] == "transferred" and rec["processed_at"].startswith(today):
                total += rec["amount_minor_units"]
        return total

    def claimed_outbound_uids(self) -> frozenset[str]:
        """feed_item_uids of outbound payments already matched against some
        inbound reimbursement - see reconciler.match_payments."""
        return frozenset(self._data["claimed_outbound"].keys())

    def claim_outbound(self, outbound_feed_item_uid: str, inbound_feed_item_uid: str, claimed_at: str) -> None:
        """Record that an outbound payment has been matched to an inbound
        reimbursement, so it's never matched against a *different* inbound
        item later (e.g. two reimbursements of the same amount, one manual
        payment)."""
        self._data["claimed_outbound"][outbound_feed_item_uid] = {
            "matched_inbound_feed_item_uid": inbound_feed_item_uid,
            "claimed_at": claimed_at,
        }
