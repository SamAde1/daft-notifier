"""Keep the uninstalled version fallback aligned with pyproject.toml."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class VersionTests(unittest.TestCase):
    def test_fallback_version_matches_pyproject(self) -> None:
        pyproject_text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        init_text = (ROOT / "daft_monitor" / "__init__.py").read_text(encoding="utf-8")
        pinned = re.search(r'^version = "([^"]+)"', pyproject_text, re.MULTILINE)
        fallback = re.search(r'__version__ = "([^"]+)"', init_text)
        self.assertIsNotNone(pinned)
        self.assertIsNotNone(fallback)
        assert pinned is not None
        assert fallback is not None
        self.assertEqual(pinned.group(1), fallback.group(1))
