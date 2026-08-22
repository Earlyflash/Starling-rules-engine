# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A small Python CLI (`starling_rules_engine/`) that polls a Starling Bank
personal account for employer expense reimbursements and, when caps
allow, automatically pays the matching amount to a credit card saved as a
Starling payee. See README.md for setup/usage; the "Architecture" section
there doubles as the file-by-file map, no need to repeat it here.

## Working in this repo

- This tool moves real money. Any change to `starling_client.py`,
  `engine.py`, or `safety.py` should be treated as high-stakes: prefer
  failing loudly (raising) over guessing/defaulting when data looks
  wrong, and never make the safety caps in `safety.py` easier to bypass
  without the user explicitly asking for it.
- `starling_client.py`'s request/response shapes were written without
  access to live Starling API docs (network egress to
  developer.starlingbank.com was blocked when this repo was scaffolded).
  If you touch it, look for a way to verify against current docs or a
  sandbox account rather than assuming the existing shape is correct.
- Tests mock the Starling client entirely (`unittest.mock`) - never make
  a test hit the real API, sandbox or otherwise.
- `dry_run` must stay the default (`true`) for a fresh `config.example.yaml`
  - don't flip that default.
- Amounts are always minor units (pence/cents) as plain ints, matching
  Starling's own `amount.minorUnits` field - don't introduce float
  currency math anywhere.

Run tests:
```
python -m unittest discover -s tests -v
```
