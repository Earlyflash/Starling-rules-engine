"""CLI entrypoint: `python -m starling_rules_engine [--config config.yaml]`.

Designed to be run on a schedule (cron / systemd timer), not as a
long-running daemon: each invocation polls once, since the last recorded
poll time (see state.py), and exits. See README.md for a suggested cron
line.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from .config import ConfigError, load_config
from .engine import run_once
from .notifier import Notifier
from .starling_client import StarlingClient
from .state import State

log = logging.getLogger(__name__)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Poll Starling for employer expense payments and reconcile a matching transfer to a credit card payee."
    )
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
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

    if config.dry_run:
        log.warning("dry_run is enabled: no real payments will be made")

    client = StarlingClient(config.starling_token, sandbox=config.sandbox)
    state = State(config.state_path)
    notifier = Notifier(config.alert_webhook_url)

    try:
        run_once(config, client, state, notifier)
    except Exception as exc:  # noqa: BLE001 - last-resort catch so a run failure is always alerted
        log.exception("run failed")
        notifier.notify("run_failed", error=str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
