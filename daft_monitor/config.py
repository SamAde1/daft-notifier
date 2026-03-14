from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml


ENV_PREFIX = "DAFT_MONITOR_"


@dataclass(slots=True)
class SearchConfig:
    name: str
    search_type: str
    location: str | list[str]
    distance: str | None = None
    sort_type: str | None = None
    suitable_for: list[str] | None = None
    facilities: list[str] | None = None
    misc_filters: list[str] | None = None
    added_since: str | None = None
    min_ber: str | None = None
    max_ber: str | None = None
    min_price: int | None = None
    max_price: int | None = None
    min_beds: int | None = None
    max_beds: int | None = None
    min_baths: int | None = None
    max_baths: int | None = None
    owner_occupied: bool | None = None
    min_tenants: int | None = None
    max_tenants: int | None = None
    min_lease: int | None = None
    max_lease: int | None = None
    min_floor_size: int | None = None
    max_floor_size: int | None = None
    property_type: str | None = None
    room_type: str | None = None
    custom_filters: dict[str, str | list[str]] | None = None
    max_pages: int | None = None


@dataclass(slots=True)
class NotifierConfig:
    """Configuration for a single named notification channel."""

    name: str
    type: str  # "ntfy" (extensible later)
    role: str  # "alerts" or "errors"
    environments: list[str]  # e.g. ["dev"], ["prod"], ["dev", "prod"]
    enabled: bool = False
    server: str = "https://ntfy.sh"
    topic: str = ""
    token: str | None = None
    priority: str | None = None
    tags: list[str] | None = None


@dataclass(slots=True)
class AppConfig:
    check_interval_minutes: int
    data_dir: str
    distance_to_location: bool
    location_name: str
    location_latitude: float | None
    location_longitude: float | None
    searches: list[SearchConfig]
    notifiers: list[NotifierConfig]


def _set_nested(target: dict[str, Any], path: list[str], value: Any) -> None:
    node = target
    for key in path[:-1]:
        if key not in node or not isinstance(node[key], dict):
            node[key] = {}
        node = node[key]
    node[path[-1]] = value


def _parse_scalar(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"none", "null"}:
        return None
    if value.isdigit():
        return int(value)
    return value


def _apply_env_overrides(config: dict[str, Any]) -> dict[str, Any]:
    # Friendly aliases for top-level settings.
    aliases = {
        f"{ENV_PREFIX}CHECK_INTERVAL_MINUTES": ["check_interval_minutes"],
        f"{ENV_PREFIX}DATA_DIR": ["data_dir"],
    }
    for env_key, path in aliases.items():
        if env_key in os.environ:
            _set_nested(config, path, _parse_scalar(os.environ[env_key]))

    # Generic nested override with double underscore separators.
    # Example: DAFT_MONITOR_NOTIFICATIONS__NTFY_DEV_ALERTS__TOPIC
    for key, value in os.environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        if key in aliases:
            continue
        if "__" not in key:
            continue
        path = [part.lower() for part in key[len(ENV_PREFIX):].split("__")]
        _set_nested(config, path, _parse_scalar(value))
    return config


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _to_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _to_bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    raise ValueError(f"Cannot parse boolean value: {value!r}")


