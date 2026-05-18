# Daft Notifier

A lightweight Python service that monitors [Daft.ie](https://www.daft.ie) for new property listings and sends push notifications to your phone via [ntfy.sh](https://ntfy.sh).

Runs on a configurable schedule, stores seen listings in SQLite to avoid duplicates, and supports both rental/sharing and residential sale monitoring from the same image.

> Hosting on Unraid? See [README.unraid.md](README.unraid.md) for a step-by-step guide.

---

## Features

- Polls Daft.ie on a configurable interval
- Multiple searches, each with independent filters
- Push notifications via ntfy.sh (mobile-friendly, with title, price, location and link)
- Deduplication — only notified once per listing
- Sales lifecycle tracking — new, price change, removed, relisted
- Optional road distance enrichment via OSRM
- Separate dev/prod notification channels
- `/health` endpoint for uptime monitoring
- Structured logging with rotation
- CI/CD via GitHub Actions → GHCR

---

## Quick Start

### Docker

```bash
cp config.example.yaml config.yaml
# Edit config.yaml with your searches and ntfy topics
docker compose up -d
docker logs -f daft-monitor
```

Volumes:
- `./data/` — SQLite database
- `./logs/` — log files
- `./config.yaml` — config (read-only mount)

Health check: `http://localhost:8080/health`

To build locally from source instead of pulling from GHCR:

```bash
docker compose -f docker-compose.dev.yml up -d --build
```

### Local

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml
python -m daft_monitor
```

Install script/charting extras:

```bash
pip install -r requirements-scripts.txt
```

Run a single cycle (useful for testing):

```bash
python -m daft_monitor --once
```

Runner scripts for local dev: `run-local.bat` (Windows), `run-local.sh` (Linux/macOS).

---

## Configuration

Copy `config.example.yaml` to `config.yaml` and edit it.

### Top-level settings

| Key | Default | Description |
|---|---|---|
| `check_interval_minutes` | `5` | How often to poll. |
| `data_dir` | `./data` | Where the SQLite DB is stored. In Docker use `./data`. |
| `distance_to_location` | `false` | Enable road-distance calculation via OSRM. |
| `location_name` | `""` | Label shown in alerts when distance is enabled. |
| `location_latitude` / `location_longitude` | `null` | Required when `distance_to_location` is `true`. |

### Search options

Only `name`, `search_type`, and `location` are required. Everything else is optional.

| Key | Description |
|---|---|
| `search_type` | `RESIDENTIAL_RENT`, `SHARING`, `RESIDENTIAL_SALE`, etc. |
| `location` | String or list of Daft location names. |
| `min_price` / `max_price` | Price range. |
| `min_beds` / `max_beds` | Bedroom count. |
| `property_type` | `APARTMENT`, `HOUSE`, etc. |
| `sort_type` | `PUBLISH_DATE_DESC`, `PRICE_ASC`, etc. |
| `facilities` | `ENSUITE`, `PARKING`, etc. |
| `room_type` | For SHARING: `double`, `single`, `twin`, `shared`. |
| `max_pages` | Limit pages fetched per cycle. |

Full enum values: [`daftlistings` enums](https://github.com/AnthonyBloomer/daftlistings/blob/master/daftlistings/enums.py).

### Notification channels

Define named channels under `notifications`. Each has a `role` (`alerts` or `errors`) and scoped `environments` (`dev`, `prod`).

```yaml
notifications:
  ntfy-prod-alerts:
    type: ntfy
    role: alerts
    environments: [prod]
    enabled: true
    server: "https://ntfy.sh"
    topic: "my-daft-alerts"
    priority: "default"
    tags: ["house"]
```

### Environment variable overrides

Useful for Docker — override any config value via env:

| Variable | Overrides |
|---|---|
| `DAFT_MONITOR_CONFIG` | Config file path |
| `DAFT_MONITOR_ENVIRONMENT` | `dev` or `prod` |
| `DAFT_MONITOR_LOG_LEVEL` | `debug`, `info`, `error` |
| `DAFT_MONITOR_WRITE_LOGS` | `true` / `false` |
| `DAFT_MONITOR_DATA_DIR` | Database directory |
| `DAFT_MONITOR_STARTUP_TEST_NOTIFICATIONS` | `true` / `false` |

---

## How It Works

1. On startup, loads config, starts health endpoint, optionally sends test notifications.
2. First run seeds the database silently — no alerts sent.
3. Subsequent runs notify only on genuinely new listings.
4. Lifecycle changes (price changes, removals, relistings) are tracked silently in the DB.
5. Sleeps until next interval, then repeats.

---

## Sales Monitoring

Run a second independent container for residential sales alongside rental/sharing:

```bash
# Rental
docker compose up -d

# Sales
cp config.sales.example.yaml config.sales.yaml
# Edit config.sales.yaml
docker compose -f docker-compose.sales.yml up -d
```

Both containers use separate config, data, and log mounts.

For local dual-instance runs outside Docker, set `DAFT_MONITOR_HEALTH_PORT` on one instance to avoid a port clash.

### Lifecycle tracking

The sales monitor tracks listing state without extra Daft requests:
- `new` — first seen
- `price_change` — price changed since last cycle
- `removed` — no longer appearing in search results
- `relisted` — reappeared after removal

Events are stored in `listing_events` and used by analytics scripts. They do not generate push notifications.

> If `max_pages` is set low, listings that fall beyond the page window may be incorrectly marked removed then relisted.

---

## Scripts

Install extras first: `pip install -r requirements-scripts.txt`

| Script | Purpose |
|---|---|
| `scripts/listings_stats.py` | Monthly price metrics for rental/sharing data |
| `scripts/listings_stats.py --generate-image true` | Same + PNG chart |
| `scripts/sales_stats.py --data-dir ./data-sales` | Sale price snapshot, lifecycle counts, distance |
| `scripts/sales_summary.py --data-dir ./data-sales` | Concise weekly summary (terminal) |
| `scripts/sales_summary.py ... --ntfy-topic <topic>` | Same + send via ntfy |
| `scripts/backfill_distances.py` | Backfill road distances for existing listings |

---

## Testing Notifications

```bash
python -m tests.test_notifier              # test alert + error to dev
python -m tests.test_notifier --type alert # alert only
python -m tests.test_notifier --type error --environment prod
```

---

## Things to Be Aware Of

- **Rate limiting** — keep `check_interval_minutes` at 5+ and `max_pages` low (2–3) to avoid Daft blocking you.
- **First run is silent** — intentional, prevents notification flood on startup.
- **ntfy topics are public** — use unique names or add [access control](https://docs.ntfy.sh/config/#access-control).
- **Single writer** — don't run two instances pointing at the same `data_dir`.
- **Host directory ownership** — Docker handles internal permissions but host dirs are your responsibility.

---

## Project Structure

```
Daft-Notifier/
├── daft_monitor/
│   ├── main.py               # Scheduler, cycle logic, signal handling
│   ├── config.py             # YAML loader, validation, env var overrides
│   ├── constants.py          # Shared constants
│   ├── models.py             # Listing + lifecycle event dataclasses
│   ├── storage.py            # SQLite schema, migrations, lifecycle helpers
│   ├── searcher.py           # daftlistings wrapper
│   ├── distance.py           # OSRM distance utilities
│   ├── health.py             # /health HTTP endpoint
│   ├── logging_setup.py      # Log rotation, formatters
│   ├── wide_event.py         # Structured event log builder
│   └── notifiers/            # ntfy implementation
├── scripts/
│   ├── listings_stats.py
│   ├── sales_stats.py
│   ├── sales_charts.py
│   ├── sales_summary.py
│   ├── backfill_coordinates.py
│   └── backfill_distances.py
├── config.example.yaml
├── config.sales.example.yaml
├── docker-compose.yml
├── docker-compose.sales.yml
├── docker-compose.dev.yml
├── docker-compose.sales.dev.yml
├── Dockerfile
├── README.unraid.md
└── README.md
```

---

## License

MIT — see [LICENSE](LICENSE).
