# Biometric Agent

A standalone service for syncing ZKTeco biometric terminals on a local network
to a Frappe HR site over HTTPS. Run it on a machine that can reach both the
terminals and the site. It does not need a local Frappe/TeebatKarbala checkout,
bench, or database; the site is the remote destination for attendance data.

## Requirements

- Docker with Docker Compose v2
- Network access from the host to each terminal (typically TCP port 4370)
- HTTPS access to the Frappe HR site
- An API key and secret for a site user with the required HR permissions

## Quick start: one terminal

From this directory:

```sh
cp .env.example .env
```

Edit `.env` and set:

| Variable | Description |
| --- | --- |
| `TK_BIOMETRIC_SITE_URL` | Frappe HR site URL, including `https://` |
| `TK_BIOMETRIC_API_KEY` | API key for the site user |
| `TK_BIOMETRIC_API_SECRET` | API secret for the site user |
| `TK_BIOMETRIC_IMPORT_START_DATE` | Earliest date to import punches from |
| `TK_BIOMETRIC_DEVICE_IP` | Terminal IP address, reachable from this host |

Choose the import start date carefully: earlier punches are ignored, and the
first sync can import all punches on the terminal from that date onward.

Try a read-only pass, then start the service:

```sh
docker compose run --rm agent --once --dry-run
docker compose up -d --build
docker compose logs -f agent
```

The dry run does not create checkins or write sync state. The service then syncs
every 15 minutes by default. To stop it, run `docker compose down`.

## Multiple terminals

For multiple terminals, configure them in TOML rather than using the single
`TK_BIOMETRIC_DEVICE_IP` setting:

1. Copy `config.example.toml` to `config.toml`.
2. Set the site URL, import start date, and one `[[devices]]` block per
   terminal. Keep API credentials in `.env`.
3. Remove or blank `TK_BIOMETRIC_DEVICE_IP` in `.env`; when it is set, the
   environment-configured terminal takes precedence over TOML device blocks.
4. Uncomment the `config.toml` volume mount in `compose.yaml`.
5. Start or rebuild the service with `docker compose up -d --build`.

Environment settings override values in `config.toml`. See
[`config.example.toml`](./config.example.toml) for device options such as punch
direction, COMM key, timeouts, and optional log clearing.

## Configuration and state

`.env.example` documents the supported environment variables. Common optional
settings include:

- `TK_BIOMETRIC_INTERVAL`: seconds between sync passes (default: `900`)
- `TK_BIOMETRIC_AGENT_HOST`: name reported for this agent
- `TK_BIOMETRIC_DEVICE_PORT`: terminal port (default: `4370`)
- `TK_BIOMETRIC_DEVICE_TIMEOUT`: terminal timeout in seconds (default: `30`)
- `TK_BIOMETRIC_LOG_LEVEL`: logging level (default: `INFO`)
- `TK_BIOMETRIC_REPORT_TO_SITE`: enable device status reporting when supported

The named Docker volume `state` stores the sync cursor across container
recreations. Removing that volume causes the agent to read punches again from
the configured import start date; duplicate checkins are handled by the agent
and site.

By default, the container uses Docker's bridge network. If the host can reach a
terminal but the container cannot, review the host's network routing and the
optional `network_mode: host` setting in `compose.yaml`.

## One-shot and dry-run commands

```sh
# One sync pass, then exit
docker compose run --rm agent --once

# One read-only pass
docker compose run --rm agent --once --dry-run
```

## Keep credentials private

Use a dedicated site API user with only the permissions the agent needs. Keep
`.env` and any `config.toml` containing credentials out of version control, and
do not include API secrets in logs or support requests.
