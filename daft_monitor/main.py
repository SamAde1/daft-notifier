from __future__ import annotations

import argparse
import logging
import os
import signal
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import FrameType
from typing import Any

from daft_monitor.config import AppConfig, load_config
from daft_monitor.health import HealthServer
from daft_monitor.logging_setup import (
    LoggingRuntimeConfig,
    parse_bool,
    parse_environment,
    parse_log_level,
    setup_logging,
)
from daft_monitor.models import Listing
from daft_monitor.notifiers import build_alert_notifiers, build_error_notifiers
from daft_monitor.notifiers.base import Notifier
from daft_monitor.searcher import Searcher
from daft_monitor.storage import Storage
from daft_monitor.wide_event import WideEvent


LOGGER = logging.getLogger("daft_monitor")
_STOP_REQUESTED = False


def _handle_shutdown_signal(signum: int, _: FrameType | None) -> None:
    global _STOP_REQUESTED
    _STOP_REQUESTED = True
    LOGGER.info("shutdown signal=%d status=stopping", signum)


def _register_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)


def _dispatch_error_notifications(
    error_notifiers: list[Notifier],
    environment: str,
    cycle_id: str,
    errors: list[dict[str, Any]],
    event: WideEvent,
) -> None:
    """Send a consolidated error notification for all errors accumulated in a cycle."""
    if not error_notifiers:
        return

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    title = f"Daft Monitor Error — {environment.upper()}"

    body_lines = [
        f"Environment: {environment}",
        f"Cycle: {cycle_id}",
        f"Time: {timestamp}",
        f"Error count: {len(errors)}",
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

    for notifier in error_notifiers:
        try:
            notifier.send_error(title, body, event)
        except Exception:
            LOGGER.error("Failed to send error notification via %s", type(notifier).__name__, exc_info=True)


def _run_cycle(config: AppConfig, storage: Storage, searcher: Searcher, environment: str) -> None:
    cycle_id = str(uuid.uuid4())
    is_seed_run = storage.is_first_run()
    event = WideEvent(
        cycle_id=cycle_id,
        is_seed_run=is_seed_run,
        check_interval_minutes=config.check_interval_minutes,
        environment=environment,
    )

    try:
        listings = searcher.run_all(config.searches, event)
        unique_by_id = {listing.id: listing for listing in listings}
        deduped = list(unique_by_id.values())
        event.add_field("deduped_listings_count", len(deduped))
        event.add_hop("dedupe", {"input_count": len(listings), "output_count": len(deduped)})

        if is_seed_run:
            inserted = storage.insert_listings(deduped)
            event.add_field("seed_inserted_count", inserted)
            event.add_hop("storage_seed", {"inserted_count": inserted})
            return

        new_listings = storage.filter_new_listings(deduped)
        event.add_field("new_listings_count", len(new_listings))
        event.add_hop("storage_diff", {"candidate_count": len(deduped), "new_count": len(new_listings)})

        alert_notifiers = build_alert_notifiers(config, environment)
        for listing in new_listings:
            for notifier in alert_notifiers:
                ok = notifier.send(listing, event)
                if ok:
                    event.increment("notifications_sent", 1)
                else:
                    event.increment("notification_errors", 1)

        inserted = storage.insert_listings(new_listings)
        event.add_hop("storage_insert", {"inserted_count": inserted})
    except Exception as exc:
        event.add_error("cycle_failed", {"error": str(exc)})
    finally:
        event.emit(LOGGER)
        # If any errors accumulated during this cycle, send error notifications.
        errors = event.payload.get("errors", [])
        if errors:
            error_notifiers = build_error_notifiers(config, environment)
            _dispatch_error_notifications(error_notifiers, environment, cycle_id, errors, event)


def run(config_path: str | None = None, run_once: bool = False) -> None:
    run_with_logging(
        config_path=config_path,
        run_once=run_once,
        environment=os.environ.get("DAFT_MONITOR_ENVIRONMENT", "dev"),
        log_level=os.environ.get("DAFT_MONITOR_LOG_LEVEL", "info"),
        write_logs=parse_bool(os.environ.get("DAFT_MONITOR_WRITE_LOGS", "true")),
        log_dir=os.environ.get("DAFT_MONITOR_LOG_DIR", "./logs"),
    )


def _send_startup_tests(config: AppConfig, environment: str) -> None:
    """Send a test alert and test error on startup so we know notifications work."""
    event = WideEvent(
        cycle_id="startup-test",
        is_seed_run=False,
        check_interval_minutes=config.check_interval_minutes,
        environment=environment,
    )

    # --- Test alert ---
    alert_notifiers = build_alert_notifiers(config, environment)
    if alert_notifiers:
        test_listing = Listing(
            id="STARTUP-TEST",
            title=f"[STARTUP TEST] Daft Monitor — {environment.upper()}",
            price="N/A",
            url="https://www.daft.ie",
            location="Startup test notification",
            bedrooms="N/A",
            image_url=None,
            search_name="startup-test",
            first_seen=Listing.now_iso(),
        )
        for n in alert_notifiers:
            ok = n.send(test_listing, event)
            LOGGER.info("startup test alert via %s -> sent=%s", type(n).__name__, ok)
    else:
        LOGGER.warning("no alert notifiers for environment=%s — skipping startup test alert", environment)

    # --- Test error ---
    error_notifiers = build_error_notifiers(config, environment)
    if error_notifiers:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        title = f"[STARTUP TEST] Daft Monitor Error — {environment.upper()}"
        body = (
            f"Environment: {environment}\n"
            f"Time: {timestamp}\n"
            f"This is a startup test error notification.\n"
            f"If you see this, error notifications are working correctly."
        )
        for n in error_notifiers:
            ok = n.send_error(title, body, event)
            LOGGER.info("startup test error via %s -> sent=%s", type(n).__name__, ok)
    else:
        LOGGER.warning("no error notifiers for environment=%s — skipping startup test error", environment)


def run_with_logging(
    *,
    config_path: str | None = None,
    run_once: bool = False,
    environment: str = "dev",
    log_level: str = "info",
    write_logs: bool = True,
    log_dir: str = "./logs",
) -> None:
    runtime_logging = LoggingRuntimeConfig(
        environment=parse_environment(environment),
        log_level=parse_log_level(log_level),
        write_logs=write_logs,
        log_dir=log_dir,
    )
    setup_logging(runtime_logging)
    _register_signal_handlers()

    LOGGER.info(
        "startup environment=%s log_level=%s write_logs=%s log_dir=%s",
        runtime_logging.environment,
        runtime_logging.log_level,
        runtime_logging.write_logs,
        runtime_logging.log_dir,
    )

    health_server = HealthServer()
    health_server.start()

    config = load_config(config_path)

    # Send startup test notifications so we know the notifiers are healthy.
    _send_startup_tests(config, runtime_logging.environment)

    storage = Storage(config.data_dir)
    searcher = Searcher()
    sleep_seconds = config.check_interval_minutes * 60

    try:
        while not _STOP_REQUESTED:
            _run_cycle(config, storage, searcher, runtime_logging.environment)
            if run_once:
                break
            if _STOP_REQUESTED:
                break
            _interruptible_sleep(sleep_seconds, config.check_interval_minutes)
    finally:
        LOGGER.info("shutdown complete")
        health_server.stop()
        storage.close()


def _interruptible_sleep(total_seconds: int, interval_minutes: int) -> None:
    """Sleep in small chunks so signals are handled promptly.

    Logs a 'waiting' message at INFO level with the next run time,
    and a heartbeat at DEBUG level every 60 seconds.
    """
    next_run_local = datetime.now() + timedelta(seconds=total_seconds)
    LOGGER.info(
        "cycle complete — waiting %d min until next run at %s",
        interval_minutes,
        next_run_local.strftime("%H:%M:%S"),
    )
    elapsed = 0
    chunk = 10  # check every 10 seconds for shutdown signal
    while elapsed < total_seconds:
        if _STOP_REQUESTED:
            return
        time.sleep(min(chunk, total_seconds - elapsed))
        elapsed += chunk
        if elapsed % 60 == 0 and elapsed < total_seconds:
            remaining = total_seconds - elapsed
            LOGGER.debug(
                "heartbeat — waiting, %d:%02d remaining",
                remaining // 60,
                remaining % 60,
            )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor Daft listings and send notifications.")
    parser.add_argument(
        "--config", dest="config_path",
        default=os.environ.get("DAFT_MONITOR_CONFIG"),
        help="Path to config.yaml",
    )
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit.")
    parser.add_argument(
        "--environment",
        default=os.environ.get("DAFT_MONITOR_ENVIRONMENT", "dev"),
        choices=["dev", "prod"],
        help="Runtime environment label (env: DAFT_MONITOR_ENVIRONMENT).",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("DAFT_MONITOR_LOG_LEVEL", "info"),
        choices=["debug", "info", "error"],
        help="Log verbosity (env: DAFT_MONITOR_LOG_LEVEL).",
    )
    parser.add_argument(
        "--write-logs",
        default=os.environ.get("DAFT_MONITOR_WRITE_LOGS", "true"),
        help="Write logs to files (true/false) (env: DAFT_MONITOR_WRITE_LOGS).",
    )
    parser.add_argument(
        "--log-dir",
        default=os.environ.get("DAFT_MONITOR_LOG_DIR", "./logs"),
        help="Directory for log files (env: DAFT_MONITOR_LOG_DIR).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run_with_logging(
        config_path=args.config_path,
        run_once=args.once,
        environment=args.environment,
        log_level=args.log_level,
        write_logs=parse_bool(args.write_logs),
        log_dir=args.log_dir,
    )
