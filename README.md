# starling-rules-engine

Watches a Starling Bank personal account for incoming payments from your
employer (expense reimbursements) and automatically transfers a matching
amount to a credit card payee - so the reimbursement doesn't just sit in
your current account while the expense sits unpaid on the card.

**This moves real money automatically. Read "Before you enable real
transfers" below before setting `dry_run: false`.**

## How it works

1. On each run, it fetches Starling feed items on your account since the
   last run (or the last `poll_lookback_minutes` on the very first run).
2. Any settled, inbound item whose counterparty name matches one of your
   configured `employer_names` is treated as a match.
3. Before doing anything else with a match, it checks whether you've
   already paid the card off yourself for that amount within
   `reconciliation.match_window_days` days either side (see
   "Reconciliation and duplicate protection" below) - if so, it's skipped
   as already settled, never paid twice.
4. Otherwise, the match is checked against your configured safety caps
   (`max_transfer_minor_units`, `max_daily_total_minor_units`). A match
   that would breach either cap is **skipped and alerted on**, never
   partially transferred.
5. If it passes the caps and `dry_run: false`, it makes a one-off payment
   of the same amount to your configured `credit_card_payee_name` (which
   must already exist as a saved payee in your Starling account).
6. Every outcome - transferred, skipped (already paid), skipped (cap),
   skipped (dry run), or error - is logged and, if
   `STARLING_RULES_ALERT_WEBHOOK_URL` is set, posted to that webhook (e.g.
   a Slack incoming webhook).
7. Each feed item is only ever processed once, tracked in `state.json` -
   safe to re-run without double-paying.

It's a one-shot script, not a daemon: it's meant to be run on a schedule
(cron / systemd timer), see "Running on a schedule" below.

## Setup

```
pip install -r requirements.txt
cp config.example.yaml config.yaml
cp .env.example .env
```

Edit `config.yaml`:
- `employer_names`: how your employer's payments show up as the
  counterparty name in the Starling app/feed (e.g. payroll or expenses
  account name).
- `credit_card_payee_name`: must exactly match an existing saved payee's
  name in your Starling account. Add your credit card as a payee in the
  Starling app first if it isn't one already (this tool never creates
  payees - the Personal API can't do that).
- `safety.max_transfer_minor_units` / `safety.max_daily_total_minor_units`:
  hard caps, in pence. Set these to something you're comfortable with
  being moved without a manual step.
- Leave `dry_run: true` for now.

