from __future__ import annotations

from daft_monitor.config import AppConfig
from daft_monitor.notifiers.base import Notifier
from daft_monitor.notifiers.ntfy import NtfyNotifier


def _build_by_role(config: AppConfig, environment: str, role: str) -> list[Notifier]:
    """Build notifiers matching a given role and environment."""
    notifiers: list[Notifier] = []
    for nc in config.notifiers:
        if not nc.enabled:
            continue
        if nc.role != role:
            continue
        if environment not in nc.environments:
            continue
        if nc.type == "ntfy":
            notifiers.append(NtfyNotifier(nc))
    return notifiers


def build_alert_notifiers(config: AppConfig, environment: str) -> list[Notifier]:
    """Return notifiers configured for new-listing alerts in the given environment."""
    return _build_by_role(config, environment, "alerts")


def build_error_notifiers(config: AppConfig, environment: str) -> list[Notifier]:
    """Return notifiers configured for error alerts in the given environment."""
    return _build_by_role(config, environment, "errors")
