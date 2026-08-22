"""Thin wrapper around the Starling Bank Personal Access API (v2).

Every endpoint here uses plain `Bearer` auth except `make_local_payment`,
whose `PUT /payments/local/account/{accountUid}/category/{categoryUid}`
needs Starling's detached request-signature scheme on top of the token -
see signing.py's docstring for the mechanics. Its body requires
`externalIdentifier` (an idempotency key) and `destinationPayeeAccountUid`
- for a Personal Access Token, the payment recipient can only be an
existing payee, addressed by its account uid.

Starling's OpenAPI spec lists two scopes on that endpoint,
`pay-local:create` and `pay-local-once:create`, but the token-creation
scope picker at developer.starlingbank.com only offers `pay-local:create`
- nothing to do about the second one from the token-creation side. If a
real payment ever fails with an insufficient-scope error naming
`pay-local-once:create`, that's the thing to chase down.

If you touch this file or signing.py, re-check both against Starling's
API docs/samples rather than assuming the shapes here are still accurate
- APIs drift.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

import requests

from .signing import SigningKey, sign_request

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
        signing_key: Optional[SigningKey] = None,
        session: Optional[requests.Session] = None,
        timeout: float = 15.0,
    ):
        self._token = token
        self._base_url = SANDBOX_BASE_URL if sandbox else PRODUCTION_BASE_URL
        self._signing_key = signing_key
        self._session = session or requests.Session()
        self._timeout = timeout

    def _request(
        self, method: str, path: str, *, params=None, raw_body: Optional[str] = None, extra_headers=None
    ) -> Any:
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        if raw_body is not None:
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)  # may override Authorization - see make_local_payment

        resp = self._session.request(
            method,
            url,
            params=params,
            data=raw_body,
            timeout=self._timeout,
            headers=headers,
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
        payee_account_uid: str,
        amount_minor_units: int,
        currency: str,
        reference: str,
        external_identifier: str,
    ) -> Any:
        """Initiate a one-off payment to an existing saved payee.

        `external_identifier` is Starling's own idempotency key for this
        request (required, <=100 chars) - separate from, and in addition
        to, the idempotency state.py already provides by only ever
        processing a given feed item once. Pass something derived from the
        feed item being reimbursed (e.g. its feed_item_uid) so a retried
        request for the same match can't double-pay even if state.json
        somehow disagrees with what Starling actually did.

        This is a `BearerAndSignature`-secured endpoint (see signing.py's
        docstring) - `self._signing_key` must be set, or this raises.
        Requires the `pay-local:create` and `pay-local-once:create`
        scopes on the personal access token.
        """
        if self._signing_key is None:
            raise RuntimeError(
                "make_local_payment called with no signing key configured "
                "(signing_key_uid / signing_private_key_path) - required to make real "
                "payments, see README.md 'Before you enable real transfers'"
            )

        path = f"/payments/local/account/{account_uid}/category/{category_uid}"
        body = {
            "externalIdentifier": external_identifier,
            "destinationPayeeAccountUid": payee_account_uid,
            "reference": reference[:18],  # 18-char limit confirmed for GBP/FPS payments; SEPA allows 35
            "amount": {"currency": currency, "minorUnits": amount_minor_units},
        }
        raw_body = json.dumps(body)
        signed = sign_request(
            method="PUT",
            base_url=self._base_url,
            path=path,
            body=raw_body,
            access_token=self._token,
            signing_key=self._signing_key,
        )
        return self._request(
            "PUT",
            path,
            raw_body=raw_body,
            extra_headers={
                "Date": signed.date,
                "Digest": signed.digest,
                "Authorization": signed.authorization,
            },
        )
