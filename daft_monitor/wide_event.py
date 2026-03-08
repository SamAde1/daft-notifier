from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any


class WideEvent:
    """Build a canonical log line through the lifecycle of one check cycle."""

    def __init__(self, cycle_id: str, is_seed_run: bool, check_interval_minutes: int, environment: str = "dev"):
        self._start = time.perf_counter()
        self._status = "ok"
        self._payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_name": "listing_check_cycle",
            "environment": environment,
            "cycle_id": cycle_id,
            "is_seed_run": is_seed_run,
            "check_interval_minutes": check_interval_minutes,
            "searches_executed": [],
            "service_hops": [],
            "total_listings_fetched": 0,
            "new_listings_count": 0,
            "notifications_sent": 0,
            "notification_errors": 0,
            "errors": [],
        }

    @property
    def payload(self) -> dict[str, Any]:
        return self._payload

    def add_field(self, key: str, value: Any) -> None:
        self._payload[key] = value

    def add_hop(self, service: str, context: dict[str, Any]) -> None:
        self._payload["service_hops"].append({"service": service, **context})

    def add_search(self, name: str) -> None:
        self._payload["searches_executed"].append(name)

    def add_error(self, message: str, context: dict[str, Any] | None = None) -> None:
        self._status = "error"
        entry: dict[str, Any] = {"message": message}
        if context:
            entry["context"] = context
        self._payload["errors"].append(entry)

    def increment(self, key: str, amount: int = 1) -> None:
        current = int(self._payload.get(key, 0))
        self._payload[key] = current + amount

    def finalize(self) -> dict[str, Any]:
        self._payload["duration_ms"] = int((time.perf_counter() - self._start) * 1000)
        self._payload["status"] = self._status
        return self._payload

    def emit(self, logger: logging.Logger) -> None:
        payload = self.finalize()

        # --- INFO: concise one-liner summary ---
        summary = (
            f"cycle_id={payload['cycle_id']} status={payload['status']} "
            f"seed={payload['is_seed_run']} fetched={payload['total_listings_fetched']} "
            f"new={payload['new_listings_count']} sent={payload['notifications_sent']} "
            f"notify_err={payload['notification_errors']} errors={len(payload['errors'])} "
            f"duration_ms={payload['duration_ms']}"
        )
        if payload["status"] == "error":
            logger.error("CHECK_CYCLE %s", summary)
        else:
            logger.info("CHECK_CYCLE %s", summary)

        # --- DEBUG: full wide-event JSON dump ---
        if not logger.isEnabledFor(logging.DEBUG):
            return

        style = os.environ.get("DAFT_MONITOR_LOG_STYLE", "pretty").strip().lower()
        if style == "json":
            logger.debug("CHECK_EVENT %s", json.dumps(payload, separators=(",", ":"), ensure_ascii=True))
            return

        pretty = json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True)
        logger.debug("CHECK_EVENT %s", pretty)