Edit `.env`:
- `STARLING_PERSONAL_ACCESS_TOKEN`: create one at
  <https://developer.starlingbank.com> under your account's Personal
  Access Tokens. Needs scopes `account:read`, `account-list:read`,
  `payee:read`, `transaction:read`, and - only if you intend to ever set
  `dry_run: false` - `pay-local:create`. (Starling's OpenAPI spec also
  names a `pay-local-once:create` scope on the payment endpoint, but it
  isn't offered as a selectable scope in the portal, so there's nothing
  to pick for it - see starling_client.py's module docstring.)
- `STARLING_RULES_ALERT_WEBHOOK_URL` (optional): a webhook that gets a
  message per transfer/skip/error.
- `STARLING_SIGNING_KEY_PASSPHRASE` (optional): only needed if you protect
  the signing private key below with a passphrase.

If you plan to set `dry_run: false`, also set `signing_key_uid` and
`signing_private_key_path` in `config.yaml` - see the "Before you enable
real transfers" section below for why and how.

## Before you enable real transfers

**Payment requests must be signed.** Beyond the personal access token,
Starling requires every payment-creation request to carry a detached
signature from an RSA key you register in the Developer Portal:

1. Generate a key pair:
   ```
   openssl genrsa -out starling-signing-private.pem 2048
   openssl rsa -in starling-signing-private.pem -pubout -out starling-signing-public.pem
   ```
2. Upload `starling-signing-public.pem` in the Developer Portal against
   your personal access token; it gives you back a key uid.
3. Set `signing_key_uid` (the uid from step 2) and
   `signing_private_key_path` (pointing at the *private* key from step 1)
   in `config.yaml`. Keep the private key file outside this repo, or at
   least somewhere gitignored (`*.pem`/`*.key` already are) - it's as
   sensitive as the access token.

`dry_run: true` never needs any of this - it never calls the payment
endpoint. The engine refuses to start with `dry_run: false` if
`signing_key_uid` / `signing_private_key_path` aren't both set.

**Before setting `dry_run: false` for real:**

1. Ideally, set `sandbox: true` in `config.yaml`, use a **sandbox**
   personal access token and a sandbox-registered signing key, and watch
   a full run (with `dry_run: false`) move fake money in the sandbox
   before ever pointing this at your real account.
2. Run against your real account with `dry_run: true` for a while first
   and check the logs/alerts look right for real incoming payments.

## Running once

```
python -m starling_rules_engine
```

Add `-v` for verbose logging, or `--config`/`--env-file` to point at
different files.

## Reconciliation and duplicate protection

If you sometimes pay the credit card off by hand instead of waiting for
this tool, the engine avoids double-paying: before auto-paying a matched
reimbursement, it looks for a settled payment you already made to
`credit_card_payee_name` of the *same amount*, within
`reconciliation.match_window_days` days either side (default 14 - see
config.example.yaml). If it finds one, the reimbursement is marked
`skipped_already_paid` instead of transferred, and that outbound payment
is recorded in `state.json` so it can't also be matched against a
*different* reimbursement of the same amount later. Set
`reconciliation.match_window_days: 0` to turn this off.

This only ever protects against a manual payment made *before* the engine
would otherwise have auto-paid - it can't see into the future, so a
manual payment made after the engine already auto-paid is a genuine
double-payment on your end, not something the engine could have caught.

Separately, `python -m starling_rules_engine.reconcile [--days 30]` is a
**read-only** report (no `dry_run: false` or signing key needed) that
pairs every reimbursement against every card payment (auto-paid or
manual) over a period, and prints what's matched, what reimbursements
have no card payment to show for them, and what card payments have no
reimbursement explaining them - useful as a sanity check independent of
what the live poller has or hasn't caught.

Add `--months N` instead of `--days` for a month-by-month breakdown -
inbound total, outbound total, how much was matchable, and what's left
unmatched on each side, per calendar month:

```
python -m starling_rules_engine.reconcile --months 3
```

## Running on a schedule

Example crontab entry, polling every 30 minutes:

```
*/30 * * * * cd /path/to/starling-rules-engine && /path/to/venv/bin/python -m starling_rules_engine >> cron.log 2>&1
```

## Running tests

```
python -m unittest discover -s tests -v
```

All tests mock the Starling client (`unittest.mock`) - none of them hit
the real API.

## Layout

- `starling_rules_engine/starling_client.py` - thin REST wrapper around
  the Starling Personal API (accounts, payees, feed items, payments).
- `starling_rules_engine/signing.py` - the RSA request-signing Starling
  requires for payment-creation requests specifically (everything else
  uses the plain access token).
- `starling_rules_engine/matcher.py` - decides if a feed item is an
  employer expense payment.
- `starling_rules_engine/reconciler.py` - matches inbound reimbursements
  against outbound card payments; shared by the engine's duplicate
  protection and by `reconcile.py`'s report.
- `starling_rules_engine/reconcile.py` - the read-only, on-demand
  reconciliation report (`python -m starling_rules_engine.reconcile`).
- `starling_rules_engine/safety.py` - the transfer/daily caps; the one
  place a match can be vetoed.
- `starling_rules_engine/state.py` - local JSON ledger for idempotency and
  the rolling daily cap.
- `starling_rules_engine/notifier.py` - logs + optional webhook alert for
  every outcome.
- `starling_rules_engine/engine.py` - ties the above together for one
  poll cycle.
- `starling_rules_engine/config.py` / `config.example.yaml` / `.env.example`
  - config loading; secrets always come from the environment, never the
    YAML file.
- `starling_rules_engine/__main__.py` - CLI entrypoint (`python -m
  starling_rules_engine`).

## Limitations

- Single account only - if your token can see more than one account, you
  must set `account_uid` explicitly in config.yaml.
- Transfers are all-or-nothing against the matched amount; it never
  transfers a partial amount to duck under a cap.
- Currently only pays a single configured credit card payee; multiple
  reimbursement destinations would need config/engine changes.
- No retry/backoff on transient Starling API errors within a run - a
  failed transfer is logged/alerted and picked up as a fresh match on the
  next scheduled run only if the feed item wasn't marked processed (it
  currently *is* marked as `error` and won't be retried automatically -
  re-run manually, or delete its entry from `state.json`, to retry).
