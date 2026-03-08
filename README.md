# Daft Notifier

A lightweight Python service that monitors [Daft.ie](https://www.daft.ie) for new property listings and sends push notifications to your phone. Built on the [`daftlistings`](https://github.com/AnthonyBloomer/daftlistings) library, it runs on a configurable schedule, stores seen listings in SQLite to avoid duplicates, and delivers alerts through [ntfy.sh](https://ntfy.sh).

Designed to run in Docker on a home server (e.g. Unraid) or locally on Windows/Linux.

---

## Features

- **Scheduled polling** — checks Daft.ie at a configurable interval (default 5 minutes).
- **Multiple searches** — define as many saved searches as you need, each with its own filters.
- **Duplicate detection** — tracks listing IDs in SQLite so you only get notified once.
- **Push notifications** — sends mobile-friendly alerts via ntfy.sh with title, price, location, and a direct link.
- **Error notifications** — configurable error alerts sent to a separate ntfy topic when something goes wrong.
- **Environment-aware** — separate `dev` and `prod` notifier channels with independent configuration.
- **Startup self-test** — sends a test alert and test error on every container start to confirm notifications are working.
- **Structured logging** — concise summaries at `info` level, full wide-event JSON at `debug` level.
- **Log file rotation** — incremental log IDs, automatic file rotation, and FIFO cleanup of old files.
- **Cross-platform** — runs on Windows, Linux, and Docker with no external scheduler (no cron needed).

---

## Quick Start

### Docker (recommended for production)

```bash
cp config.example.yaml config.yaml
# Edit config.yaml with your searches and ntfy topics
docker compose up -d --build
docker logs -f daft-monitor
```

State is persisted through mounted volumes:
- `./data/` — SQLite database (`listings.db`)
- `./logs/` — log files
- `./config.yaml` — configuration (read-only mount)

The container runs as `prod` by default, restarts automatically, and sends startup test notifications on every boot.

### Local (for development and testing)

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml
# Edit config.yaml
python -m daft_monitor
```

For a single check cycle (useful for testing):

```bash
python -m daft_monitor --once
```

#### Runner scripts

Convenience scripts are included for local development:

| Platform | Script |
|---|---|
| Windows | `run-local.bat` (uses PowerShell, falls back to cmd) |
| Linux / macOS | `./run-local.sh` |

Both accept four optional positional parameters:

| # | Parameter | Options | Default |
|---|---|---|---|
| 1 | Environment | `dev`, `prod` | `dev` |
| 2 | Log level | `debug`, `info`, `error` | `info` |
| 3 | Write log files | `true`, `false` | `true` |
| 4 | Log directory | any path | `./logs` |

Example:

```bash
# Windows — run in dev, debug logs, write to ./logs
run-local.bat dev debug

# Linux — run in dev, info logs, no file logging
./run-local.sh dev info false
```

Logs are always printed to the terminal regardless of the write-log setting.

---

## Configuration

All configuration lives in `config.yaml`. Copy `config.example.yaml` as a starting point.

### Top-level settings

| Key | Type | Default | Description |
|---|---|---|---|
| `check_interval_minutes` | int | `5` | How often to check for new listings. |
| `data_dir` | string | `./data` | Where the SQLite database is stored. |
| `searches` | list | required | One or more search definitions (see below). |
| `notifications` | object | optional | Named notification channels (see below). |

### Search options

Each entry under `searches` defines a Daft.ie search. Only `name`, `search_type`, and `location` are required — everything else is optional.

| Key | Type | Description |
|---|---|---|
| `name` | string | Friendly label used in logs and notifications. |
| `search_type` | string | Daft search type: `RESIDENTIAL_RENT`, `SHARING`, `RESIDENTIAL_SALE`, etc. |
| `location` | string or list | Location name(s) as accepted by Daft (e.g. `"Dublin City"` or `["Dublin", "Cork"]`). |
| `min_price` / `max_price` | int | Price range filter. |
| `min_beds` / `max_beds` | int | Bedroom count filter. |
| `min_baths` / `max_baths` | int | Bathroom count filter. |
| `property_type` | string | `APARTMENT`, `HOUSE`, etc. |
| `sort_type` | string | `PUBLISH_DATE_DESC`, `PRICE_ASC`, etc. |
| `distance` | string | Search radius: `KM5`, `KM10`, etc. |
| `facilities` | list | Facility filters: `ENSUITE`, `PARKING`, `CABLE_TELEVISION`, etc. |
| `suitable_for` | list | `MALE`, `FEMALE`, etc. |
| `added_since` | string | `DAYS_1`, `DAYS_3`, `DAYS_7`, `DAYS_14`, `DAYS_30`. |
| `min_ber` / `max_ber` | string | BER energy rating range. |
| `owner_occupied` | bool | Owner occupied filter. |
| `min_tenants` / `max_tenants` | int | Tenant count range. |
| `min_lease` / `max_lease` | int | Lease length in months. |
| `min_floor_size` / `max_floor_size` | int | Floor size filter. |
| `room_type` | string | For `SHARING` searches: `double`, `single`, `twin`, `shared`. |
| `custom_filters` | object | Arbitrary Daft API filters not exposed by daftlistings (key-value pairs injected into the request). |
| `max_pages` | int | Limit result pages to reduce API requests. Omit to fetch all. |

All enum values (search type, sort type, facilities, etc.) correspond to the names in the [`daftlistings` enums](https://github.com/AnthonyBloomer/daftlistings/blob/master/daftlistings/enums.py).

### Notification channels

Notifications are defined as named entries under `notifications`. Each channel has a **role** (`alerts` for new listings, `errors` for error reports) and is scoped to one or more **environments** (`dev`, `prod`).

```yaml
notifications:
  ntfy-dev-alerts:
    type: ntfy
    role: alerts
    environments: [dev]
    enabled: true
    server: "https://ntfy.sh"
    topic: "my-daft-dev-alerts"
    priority: "default"
    tags: ["house"]

  ntfy-prod-errors:
    type: ntfy
    role: errors
    environments: [prod]
    enabled: true
    server: "https://ntfy.sh"
    topic: "my-daft-prod-errors"
    priority: "high"
    tags: ["warning"]
```

| Key | Type | Description |
|---|---|---|
| `type` | string | Notification provider. Currently only `ntfy` is supported. |
| `role` | string | `alerts` (new listing notifications) or `errors` (error reports). |
| `environments` | list | Environments this channel is active in: `[dev]`, `[prod]`, or `[dev, prod]`. |
| `enabled` | bool | Set `false` to disable without removing the config. |
| `server` | string | ntfy server URL (default `https://ntfy.sh`). |
| `topic` | string | ntfy topic name. Subscribe to this in the ntfy app. |
| `token` | string | Optional bearer token for authenticated topics. |
| `priority` | string | ntfy priority: `min`, `low`, `default`, `high`, `max`. |
| `tags` | list | ntfy tags shown alongside the notification. |

You can define as many channels as you want. A typical setup has four: dev alerts, dev errors, prod alerts, and prod errors.

### Environment variable overrides

Any configuration value can be overridden with environment variables. These are particularly useful in Docker.

| Variable | Overrides |
|---|---|
| `DAFT_MONITOR_CONFIG` | Path to config file. |
| `DAFT_MONITOR_ENVIRONMENT` | `dev` or `prod`. |
| `DAFT_MONITOR_LOG_LEVEL` | `debug`, `info`, or `error`. |
| `DAFT_MONITOR_WRITE_LOGS` | `true` or `false`. |
| `DAFT_MONITOR_LOG_DIR` | Log file directory path. |
| `DAFT_MONITOR_CHECK_INTERVAL_MINUTES` | Polling interval. |
| `DAFT_MONITOR_DATA_DIR` | Database directory. |

CLI arguments (e.g. `--environment prod`) take precedence over environment variables, which take precedence over `config.yaml` values.

---

## How It Works

1. **Startup** — loads configuration, sets up logging, and sends test notifications to confirm delivery.
2. **First run (seed)** — if the database is empty, all fetched listings are stored silently with no notifications sent. This prevents a flood of alerts on first launch.
3. **Subsequent runs** — fetched listings are compared against the database. Only genuinely new listings trigger notifications.
4. **Sleep** — waits for the configured interval, then repeats. Logs a heartbeat in debug mode so you can tell it's alive.
5. **Shutdown** — handles `SIGTERM` and `SIGINT` gracefully, closing the database cleanly.

---

## Logging

### Terminal output

- **`info` level** — one-line cycle summary per check (fetched count, new count, notification results, duration).
- **`debug` level** — everything from `info` plus full wide-event JSON with HTTP request/response details, service hops, and deduplication stats.
- **`error` level** — errors only.

### File logging

When file logging is enabled, entries are written to `{log_dir}/daft_notifier_{environment}_{date}.log`.

| Setting | Dev | Prod |
|---|---|---|
| Max entries per file | 1,000 | 1,500 |
| Max log files retained | 5 | 10 |

Each log entry gets an incremental ID that persists across restarts. When a file reaches its entry limit, a new file is created. When the file count exceeds the limit, the oldest file is deleted (FIFO). If multiple files are needed on the same date, they are suffixed (`_2`, `_3`, etc.).

### Wide events

Each check cycle produces a single structured event containing:

- Cycle ID and timestamp
- Environment and seed status
- Searches executed with HTTP response details
- Deduplication and storage statistics
- Notification delivery results
- Errors (if any) with context
- Total duration

At `info` level this is a compact one-line summary. At `debug` level the full JSON payload is printed.

---

## Testing Notifications

A test script is included to verify your notification setup without running the full monitor:

```bash
# Send both a test alert and test error to dev
python -m tests.test_notifier

# Send only a test alert to dev
python -m tests.test_notifier --type alert

# Send only a test error to prod (must explicitly pass prod)
python -m tests.test_notifier --type error --environment prod
```

Test notifications are clearly labelled as tests and mimic the format of real alerts.

---

## Things to Be Aware Of

- **Daft.ie rate limiting** — the service sets browser-like HTTP headers to avoid being blocked. If you set `check_interval_minutes` too low or `max_pages` too high, you risk getting rate-limited or blocked. A 5–10 minute interval with 2–3 pages is a safe default.
- **First run is silent** — the initial check seeds the database without sending notifications. This is intentional.
- **ntfy topics are public by default** — anyone who knows your topic name can subscribe. Use unique topic names or set up [ntfy access control](https://docs.ntfy.sh/config/#access-control) with tokens.
- **`misc_filters`** — these are parsed and logged but the `daftlistings` library currently has no setter for them. They are included for forward compatibility.
- **`custom_filters`** — these inject raw key-value pairs into the Daft API payload. They bypass the library entirely, so use them carefully and only when a filter is not available through the standard options.
- **Unicode in listings** — some listing titles contain special characters (e.g. `€`). These are automatically sanitised in notification headers to avoid encoding errors.
- **SQLite locking** — the database is single-writer. Don't run multiple instances of the monitor against the same `data_dir`.
- **Docker volumes** — on first run, ensure the `data/` and `logs/` directories exist on the host or Docker will create them as root-owned. The Dockerfile handles internal permissions but host directory ownership is your responsibility.
- **Container restarts** — the Docker container is set to `restart: unless-stopped`. Every restart triggers startup test notifications. This is intentional — it confirms notifications work after any crash or host reboot.

---

## Project Structure

```
Daft-Notifier/
├── daft_monitor/
│   ├── __init__.py
│   ├── __main__.py          # Entry point for python -m daft_monitor
│   ├── main.py              # Scheduler, cycle logic, signal handling
│   ├── config.py            # YAML loader, validation, env var overrides
│   ├── models.py            # Listing dataclass
│   ├── searcher.py          # daftlistings wrapper, HTTP headers, retries
│   ├── storage.py           # SQLite operations (insert, dedup, seed detection)
│   ├── wide_event.py        # Structured wide-event log builder
│   ├── logging_setup.py     # Log rotation, incremental IDs, formatters
│   └── notifiers/
│       ├── __init__.py       # Notifier factory (builds by role + environment)
│       ├── base.py           # Abstract notifier interface
│       └── ntfy.py           # ntfy.sh implementation
├── tests/
│   └── test_notifier.py     # Notification test script
├── config.example.yaml      # Template configuration
├── config.yaml              # Your configuration (git-ignored)
├── requirements.txt         # Python dependencies
├── Dockerfile               # Container image (python:3.12-slim)
├── docker-compose.yml       # Container orchestration with volumes
├── run-local.bat            # Windows runner script
├── run-local.ps1            # PowerShell runner script
├── run-local.sh             # Linux/macOS runner script
├── LICENSE                  # MIT
└── README.md
```

---

## License

MIT — see [LICENSE](LICENSE).
