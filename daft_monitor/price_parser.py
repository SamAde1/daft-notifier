"""Parse free-text Daft price strings into numeric values.

Daft returns prices as human-readable text such as:
  - "€1,200 per month"
  - "€250 per week"
  - "From €700 to €1,250 per month"
  - "€395,000" / "From €310,000"  (sale)
  - "Price on Application"

Analytics and digests need numbers, so this module extracts:
  price_value  — the euro amount (float), or None if unparseable
  price_period — "month", "week", or "sale", or None
  price_monthly_eq — monthly-equivalent rent (week * 52/12), or None for sales
"""

from __future__ import annotations

import re
from typing import Literal

PricePeriod = Literal["month", "week", "sale"]

_CURRENCY = r"(?:€|eur)\s*"
_AMOUNT = r"([\d,]+(?:\.\d+)?)"


def parse_price(
    price_str: str | None,
    bedrooms: str | None = None,
) -> tuple[float | None, PricePeriod | None]:
    """Parse a Daft price string into (value, period).

    For "From X to Y" rental ranges, uses a bedrooms heuristic when available:
      - "Single & Double" (no twin) → higher value
      - "Double & Twin" → lower value
      - otherwise → midpoint
    For sales "From X" (no upper bound), uses X.
    """
    if not price_str or not str(price_str).strip():
        return None, None

    text = str(price_str).strip()
    lowered = text.lower()

    if "price on application" in lowered or lowered in {"poa", "n/a", "na"}:
        return None, None

    # "From €X to €Y per month|week"
    from_to = re.match(
        rf"from\s+{_CURRENCY}?{_AMOUNT}\s+to\s+{_CURRENCY}?{_AMOUNT}\s+per\s+(month|week)\b",
        text,
        re.I,
    )
    if from_to:
        low = _to_float(from_to.group(1))
        high = _to_float(from_to.group(2))
        period: PricePeriod = "month" if from_to.group(3).lower() == "month" else "week"
        return _choose_range(low, high, bedrooms), period

    # "€X per month" / "€X per week"
    per = re.search(rf"{_CURRENCY}?{_AMOUNT}\s+per\s+(month|week)\b", text, re.I)
    if per:
        value = _to_float(per.group(1))
        period = "month" if per.group(2).lower() == "month" else "week"
        return value, period

    # Sales: "From €X" (asking-price floor)
    from_sale = re.match(rf"from\s+{_CURRENCY}?{_AMOUNT}\s*$", text, re.I)
    if from_sale:
        return _to_float(from_sale.group(1)), "sale"

    # Sales: "AMV: €X" (Advised Minimum Value — common on Irish auction listings)
    amv = re.match(rf"amv\s*:?\s*{_CURRENCY}?{_AMOUNT}\s*$", text, re.I)
    if amv:
        return _to_float(amv.group(1)), "sale"

    # Sales: plain "€X" / "EUR X"
    plain = re.match(rf"^{_CURRENCY}?{_AMOUNT}\s*$", text, re.I)
    if plain:
        return _to_float(plain.group(1)), "sale"

    return None, None


def monthly_equivalent(price_value: float | None, price_period: PricePeriod | None) -> float | None:
    """Convert a parsed rent to a monthly figure. Sales return None."""
    if price_value is None or price_period is None:
        return None
    if price_period == "month":
        return price_value
    if price_period == "week":
        return price_value * 52.0 / 12.0
    return None  # sale


def parse_price_fields(
    price_str: str | None,
    bedrooms: str | None = None,
) -> tuple[float | None, PricePeriod | None, float | None]:
    """Convenience: return (price_value, price_period, price_monthly_eq)."""
    value, period = parse_price(price_str, bedrooms)
    return value, period, monthly_equivalent(value, period)


def _to_float(raw: str) -> float:
    return float(raw.replace(",", ""))


def _choose_range(low: float, high: float, bedrooms: str | None) -> float:
    beds = (bedrooms or "").strip().lower()
    if "single" in beds and "double" in beds and "twin" not in beds:
        return high
    if "double" in beds and "twin" in beds:
        return low
    return (low + high) / 2.0
