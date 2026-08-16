# Daft Notifier on Unraid

This guide covers hosting `daft-notifier` on Unraid using Compose Manager.

The public GitHub repo must not contain your live configs, ntfy topics, or database files. Keep those only under `/mnt/user/appdata/`.

---

## 1) Prerequisites

- Unraid server with Docker enabled
- Compose Manager plugin installed
- A way to edit files under `/mnt/user/appdata/`
- ntfy topics ready for **errors** (listing alerts can stay off at first)
- Watchtower / any auto-updater **paused** for these containers before a schema-changing release

### Before you copy anything into Git

If Daft session cookies or account tokens were ever saved on a PC (browser export, HAR, example payload):

1. Sign out of Daft.ie on every browser and revoke/end other sessions if the account settings allow it.
2. Change the Daft password if you use one.
3. Delete the capture files. They must never be committed.

Rotate ntfy topic names when moving to observation mode, and add a bearer token if you can. Old topic names in a public repo or chat log are effectively open mailboxes.

---

## 2) Create folders and config files

Create these folders:

- `/mnt/user/appdata/daft-notifier/data`
- `/mnt/user/appdata/daft-notifier/logs`
- `/mnt/user/appdata/daft-notifier/data-sales`
- `/mnt/user/appdata/daft-notifier/logs-sales`

Create these config files **on the server only**:

- `/mnt/user/appdata/daft-notifier/config.yaml` (rental/sharing)
- `/mnt/user/appdata/daft-notifier/config.sales.yaml` (sales)

Use:

- `config.example.yaml` as the base for `config.yaml`
- `config.sales.example.yaml` as the base for `config.sales.yaml`

Those examples watch **all sharing, residential rent, and residential sale listings** in Dublin, Kildare, Meath, and Wicklow, with `notify: false` and `deep_scan: true`. They do not include New Homes or Student Accommodation.

Important for Docker in `config.sales.yaml`:

- use `data_dir: "./data"` (not `./data-sales`)
- the host-side `data-sales` path is handled by the volume mount

First week:

- leave listing-alert notifiers `enabled: false`
- leave `digest_day` unset
- keep error notifiers `enabled: true`

---

## 3) Compose stack (rental + sales)

Pin the image to a **release tag**, not `latest`. Replace `vX.Y.Z` with the tag you actually deployed.

```yaml
services:
  daft-monitor:
    image: ghcr.io/samade1/daft-notifier:vX.Y.Z
    pull_policy: always
    container_name: daft-monitor
    restart: unless-stopped
    ports:
      - "18081:8080"
    volumes:
      - /mnt/user/appdata/daft-notifier/data:/app/data
      - /mnt/user/appdata/daft-notifier/logs:/app/logs
      - /mnt/user/appdata/daft-notifier/config.yaml:/app/config.yaml:ro
    environment:
      DAFT_MONITOR_CONFIG: /app/config.yaml
      DAFT_MONITOR_ENVIRONMENT: prod
      DAFT_MONITOR_LOG_LEVEL: info
      DAFT_MONITOR_WRITE_LOGS: "true"
      DAFT_MONITOR_LOG_DIR: /app/logs
      DAFT_MONITOR_STARTUP_TEST_NOTIFICATIONS: "false"

  daft-monitor-sales:
    image: ghcr.io/samade1/daft-notifier:vX.Y.Z
    pull_policy: always
    container_name: daft-monitor-sales
    restart: unless-stopped
    ports:
      - "18082:8080"
    volumes:
      - /mnt/user/appdata/daft-notifier/data-sales:/app/data
      - /mnt/user/appdata/daft-notifier/logs-sales:/app/logs
      - /mnt/user/appdata/daft-notifier/config.sales.yaml:/app/config.yaml:ro
    environment:
      DAFT_MONITOR_CONFIG: /app/config.yaml
      DAFT_MONITOR_ENVIRONMENT: prod
      DAFT_MONITOR_LOG_LEVEL: info
      DAFT_MONITOR_WRITE_LOGS: "true"
      DAFT_MONITOR_LOG_DIR: /app/logs
      DAFT_MONITOR_STARTUP_TEST_NOTIFICATIONS: "false"
```

Notes:

- Keep ports different (`18081`, `18082`).
- Keep rental and sales data/log folders separate.
- Pause Watchtower for both container names until you choose to move to a newer tag.

---

## 4) Nightly database backups

Appdata Backup is useful, but also keep SQLite-consistent copies on another share.

