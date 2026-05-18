# Daft Notifier on Unraid

This guide covers hosting `daft-notifier` on Unraid using Compose Manager.

If you are new to self-hosting, follow this file end-to-end.

---

## 1) Prerequisites

- Unraid server with Docker enabled
- Compose Manager plugin installed
- A way to edit files under `/mnt/user/appdata/`
- ntfy topics ready for alerts/errors

---

## 2) Create folders and config files

Create these folders:

- `/mnt/user/appdata/daft-notifier/data`
- `/mnt/user/appdata/daft-notifier/logs`
- `/mnt/user/appdata/daft-notifier/data-sales`
- `/mnt/user/appdata/daft-notifier/logs-sales`

Create these config files:

- `/mnt/user/appdata/daft-notifier/config.yaml` (rental/sharing)
- `/mnt/user/appdata/daft-notifier/config.sales.yaml` (sales)

Use:
- `config.example.yaml` as the base for `config.yaml`
- `config.sales.example.yaml` as the base for `config.sales.yaml`

Important for Docker in `config.sales.yaml`:
- use `data_dir: "./data"` (not `./data-sales`)
- the host-side `data-sales` path is handled by the volume mount

---

## 3) Compose stack (rental + sales)

Add this stack in Compose Manager:

```yaml
services:
  daft-monitor:
    image: ghcr.io/samade1/daft-notifier:latest
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
    image: ghcr.io/samade1/daft-notifier:latest
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
- Keep ports different (`18081`, `18082`) to avoid conflicts.
- Keep rental and sales data/log folders separate.

---

## 4) Deploy and verify

After stack deploy:

1. Check containers are running in Unraid Docker UI.
2. Check health endpoints:
   - `http://<unraid-ip>:18081/health`
   - `http://<unraid-ip>:18082/health`
3. Check logs for both containers and confirm cycle summaries show `status=ok`.
4. Confirm notifications arrive on the expected ntfy topics.

---

## 5) Monitoring with Uptime Kuma

The containers expose a `/health` endpoint (HTTP 200 when running). Add monitors in Uptime Kuma:

- **Type:** HTTP(s)
- **URL:** `http://<unraid-ip>:18081/health` (and `:18082` for sales)
- **Heartbeat Interval:** 60 seconds

Uptime Kuma has built-in ntfy support, so you can reuse the same topics already in your config.

---

## 6) Updating the image

- If you use Watchtower, it can auto-update images.
- If not, redeploy stack in Compose Manager to pull latest image.
- `pull_policy: always` helps ensure fresh pulls during deploys.

Optional safer rollout:
- pin image tags instead of `latest`:

```yaml
image: ghcr.io/samade1/daft-notifier:<tag>
```

---

## 7) Common mistakes

- Using `data_dir: "./data-sales"` inside Docker config files.
  - Fix: use `data_dir: "./data"` and mount host `data-sales` to `/app/data`.
- Reusing same host folders for rental and sales.
  - Fix: keep `data` vs `data-sales` and `logs` vs `logs-sales` separate.
- Typo in ntfy server URL.
  - Fix: must be `https://ntfy.sh`.

---

## 8) Rollback

To disable sales monitor only:

1. Stop/remove `daft-monitor-sales` service from the stack.
2. Keep `daft-monitor` running.
3. Optionally archive/remove:
   - `/mnt/user/appdata/daft-notifier/data-sales`
   - `/mnt/user/appdata/daft-notifier/logs-sales`
   - `/mnt/user/appdata/daft-notifier/config.sales.yaml`
