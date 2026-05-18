from __future__ import annotations

ENV_PREFIX = "DAFT_MONITOR_"

OSRM_TABLE_URL = "http://router.project-osrm.org/table/v1/driving/"

DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) Gecko/20100101 Firefox/146.0"

EVENT_NEW = "new"
EVENT_PRICE_CHANGE = "price_change"
EVENT_REMOVED = "removed"
EVENT_RELISTED = "relisted"

IRISH_COUNTIES = (
    "Antrim",
    "Armagh",
    "Carlow",
    "Cavan",
    "Clare",
    "Cork",
    "Derry",
    "Donegal",
    "Down",
    "Dublin",
    "Fermanagh",
    "Galway",
    "Kerry",
    "Kildare",
    "Kilkenny",
    "Laois",
    "Leitrim",
    "Limerick",
    "Longford",
    "Louth",
    "Mayo",
    "Meath",
    "Monaghan",
    "Offaly",
    "Roscommon",
    "Sligo",
    "Tipperary",
    "Tyrone",
    "Waterford",
    "Westmeath",
    "Wexford",
    "Wicklow",
)

COUNTY_MATCH_ORDER = tuple(sorted(IRISH_COUNTIES, key=len, reverse=True))
