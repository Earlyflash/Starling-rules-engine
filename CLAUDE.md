# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A small Python CLI (`starling_rules_engine/`) that polls a Starling Bank
personal account for employer expense reimbursements and, when caps
allow, automatically pays the matching amount to a credit card saved as a
Starling payee. See README.md for setup/usage; the "Architecture" section
there doubles as the file-by-file map, no need to repeat it here.

It's a one-shot script, not a daemon - intended to be invoked once per run
(e.g. from cron) via `python -m starling_rules_engine`, with all
"since when did I last run" state persisted to `state.json` between
invocations.

## Commands

Install deps: `pip install -r requirements.txt`

Run tests: `python -m unittest discover -s tests -v`

Run a single test module: `python -m unittest tests.test_engine -v`

Run a single test case/method: `python -m unittest tests.test_engine.EngineTest.test_name -v`

Run the CLI locally (requires `config.yaml` + `.env`, see README.md setup):
`python -m starling_rules_engine [-v] [--config config.yaml] [--env-file .env]`

Run the read-only reconciliation report (same config, no payments API call,
no signing key needed): `python -m starling_rules_engine.reconcile [--days 30]`

CI (`.github/workflows/tests.yml`) just runs `pip install -r requirements.txt`
then the test command above on Python 3.11, on every push/PR.

There is no lint/format/build step configured in this repo.

## Architecture

One poll cycle (`engine.run_once`), invoked by `__main__.py`, flows through
these modules in order:

1. **`starling_client.py`** - thin `requests`-based wrapper around the
   Starling Personal API. Resolves the account, lists feed items in the
   window since the last run, looks up the configured payee, and (when
   not dry-run) fires the actual payment. This is the only module that
   talks to the network.
2. **`signing.py`** - Starling requires payment-creation requests
   specifically (not the read endpoints) to carry a detached RSA
   signature in addition to the bearer token; this builds those headers.
   `starling_client.make_local_payment` raises if no signing key is
   configured, so dry-run/read-only use never needs this set up.
4. **`matcher.py`** - pure function `is_employer_payment`: is a given
   `FeedItem` a settled, inbound payment from one of `employer_names`?
   No side effects, no state.
5. **`reconciler.py`** - pure matching logic (`match_payments`) that pairs
   inbound reimbursements with outbound card payments by amount + closest
   date within a window, each outbound item usable once. Shared by two
   callers: engine.py's duplicate-payment guard (only matches *manual*
   card payments - `AUTO_PAYMENT_REFERENCE_PREFIX` is how it tells those
   apart from ones this tool made itself) and `reconcile.py`'s report
   (matches *all* card payments, auto or manual, for a full-period view).
6. **`safety.py`** - pure function `check()`: given the matched amount and
   what's already been transferred today, either passes or raises
   `SafetyRejection`. This is the *only* place a match can be vetoed, and
   it's deliberately isolated from engine.py so the caps are trivial to
   unit test and audit in one place. A rejection always means "skip this
   item and alert" - the engine never transfers a partial amount to duck
   under a cap.
7. **`state.py`** - local JSON ledger (`state.json` by default) of
   processed feed items + last poll timestamp + claimed outbound payments
   (`claimed_outbound`: outbound feed_item_uid -> which inbound item it
   was matched to, so reconciler.py never matches the same manual payment
   against two different reimbursements across runs). This is what makes
   reruns idempotent (each feed item is processed at most once) and lets
   `safety.py`'s daily cap look at same-day transfers across runs. Writes
   are atomic (write to a tempfile in the same dir, then `os.replace`).
8. **`notifier.py`** - every outcome (`transferred`, `skipped_safety_cap`,
   `skipped_already_paid`, `dry_run_would_transfer`, `transfer_failed`,
   `run_failed`) is logged and, if `STARLING_RULES_ALERT_WEBHOOK_URL` is
   set, posted to that webhook. Nothing the engine does happens silently.
9. **`engine.py`** - `run_once()` ties the above together: resolve
   account/payee -> fetch feed items since last poll -> for each new,
   matching item, check it's not already been paid manually
   (reconciler.py) -> check safety caps -> dry-run log or real payment ->
   record outcome in state. `_handle_match()` is the per-item state
   machine and is the place to look when tracing what happens to one
   feed item.
10. **`reconcile.py`** - separate, read-only entry point
    (`python -m starling_rules_engine.reconcile`) that runs reconciler.py's
    matching over a full period instead of incrementally, for a monthly
    sanity-check report. Doesn't touch state.json or call the payments
    API. Reuses `engine.resolve_account`/`resolve_credit_card_payee`
    rather than re-implementing account/payee resolution.
11. **`config.py`** - loads `config.yaml` (via PyYAML) plus secrets from
   the environment (via `python-dotenv` loading `.env`). Secrets
   (`STARLING_PERSONAL_ACCESS_TOKEN`, `STARLING_RULES_ALERT_WEBHOOK_URL`,
   `STARLING_SIGNING_KEY_PASSPHRASE`) are **only** ever read from the
   environment, never from the YAML file, so `config.yaml` is safe to keep
   around unencrypted (it's still gitignored anyway, being personal).
   `signing_key_uid` / `signing_private_key_path` (see `signing.py`) live
   in `config.yaml` rather than `.env` since, like `state_path`, they're a
   uid and a file path rather than a secret value itself.
   `reconciliation.match_window_days` (default 14, 0 disables) controls
   both engine.py's duplicate-payment guard and reconcile.py's matching
   tolerance.
   `config.example.yaml` / `.env.example` are the templates users copy
   from.

Amounts are minor units (pence) as plain ints throughout, matching
Starling's `amount.minorUnits` field - see "Working in this repo" below.

## Working in this repo

- This tool moves real money. Any change to `starling_client.py`,
  `engine.py`, or `safety.py` should be treated as high-stakes: prefer
  failing loudly (raising) over guessing/defaulting when data looks
  wrong, and never make the safety caps in `safety.py` easier to bypass
  without the user explicitly asking for it.
- `starling_client.py`'s request/response shapes and `signing.py`'s
  signature scheme are both checked against Starling's API docs/samples -
  see their module docstrings for sources. If you touch either file,
  re-verify against those sources rather than assuming the shapes here
  are still accurate - APIs drift.
- Tests mock the Starling client entirely (`unittest.mock`) - never make
  a test hit the real API, sandbox or otherwise.
- `dry_run` must stay the default (`true`) for a fresh `config.example.yaml`
  - don't flip that default.
- Amounts are always minor units (pence/cents) as plain ints, matching
  Starling's own `amount.minorUnits` field - don't introduce float
  currency math anywhere.
