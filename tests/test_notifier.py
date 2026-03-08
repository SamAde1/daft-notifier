"""Test notification delivery.

Usage:
    python -m tests.test_notifier                              # send both alert + error to dev
    python -m tests.test_notifier --type alert                 # alert only to dev
    python -m tests.test_notifier --type error                 # error only to dev
    python -m tests.test_notifier --environment prod           # both to prod
    python -m tests.test_notifier --type alert --environment prod
"""

from __future__ import annotations

import argparse
import logging
import uuid
from datetime import datetime, timezone

from daft_monitor.config import load_config
from daft_monitor.logging_setup import LoggingRuntimeConfig, setup_logging
from daft_monitor.models import Listing
from daft_monitor.notifiers import build_alert_notifiers, build_error_notifiers
from daft_monitor.wide_event import WideEvent


def _create_test_listing() -> Listing:
    """Build a realistic-looking test listing that is clearly marked as a test."""
    return Listing(
        id="TEST-0000001",
        title="[TEST] 2 Bed Apartment, Grand Canal Dock, Dublin 2",
        price="EUR1,500 per month",
        url="https://www.daft.ie/share/test-listing-do-not-click",
        location="Grand Canal Dock, Dublin 2",
        bedrooms="Double Room",
        image_url=None,
        search_name="test-search",
        first_seen=Listing.now_iso(),
    )


def _create_test_errors() -> tuple[str, list[dict]]:
    """Build realistic-looking test errors that are clearly marked as tests."""
    cycle_id = "test-cycle-" + str(uuid.uuid4())[:8]
    errors = [
        {
            "message": "search_failed",
            "context": {
                "search_name": "Dublin Sharing Test",
                "error": "[TEST] ConnectionError: Failed to reach gateway.daft.ie — simulated network timeout",
            },
        },
        {
            "message": "notification_failed",
            "context": {
                "provider": "ntfy",
                "notifier": "ntfy-test",
                "listing_id": "TEST-0000001",
                "error": "[TEST] HTTPError: 503 Service Unavailable — simulated server error",
            },
        },
    ]
    return cycle_id, errors


def _send_test_alert(config, environment: str, event: WideEvent, logger: logging.Logger) -> None:
    notifiers = build_alert_notifiers(config, environment)
    if not notifiers:
        logger.warning("No alert notifiers configured for environment=%s", environment)
        return

    listing = _create_test_listing()
    logger.info("Sending TEST alert to %s notifier(s) in environment=%s ...", len(notifiers), environment)
    for n in notifiers:
        ok = n.send(listing, event)
        logger.info("  Alert via %s -> sent=%s", type(n).__name__, ok)


def _send_test_error(config, environment: str, event: WideEvent, logger: logging.Logger) -> None:
    notifiers = build_error_notifiers(config, environment)
    if not notifiers:
        logger.warning("No error notifiers configured for environment=%s", environment)
        return

    cycle_id, errors = _create_test_errors()
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    title = f"[TEST] Daft Monitor Error — {environment.upper()}"
    body_lines = [
        f"Environment: {environment}",
        f"Cycle: {cycle_id}",
        f"Time: {timestamp}",
        f"Error count: {len(errors)}",
        "",
        "NOTE: This is a TEST error notification.",
        "",
    ]
    for i, err in enumerate(errors, 1):
        body_lines.append(f"--- Error {i} ---")
        body_lines.append(f"Type: {err.get('message', 'Unknown')}")
        ctx = err.get("context", {})
        if isinstance(ctx, dict):
            for k, v in ctx.items():
                body_lines.append(f"  {k}: {v}")
        body_lines.append("")

    body = "\n".join(body_lines)

    logger.info("Sending TEST error to %s notifier(s) in environment=%s ...", len(notifiers), environment)
    for n in notifiers:
        ok = n.send_error(title, body, event)
        logger.info("  Error via %s -> sent=%s", type(n).__name__, ok)


def run_test(test_type: str, environment: str, config_path: str = "config.yaml") -> None:
    setup_logging(LoggingRuntimeConfig(environment=environment, log_level="info", write_logs=False))
    logger = logging.getLogger("daft_monitor.test")

    config = load_config(config_path)
    event = WideEvent(
        cycle_id=f"test-{test_type}",
        is_seed_run=False,
        check_interval_minutes=0,
        environment=environment,
    )

    if test_type in ("alert", "both"):
        _send_test_alert(config, environment, event, logger)

    if test_type in ("error", "both"):
        _send_test_error(config, environment, event, logger)

    event.emit(logger)
    logger.info("Test complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test notification delivery.")
    parser.add_argument(
        "--type",
        choices=["alert", "error"],
        default=None,
        help="Test type: alert, error. Omit to send both.",
    )
    parser.add_argument(
        "--environment",
        choices=["dev", "prod"],
        default="dev",
        help="Target environment (default: dev). Must explicitly pass 'prod' for prod.",
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config file.")
    args = parser.parse_args()

    run_test(args.type or "both", args.environment, args.config)
