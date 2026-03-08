from __future__ import annotations

from abc import ABC, abstractmethod

from daft_monitor.models import Listing
from daft_monitor.wide_event import WideEvent


class Notifier(ABC):
    @abstractmethod
    def send(self, listing: Listing, event: WideEvent) -> bool:
        """Send a new-listing alert notification. Returns True on success."""
        raise NotImplementedError

    @abstractmethod
    def send_error(self, error_title: str, error_body: str, event: WideEvent) -> bool:
        """Send an error notification. Returns True on success."""
        raise NotImplementedError