def _to_str_list_or_none(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result: list[str] = []
        for index, item in enumerate(value):
            if not isinstance(item, str):
                raise ValueError(f"List entry at index {index} must be a string.")
            result.append(item)
        return result
    raise ValueError("Expected a string or list of strings.")


def _parse_custom_filters(raw: Any) -> dict[str, str | list[str]] | None:
    """Parse custom_filters from config.

    Accepts a dict where each key is a filter name and the value is either
    a string or a list of strings.  Example YAML::

        custom_filters:
          roomType: "double"
          adState: ["published"]
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("custom_filters must be a mapping of name -> value(s).")
    result: dict[str, str | list[str]] = {}
    for key, val in raw.items():
        key_str = str(key).strip()
        if isinstance(val, list):
            result[key_str] = [str(v).strip() for v in val]
        else:
            result[key_str] = str(val).strip()
    return result


def _parse_notifier(name: str, raw: dict[str, Any]) -> NotifierConfig:
    """Parse a single named notifier entry from the config."""
    ntype = str(raw.get("type", "ntfy")).strip().lower()
    role = str(raw.get("role", "alerts")).strip().lower()
    _require(role in {"alerts", "errors"}, f"notifications.{name}.role must be 'alerts' or 'errors'.")

    envs_raw = raw.get("environments", ["dev", "prod"])
    if isinstance(envs_raw, str):
        envs = [envs_raw.strip().lower()]
    elif isinstance(envs_raw, list):
        envs = [str(e).strip().lower() for e in envs_raw]
    else:
        envs = ["dev", "prod"]

    nc = NotifierConfig(
        name=name,
        type=ntype,
        role=role,
        environments=envs,
        enabled=bool(raw.get("enabled", False)),
        server=str(raw.get("server", "https://ntfy.sh")).rstrip("/"),
        topic=str(raw.get("topic", "")),
        token=(str(raw["token"]) if raw.get("token") else None),
        priority=(str(raw["priority"]) if raw.get("priority") else None),
        tags=(list(raw["tags"]) if isinstance(raw.get("tags"), list) else None),
    )

    if nc.enabled and nc.type == "ntfy":
        _require(bool(nc.topic), f"notifications.{name}.topic is required when enabled.")

    return nc


def load_config(path: str | None = None) -> AppConfig:
    config_path = path or os.environ.get(f"{ENV_PREFIX}CONFIG", "config.yaml")
    raw_path = Path(config_path)
    if not raw_path.exists():
        raise FileNotFoundError(f"Config file not found: {raw_path}")

    loaded = yaml.safe_load(raw_path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError("Config root must be a mapping/object.")

    cfg = _apply_env_overrides(loaded)
    _require("searches" in cfg and isinstance(cfg["searches"], list), "Config requires a 'searches' list.")
    _require(len(cfg["searches"]) > 0, "Config requires at least one search entry.")

    interval = int(cfg.get("check_interval_minutes", 10))
    _require(interval > 0, "check_interval_minutes must be > 0.")

    data_dir = str(cfg.get("data_dir", "./data"))
    distance_to_location = bool(cfg.get("distance_to_location", False))
    location_name = str(cfg.get("location_name", "City Centre")).strip()
    location_latitude = _to_float_or_none(cfg.get("location_latitude"))
    location_longitude = _to_float_or_none(cfg.get("location_longitude"))
    if distance_to_location:
        _require(bool(location_name), "location_name is required when distance_to_location=true.")
        _require(location_latitude is not None, "location_latitude is required when distance_to_location=true.")
        _require(location_longitude is not None, "location_longitude is required when distance_to_location=true.")

    searches: list[SearchConfig] = []
    for idx, search in enumerate(cfg["searches"]):
        if not isinstance(search, dict):
            raise ValueError(f"searches[{idx}] must be an object.")
        name = str(search.get("name", "")).strip()
        search_type = str(search.get("search_type", "")).strip()
        location_raw = search.get("location")
        _require(bool(name), f"searches[{idx}].name is required.")
        _require(bool(search_type), f"searches[{idx}].search_type is required.")
        _require(
            isinstance(location_raw, (str, list)),
            f"searches[{idx}].location must be a string or list of strings.",
        )
        if isinstance(location_raw, list):
            _require(
                all(isinstance(item, str) for item in location_raw),
                f"searches[{idx}].location list must contain only strings.",
            )
        location = cast(str | list[str], location_raw)

        searches.append(
            SearchConfig(
                name=name,
                search_type=search_type,
                location=location,
                distance=(str(search["distance"]).strip() if search.get("distance") is not None else None),
                sort_type=(str(search["sort_type"]).strip() if search.get("sort_type") is not None else None),
                suitable_for=_to_str_list_or_none(search.get("suitable_for")),
                facilities=_to_str_list_or_none(search.get("facilities") or search.get("facility")),
                misc_filters=_to_str_list_or_none(search.get("misc_filters")),
                added_since=(str(search["added_since"]).strip() if search.get("added_since") is not None else None),
                min_ber=(str(search["min_ber"]).strip() if search.get("min_ber") is not None else None),
                max_ber=(str(search["max_ber"]).strip() if search.get("max_ber") is not None else None),
                min_price=_to_int_or_none(search.get("min_price")),
                max_price=_to_int_or_none(search.get("max_price")),
                min_beds=_to_int_or_none(search.get("min_beds")),
                max_beds=_to_int_or_none(search.get("max_beds")),
                min_baths=_to_int_or_none(search.get("min_baths")),
                max_baths=_to_int_or_none(search.get("max_baths")),
                owner_occupied=_to_bool_or_none(search.get("owner_occupied")),
                min_tenants=_to_int_or_none(search.get("min_tenants")),
                max_tenants=_to_int_or_none(search.get("max_tenants")),
                min_lease=_to_int_or_none(search.get("min_lease")),
                max_lease=_to_int_or_none(search.get("max_lease")),
                min_floor_size=_to_int_or_none(search.get("min_floor_size")),
                max_floor_size=_to_int_or_none(search.get("max_floor_size")),
                property_type=(str(search["property_type"]).strip() if search.get("property_type") is not None else None),
                room_type=(str(search["room_type"]).strip().lower() if search.get("room_type") is not None else None),
                custom_filters=(_parse_custom_filters(search["custom_filters"]) if search.get("custom_filters") else None),
                max_pages=_to_int_or_none(search.get("max_pages")),
            )
        )

    # Parse named notifiers.
    notifications = cfg.get("notifications", {})
    if not isinstance(notifications, dict):
        notifications = {}

    notifier_configs: list[NotifierConfig] = []
    for notifier_name, notifier_raw in notifications.items():
        if not isinstance(notifier_raw, dict):
            continue
        notifier_configs.append(_parse_notifier(notifier_name, notifier_raw))

    return AppConfig(
        check_interval_minutes=interval,
        data_dir=data_dir,
        distance_to_location=distance_to_location,
        location_name=location_name,
        location_latitude=location_latitude,
        location_longitude=location_longitude,
        searches=searches,
        notifiers=notifier_configs,
    )