Destination used previously: `/mnt/user/Archive/daft-notifier-backups` with 14 rotations.

Unraid 7 may not have `sqlite3` on the host. Use Python inside the running container:

```bash
#!/bin/bash
set -euo pipefail

DEST_DIR="/mnt/user/Archive/daft-notifier-backups"
KEEP=14
STAMP=$(date +%Y%m%d_%H%M%S)
mkdir -p "$DEST_DIR"

backup_one() {
  local label="$1"
  local container="$2"
  local dest="$DEST_DIR/${label}_${STAMP}.db"
  local temp_db="/tmp/${label}_${STAMP}.db"

  docker exec "$container" python -c '
import sqlite3, sys
source = sqlite3.connect(sys.argv[1])
destination = sqlite3.connect(sys.argv[2])
try:
    source.backup(destination)
finally:
    destination.close()
    source.close()
' "/app/data/listings.db" "$temp_db"

  docker cp "${container}:${temp_db}" "$dest"
  docker exec "$container" rm -f "$temp_db"

  docker cp "$dest" "${container}:/tmp/verify_${label}.db"
  integrity_result=$(docker exec "$container" python -c '
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
print(conn.execute("PRAGMA integrity_check").fetchone()[0])
conn.close()
' "/tmp/verify_${label}.db")
  docker exec "$container" rm -f "/tmp/verify_${label}.db"

  if [ "$integrity_result" != "ok" ]; then
    echo "FAIL: $label integrity $integrity_result"
    rm -f "$dest"
    return 1
  fi
  echo "OK: $dest"
}

backup_one "rent" "daft-monitor"
backup_one "sales" "daft-monitor-sales"

prune() {
  local label="$1"
  ls -1t "$DEST_DIR"/${label}_*.db 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
    rm -f "$old"
  done
}
prune "rent"
prune "sales"
```

Test the script once by hand and confirm two files appear.

---

## 5) Deploy an observation-mode upgrade

Do this **one container at a time** (rental first, sales second).

1. Confirm nightly backups work.
2. Pause Watchtower.
3. Stop both containers.
4. Snapshot both DBs and both configs to a named folder, for example `/mnt/user/Archive/daft-notifier-backups/pre-vX.Y.Z/`.
5. Copy the new example searches into the server configs. Keep your real ntfy topics; do not copy those back into Git.
6. Start **only** `daft-monitor` on the pinned tag.
7. Check:
   - `http://<unraid-ip>:18081/health`
   - logs show `status=ok`
   - each search gets a silent baseline (`seed`, not a flood of listing alerts)
   - no repeating HTTP 403/429
   - disk use on `data/` is growing sanely
8. Wait 24–48 hours so every rental/sharing search gets a deep-scan turn.
9. If healthy, start `daft-monitor-sales` the same way and wait another 24–48 hours.

Optional check against a **copy** of the DB (never the only live file):

```bash
python scripts/verify_observation_migration.py --data-dir ./data-copy
python scripts/backfill_price_parse.py --data-dir ./data-copy
```

---

## 6) Monitoring with Uptime Kuma

The containers expose `/health` (HTTP 200 when the process is alive). Add monitors:

- **Type:** HTTP(s)
- **URL:** `http://<unraid-ip>:18081/health` (and `:18082` for sales)
- **Heartbeat Interval:** 60 seconds

This does not detect a stuck cycle or a Daft outage. Watch logs and error ntfy messages for that.

---

## 7) Updating the image

- Do not auto-follow `latest` across a schema change.
- Redeploy Compose Manager onto a new tag when you choose to.
- `pull_policy: always` is fine **with a pinned tag**.

---

## 8) Common mistakes

- Using `data_dir: "./data-sales"` inside Docker config files.
  - Fix: use `data_dir: "./data"` and mount host `data-sales` to `/app/data`.
- Reusing the same host folders for rental and sales.
- Committing `config.yaml`, live Unraid configs, or Daft payload captures to the public repo.
- Leaving listing alerts enabled on broad county-wide searches.
- Setting `deep_scan_max_pages` below the live Daft result count.
- Typo in ntfy server URL — must be `https://ntfy.sh` unless you self-host.

---

## 9) Rollback

If the new image misbehaves **before** you care about data it wrote:

1. Stop the affected container.
2. Restore the pre-release `listings.db` and config from the named snapshot.
3. Repin the previous image tag and start it.

Do not point the old image at a database the new image already migrated if you need a clean downgrade. Restore the snapshot instead.

To disable sales only: stop `daft-monitor-sales` and leave rental running.
