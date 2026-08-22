"""Every action the engine takes gets logged and, optionally, pushed to a webhook.

This is the "alert on every action" half of the safety net: nothing the
engine does - transfer, skip, or error - happens silently.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, webhook_url: Optional[str] = None, session: Optional[requests.Session] = None):
        self._webhook_url = webhook_url
        self._session = session or requests.Session()

    def notify(self, event: str, **fields) -> None:
        payload = {"event": event, **fields}
        log.info(json.dumps(payload, sort_keys=True, default=str))
        if self._webhook_url:
            try:
                self._session.post(
                    self._webhook_url, json={"text": self._format_text(event, fields)}, timeout=10
                )
            except requests.RequestException as exc:
                log.warning("failed to deliver webhook notification: %s", exc)

    @staticmethod
    def _format_text(event: str, fields: dict) -> str:
        parts = [f"[starling-rules-engine] {event}"]
        parts.extend(f"{key}={value}" for key, value in fields.items())
        return " ".join(parts)
