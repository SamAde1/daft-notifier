# Daft Notifier

A Python service that watches [Daft.ie](https://www.daft.ie) for rental, sharing, and sale listings, records them in SQLite, and can send push notifications via [ntfy.sh](https://ntfy.sh).

It started as a new-listing alerter. It now also supports **observation mode**: broad, silent searches that record the market so you can analyse it later. The same image runs rental/sharing and sales as separate containers.

> Hosting on Unraid? See [README.unraid.md](README.unraid.md) for backups, pinned images, and rollout.

---

## Features

- Polls Daft.ie on a configurable interval
- Multiple searches, each with its own filters, `notify` flag, and optional deep scan
- Push notifications via ntfy.sh (alerts, errors, optional weekly digest)
- Silent first-run / criteria-change seeding — existing stock is recorded, not treated as brand new
- Lifecycle tracking: new, seed, price change, removed, relisted
- Deep scans with completeness checks and a grace period before a listing is treated as gone
- Optional road distance via OSRM
- Separate dev/prod notification channels
- `/health` endpoint for uptime monitoring
- Structured logging with rotation
- CI: unit tests, secret scan, then Docker build to GHCR

---

## Quick Start

### Docker

```bash
cp config.example.yaml config.yaml
# Edit config.yaml: ntfy topics, and enable alerts only if you want them
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

Pin production to a release tag rather than `latest`:

```bash
set DAFT_NOTIFIER_IMAGE=ghcr.io/samade1/daft-notifier:v2.0.0
docker compose up -d
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

Copy `config.example.yaml` to `config.yaml` (and `config.sales.example.yaml` to `config.sales.yaml` for sales). **Do not commit those copies.** They belong on the machine that runs the monitor.

The shipped examples are four-county observation configs for Dublin, Kildare, Meath, and Wicklow: silent (`notify: false`), one search per county, no price/bed/property-type filters. Listing alerts are disabled; error alerts stay on. Weekly digest is commented out until you have a clean week of data.

### Top-level settings

| Key | Default | Description |
|---|---|---|
| `check_interval_minutes` | `5` | How often to poll. Observation rent example uses `30`; sales uses `120`. |
| `data_dir` | `./data` | Where the SQLite DB is stored. In Docker use `./data`. |
| `distance_to_location` | `false` | Enable road-distance calculation via OSRM. |
| `location_name` | `""` | Label shown in alerts when distance is enabled. |
| `location_latitude` / `location_longitude` | `null` | Required when `distance_to_location` is `true`. |
| `removal_grace_hours` | `48` | How long a listing must stay missing after a complete deep scan before it is removed. |
| `deep_scan_max_pages` | unset | Safety cap for deep/baseline pagination (50 listings per page). |
| `deep_scan_min_interval_hours` | `24` | Minimum time between complete deep scans of the same search. |
| `digest_day` / `digest_hour` / `digest_timezone` | unset / `9` / `Europe/Dublin` | Weekly digest schedule. Leave `digest_day` unset to disable. |

### Search options

Only `name`, `search_type`, and `location` are required. Everything else is optional.

| Key | Description |
|---|---|
| `id` | Stable search identity. Defaults to `name`. Keep this fixed if you rename the display name. |
| `search_type` | `RESIDENTIAL_RENT`, `SHARING`, `RESIDENTIAL_SALE`, etc. |
| `location` | String or list of Daft location names. |
| `notify` | `false` records listings silently. `true` (default) sends alerts for new/reactivated memberships. |
| `deep_scan` | `true` enables full pagination used to prove a listing is gone. Default `false`. |
| `shallow_pages` | Pages fetched every cycle (newest first). Deep-scan searches should keep this small (1–2). |
| `min_price` / `max_price` | Price range. |
| `min_beds` / `max_beds` | Bedroom count. |
| `property_type` | `APARTMENT`, `HOUSE`, etc. Omit to include all residential types. |
| `sort_type` | `PUBLISH_DATE_DESC`, `PRICE_ASC`, etc. |
| `facilities` | `ENSUITE`, `PARKING`, etc. |
| `room_type` | For SHARING: `double`, `single`, `twin`, `shared`. |
| `max_pages` | Legacy page cap. Prefer `shallow_pages` plus `deep_scan`. |

Full enum values: [`daftlistings` enums](https://github.com/AnthonyBloomer/daftlistings/blob/master/daftlistings/enums.py).

### How scanning works

Each cycle:

1. **Shallow scan** every search (typically 2 pages). This is how new listings are found. It is never treated as proof that something disappeared.
2. **At most one baseline** for a newly added or changed search, fully paginated. That first inventory is stored as `seed`, not `new`.
3. **At most one deep scan** for an eligible `deep_scan: true` search, fully paginated with pauses between pages. Only a *complete* deep scan can start the removal grace clock.

A deep scan is complete only if it reached a short last page, had no HTTP or mapping errors, and was not cut off by `deep_scan_max_pages`. If Daft returns 403/429, the scan is aborted and marked incomplete. Removals wait; they are not guessed from a partial fetch.

### Notification channels

Define named channels under `notifications`. Each has a `role` (`alerts`, `errors`, or `digest`) and scoped `environments` (`dev`, `prod`).

```yaml
notifications:
  ntfy-prod-errors:
    type: ntfy
    role: errors
    environments: [prod]
    enabled: true
    server: "https://ntfy.sh"
    topic: "my-daft-prod-errors"
    token: "optional-bearer-token"
    priority: "high"
    tags: ["warning"]
```

Public ntfy topics can be read or written by anyone who knows the name. Use unique names and, preferably, a token.

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

Keep production on `info`. Debug logs can include Daft response previews.

---

## How It Works

1. On startup, loads config, starts the health endpoint, optionally sends test notifications.
2. Registers each search. If it is new or its filters changed, it stays in seed mode until a complete baseline finishes.
3. Shallow scans find new listings. Deep scans, over days, confirm what is still on the market.
4. Listings from `notify: false` searches are stored only. Errors still go to the error channel.
5. Sleeps until the next interval, then repeats.

Old removal/relist rows from before the deep-scan logic are kept in the database but marked untrusted by epoch timestamps. Analytics and the digest ignore them on purpose.

---

## Sales Monitoring

Run a second independent container for residential sales:

```bash
# Rental / sharing
docker compose up -d

# Sales
cp config.sales.example.yaml config.sales.yaml
# Edit config.sales.yaml
docker compose -f docker-compose.sales.yml up -d
```

Both containers use separate config, data, and log mounts. Do not point two instances at the same `data_dir`.

For local dual-instance runs outside Docker, set `DAFT_MONITOR_HEALTH_PORT` on one instance to avoid a port clash.

### Lifecycle tracking

- `seed` — already on the market when a search was added or its filters changed
- `new` — first seen after seeding finished
- `price_change` — price text changed
- `removed` — missing from a complete deep scan for longer than the grace period (or, for non-deep searches, missing from a successful shallow fetch)
- `relisted` — came back after removal

These events are stored for analytics. They do not generate listing alerts unless you add that later.

---

## Scripts

Install extras first: `pip install -r requirements-scripts.txt`

| Script | Purpose |
|---|---|
| `scripts/listings_stats.py` | Monthly price metrics for rental/sharing data |
| `scripts/sales_stats.py --data-dir ./data-sales` | Sale price snapshot, lifecycle counts, distance |
| `scripts/sales_summary.py --data-dir ./data-sales` | Concise weekly summary (terminal) |
| `scripts/backfill_distances.py` | Backfill road distances for existing listings |
| `scripts/backfill_price_parse.py` | Fill parsed price columns on older rows |
| `scripts/analyze_market.py --data-dir ./data --segment all` | Price/velocity/posting-time report |
| `scripts/measure_search_capacity.py` | First-page Daft counts; sizes `deep_scan_max_pages` |
| `scripts/verify_observation_migration.py --data-dir ./data` | Schema/integrity check on a DB copy |

Copy the SQLite file off the server and run analytics on a laptop. Do not run pandas jobs inside the monitor container.

---

## Testing Notifications

```bash
python -m tests.test_notifier              # test alert + error to dev
python -m tests.test_notifier --type alert # alert only
python -m tests.test_notifier --type error --environment prod
```

---

## Things to Be Aware Of

- **Rate limiting** — keep `check_interval_minutes` at 30+ for broad observation. Shallow scans should stay at 1–2 pages. Deep scans already pause between pages.
- **Page cap** — if `deep_scan_max_pages` is below the live result count, that search never completes and never confirms removals. Measure with `scripts/measure_search_capacity.py` before widening searches.
- **First run is silent** — intentional.
- **ntfy topics are public unless protected** — unique names, preferably with a token.
- **Single writer** — don't run two instances pointing at the same `data_dir`.
- **Do not commit** live configs, database files, Daft request captures, cookies, or tokens. The repo is public.
- **Database growth** — listing history is kept forever. `search_runs` grows with every cycle; plan backups and, later, retention.
- **Health is liveness only** — `/health` means the process is up, not that Daft or the last cycle succeeded.

---

## Project Structure

```
Daft-Notifier/
├── daft_monitor/
│   ├── main.py               # Scheduler, cycle logic, signal handling
│   ├── config.py             # YAML loader, validation, env var overrides
│   ├── storage.py            # SQLite schema, migrations, lifecycle helpers
│   ├── searcher.py           # Daft queries, shallow/deep pagination
│   ├── lifecycle_v2.py       # Completeness and grace-period rules
│   ├── digest.py             # Weekly text digest
│   ├── analytics.py          # Offline analysis helpers (not used at runtime)
│   └── notifiers/            # ntfy implementation
├── scripts/
├── tests/
├── config.example.yaml
├── config.sales.example.yaml
├── docker-compose.yml
├── docker-compose.sales.yml
├── README.unraid.md
└── README.md
```

---

## License

MIT — see [LICENSE](LICENSE).
