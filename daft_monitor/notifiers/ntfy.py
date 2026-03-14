from __future__ import annotations

import requests

from daft_monitor.config import NotifierConfig
from daft_monitor.models import Listing
from daft_monitor.notifiers.base import Notifier
from daft_monitor.wide_event import WideEvent


class NtfyNotifier(Notifier):
    def __init__(self, cfg: NotifierConfig, timeout_seconds: int = 20, location_name: str | None = None):
        if not cfg.topic:
            raise ValueError("Ntfy topic is required.")
        self.cfg = cfg
        self.timeout_seconds = timeout_seconds
        self.location_name = location_name

    @staticmethod
    def _ascii_safe(text: str) -> str:
        """HTTP headers only support latin-1 / ASCII.

        Replace common non-ASCII chars (e.g. euro sign) with safe
        equivalents so ntfy headers don't blow up.
        """
        replacements = {
            "\u20ac": "EUR",  # €
            "\u00a3": "GBP",  # £
            "\u00a5": "JPY",  # ¥
        }
        for char, replacement in replacements.items():
            text = text.replace(char, replacement)
        # Drop anything else that can't be latin-1 encoded.
        return text.encode("latin-1", errors="replace").decode("latin-1")

    def _base_headers(self) -> dict[str, str]:
        """Build the common headers shared by alert and error sends."""
        headers: dict[str, str] = {}
        if self.cfg.priority:
            headers["Priority"] = self.cfg.priority
        if self.cfg.tags:
            headers["Tags"] = ",".join(self.cfg.tags)
        if self.cfg.token:
            headers["Authorization"] = f"Bearer {self.cfg.token}"
        return headers

    # --- Alert notification (new listing) ---

    def send(self, listing: Listing, event: WideEvent) -> bool:
        url = f"{self.cfg.server}/{self.cfg.topic}"

        beds = listing.bedrooms or "N/A"
        price = listing.price or "N/A"
        location = listing.location or "Unknown"

        title = self._ascii_safe(f"{listing.title} - {price}")
        body_lines = [
            f"Price: {price}",
            f"Location: {location}",
            f"Beds: {beds}",
            f"Link: {listing.url}",
        ]
        if listing.distance_to_location is not None and self.location_name:
            body_lines.insert(3, f"Distance to {self.location_name}: {listing.distance_to_location:.2f} km")
        body = "\n".join(body_lines)

        headers = self._base_headers()
        headers["Title"] = title[:200]
        headers["Click"] = listing.url

        try:
            response = requests.post(
                url,
                data=body.encode("utf-8"),
                headers=headers,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            event.add_hop(
                "notification_ntfy",
                {
                    "notifier": self.cfg.name,
                    "listing_id": listing.id,
                    "status": "sent",
                    "http_status": response.status_code,
                },
            )
            return True
        except Exception as exc:
            event.add_hop(
                "notification_ntfy",
                {
                    "notifier": self.cfg.name,
                    "listing_id": listing.id,
                    "status": "error",
                    "error": str(exc),
                },
            )
            event.add_error(
                "notification_failed",
                {
                    "provider": "ntfy",
                    "notifier": self.cfg.name,
                    "listing_id": listing.id,
                    "error": str(exc),
                },
            )
            return False

    # --- Error notification ---

    def send_error(self, error_title: str, error_body: str, event: WideEvent) -> bool:
        url = f"{self.cfg.server}/{self.cfg.topic}"
        title = self._ascii_safe(error_title)[:200]

        headers = self._base_headers()
        headers["Title"] = title

        try:
            response = requests.post(
                url,
                data=error_body.encode("utf-8"),
                headers=headers,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            event.add_hop(
                "notification_ntfy_error",
                {
                    "notifier": self.cfg.name,
                    "status": "sent",
                    "http_status": response.status_code,
                },
            )
            return True
        except Exception as exc:
            event.add_hop(
                "notification_ntfy_error",
                {
                    "notifier": self.cfg.name,
                    "status": "error",
                    "error": str(exc),
                },
            )
            return False
