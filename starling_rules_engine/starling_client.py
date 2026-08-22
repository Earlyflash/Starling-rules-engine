"""Thin wrapper around the Starling Bank Personal Access API (v2).

IMPORTANT - VERIFY BEFORE GOING LIVE
-------------------------------------
This client was written from general knowledge of Starling's public API.
The sandbox this project was scaffolded in could not reach
developer.starlingbank.com (blocked by network egress policy there) to
confirm current request/response schemas against the live docs. Before
setting `dry_run: false` in config.yaml, verify every endpoint and payload
shape below against https://developer.starlingbank.com/docs - ideally by
running once against Starling's *sandbox* environment (`sandbox: true` +
a sandbox personal access token) and inspecting the real responses.
Fields most likely to have drifted: the exact body shape of
`make_local_payment`, the reference length limit, and whether the feed
item `status` values checked in matcher.py are still current.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import requests

PRODUCTION_BASE_URL = "https://api.starlingbank.com/api/v2"
SANDBOX_BASE_URL = "https://api-sandbox.starlingbank.com/api/v2"


class StarlingApiError(RuntimeError):
    """Raised for any non-2xx response from the Starling API."""

    def __init__(self, method: str, url: str, status: int, body: str):
        super().__init__(f"{method} {url} -> {status}: {body[:500]}")
        self.status = status
        self.body = body


@dataclass
class Account:
    account_uid: str
    default_category: str
    currency: str
    name: str


@dataclass
class Payee:
    payee_uid: str
    name: str
    payee_account_uid: Optional[str]


@dataclass
class FeedItem:
    feed_item_uid: str
    amount_minor_units: int
    currency: str
    direction: str  # "IN" or "OUT"
    counter_party_name: str
    reference: str
    status: str
    transaction_time: str


class StarlingClient:
    def __init__(
        self,
        token: str,
        *,
        sandbox: bool = False,
        session: Optional[requests.Session] = None,
        timeout: float = 15.0,
    ):
        self._token = token
        self._base_url = SANDBOX_BASE_URL if sandbox else PRODUCTION_BASE_URL
        self._session = session or requests.Session()
        self._timeout = timeout

    def _request(self, method: str, path: str, *, params=None, json_body=None) -> Any:
        url = f"{self._base_url}{path}"
        resp = self._session.request(
            method,
            url,
            params=params,
            json=json_body,
            timeout=self._timeout,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            },
        )
        if not resp.ok:
            raise StarlingApiError(method, url, resp.status_code, resp.text)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def list_accounts(self) -> list[Account]:
        data = self._request("GET", "/accounts")
        return [
            Account(
                account_uid=a["accountUid"],
                default_category=a["defaultCategory"],
                currency=a["currency"],
                name=a.get("name", ""),
            )
            for a in (data or {}).get("accounts", [])
        ]

    def list_payees(self) -> list[Payee]:
        data = self._request("GET", "/payees")
        payees = []
        for p in (data or {}).get("payees", []):
            accounts = p.get("accounts") or []
            default_account_uid = None
            for acc in accounts:
                if default_account_uid is None:
                    default_account_uid = acc.get("payeeAccountUid")
                if acc.get("defaultAccount"):
                    default_account_uid = acc.get("payeeAccountUid")
                    break
            payees.append(
                Payee(
                    payee_uid=p["payeeUid"],
                    name=p.get("payeeName", ""),
                    payee_account_uid=default_account_uid,
                )
            )
        return payees

    def list_feed_items_between(
        self, account_uid: str, category_uid: str, min_ts: str, max_ts: str
    ) -> list[FeedItem]:
        data = self._request(
            "GET",
            f"/feed/account/{account_uid}/category/{category_uid}/transactions-between",
            params={"minTransactionTimestamp": min_ts, "maxTransactionTimestamp": max_ts},
        )
        items = []
        for f in (data or {}).get("feedItems", []):
            amount = f.get("amount", {})
            items.append(
                FeedItem(
                    feed_item_uid=f["feedItemUid"],
                    amount_minor_units=amount.get("minorUnits", 0),
                    currency=amount.get("currency", ""),
                    direction=f.get("direction", ""),
                    counter_party_name=f.get("counterPartyName", "") or "",
                    reference=f.get("reference", "") or "",
                    status=f.get("status", ""),
                    transaction_time=f.get("transactionTime", ""),
                )
            )
        return items

    def make_local_payment(
        self,
        *,
        account_uid: str,
        category_uid: str,
        payee_uid: str,
        payee_account_uid: str,
        amount_minor_units: int,
        currency: str,
        reference: str,
    ) -> Any:
        """Initiate a one-off payment to an existing saved payee.

        VERIFY: confirm this is still `PUT
        /payments/local/account/{accountUid}/category/{categoryUid}` with
        this body shape, and whether Starling now expects/returns an
        idempotency identifier for this endpoint, before relying on this
        in production. Application-level idempotency is handled separately
        in state.py (each feed item is only ever processed once), which
        does not depend on this being correct - but a wrong body shape
        here will make every live transfer attempt fail loudly rather
        than silently, which is the safer failure mode.
        """
        body = {
            "amount": {"currency": currency, "minorUnits": amount_minor_units},
            "reference": reference[:18],  # Starling references have historically been short; confirm current limit
            "payeeUid": payee_uid,
            "payeeAccountUid": payee_account_uid,
        }
        return self._request(
            "PUT",
            f"/payments/local/account/{account_uid}/category/{category_uid}",
            json_body=body,
        )
