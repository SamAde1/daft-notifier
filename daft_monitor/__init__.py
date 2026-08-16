"""Daft listing monitor package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("daft-notifier")
except PackageNotFoundError:
    # Uninstalled checkout. Must match [project].version in pyproject.toml.
    __version__ = "1.2.0"
