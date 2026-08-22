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
3. Each match is checked against your configured safety caps
   (`max_transfer_minor_units`, `max_daily_total_minor_units`). A match
   that would breach either cap is **skipped and alerted on**, never
   partially transferred.
4. If it passes the caps and `dry_run: false`, it makes a one-off payment
   of the same amount to your configured `credit_card_payee_name` (which
   must already exist as a saved payee in your Starling account).
5. Every outcome - transferred, skipped (cap), skipped (dry run), or
   error - is logged and, if `STARLING_RULES_ALERT_WEBHOOK_URL` is set,
   posted to that webhook (e.g. a Slack incoming webhook).
6. Each feed item is only ever processed once, tracked in `state.json` -
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
  Access Tokens. It needs read access to accounts/transactions/payees,
  plus whichever scope covers paying an existing saved payee - the token
  creation screen lists the available scopes; pick the minimum set that
  covers reading feed items, payees, and making a payment to an existing
  payee.
- `STARLING_RULES_ALERT_WEBHOOK_URL` (optional): a webhook that gets a
  message per transfer/skip/error.

## Before you enable real transfers

This project was scaffolded without network access to
`developer.starlingbank.com`, so the exact request/response shape used in
`starling_rules_engine/starling_client.py` - particularly
`make_local_payment` - is based on general knowledge of Starling's public
API, not a docs page checked at the time of writing. **Before setting
`dry_run: false`:**

1. Read `starling_rules_engine/starling_client.py`'s module docstring and
   the `make_local_payment` docstring for what to double check.
2. Cross-check the endpoint, request body, and required token scope
   against the current docs at <https://developer.starlingbank.com/docs>.
3. Ideally, set `sandbox: true` in `config.yaml`, use a **sandbox**
   personal access token, and watch a full run (with `dry_run: false`)
   move fake money in the sandbox before ever pointing this at your real
   account.
4. Run against your real account with `dry_run: true` for a while first
   and check the logs/alerts look right for real incoming payments.

## Running once

```
python -m starling_rules_engine
```

Add `-v` for verbose logging, or `--config`/`--env-file` to point at
different files.

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
- `starling_rules_engine/matcher.py` - decides if a feed item is an
  employer expense payment.
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
