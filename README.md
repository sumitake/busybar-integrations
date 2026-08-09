# BUSY Bar Integrations

[![ci](https://github.com/sumitake/busybar-integrations/actions/workflows/ci.yml/badge.svg)](https://github.com/sumitake/busybar-integrations/actions/workflows/ci.yml)
[![CodeQL](https://github.com/sumitake/busybar-integrations/actions/workflows/codeql.yml/badge.svg)](https://github.com/sumitake/busybar-integrations/actions/workflows/codeql.yml)
[![secret-scan](https://github.com/sumitake/busybar-integrations/actions/workflows/secret-scan.yml/badge.svg)](https://github.com/sumitake/busybar-integrations/actions/workflows/secret-scan.yml)
[![License: MPL-2.0](https://img.shields.io/badge/License-MPL--2.0-blue.svg)](LICENSE)

Local-API integrations for the BUSY Bar — a 72×16 LED status display on USB or LAN.

## Requirements

- **BUSY Bar** on USB (default address `10.0.4.20`) or LAN
- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** — fast Python package installer and resolver

Per-integration extras (e.g., macOS Calendar access for `calendar_countdown`) are noted in each integration's README.

## Quick start

1. Clone the repo:
   ```bash
   git clone https://github.com/your-org/busybar-integrations.git
   cd busybar-integrations
   ```

2. Sync dependencies:
   ```bash
   uv sync
   ```

3. Copy and edit the configuration:
   ```bash
   cp config.example.toml config.toml
   ```
   Edit `config.toml` to set your BUSY Bar address and integration-specific settings.

4. Test an integration with dry-run:
   ```bash
   cd integrations
   uv run python -m calendar_countdown.main --once --dry-run
   ```
   Or for CI status:
   ```bash
   uv run python -m ci_status.main --once --dry-run
   ```

## How it works

The display is a shared 72×16 canvas. Each integration publishes text, shapes, or status via the `busybar.client.BusyBarClient` API (see [`src/busybar/client.py`](src/busybar/client.py)). The display arbitrates by **priority**, through the shared ladder in [`src/busybar/display.py`](src/busybar/display.py):

| Priority | Tier | Occupied by |
|---|---|---|
| 20 | `PRIORITY_AMBIENT` | `calendar_countdown`'s baseline countdown (normal and in-progress) |
| 21 | `PRIORITY_OVERLAY` | `ci_status`'s rotation — running badge, GitHub GraphQL/REST quota gauges, and failure/stuck/quiet-green frames |
| 25 | `PRIORITY_AMBIENT_RAISED` | `calendar_countdown` inside `approach_minutes`, outside `notice_minutes` — no longer interruptible by the overlay tier |
| 60 | `PRIORITY_ALERT` | Reserved/unused — no in-repo integration currently draws here |
| 65 | `PRIORITY_AMBIENT_URGENT` | `calendar_countdown` inside `notice_minutes`/`warn_minutes` |
| 90 | `PRIORITY_SESSION` | An authenticated BUSY/CUSTOM work session on the device — outranks everything else |

Two firmware facts shape all of the above: equal priority from a different `application_name` is **rejected** (`409`), not a hand-off — only a strictly higher number preempts; and a preempted app's elements are **evicted, not restored** — the lower-priority app only reclaims the screen via its own next scheduled redraw, never automatically. Each element carries an optional `timeout`; if its source doesn't refresh within that window, the element self-clears rather than sticking on screen indefinitely.

**Overlay dwell/rotation.** `ci_status`'s overlay-tier frames (running badge, then the GraphQL and REST quota gauges) each draw for one `OVERLAY_DWELL_SECONDS` (10s) dwell slot, then stay silent for at least one more dwell period before redrawing — giving `calendar_countdown`'s own ambient-tier redraws (also tuned to a 10s cadence) a real chance to land in the resulting gap. Because eviction is one-way, the two integrations trade the panel back and forth rather than alternating cleanly; see each integration's README for the measured recovery rates.

**Escalation beats alerts.** `ci_status` no longer draws at `PRIORITY_ALERT`; failure and stuck-queue frames now rotate at `PRIORITY_OVERLAY` (21) alongside the running badge and quota gauges, under the calendar's ambient tiers. As an upcoming calendar event gets closer, `calendar_countdown` climbs from `PRIORITY_AMBIENT` (20) through `PRIORITY_AMBIENT_RAISED` (25, inside `approach_minutes`) to `PRIORITY_AMBIENT_URGENT` (65, inside `notice_minutes`/`warn_minutes`), which already sits strictly above the overlay tier — so an imminent event naturally outranks a CI failure, and the failure frame alternates with the calendar's own redraws rather than camping the panel.

The `application_name` field tags each draw's source, letting the display track ownership and multi-instance behavior.

## Adding an integration

To add a new integration:

1. Read [`src/busybar/client.py`](src/busybar/client.py) — the public API for drawing to the display.
2. Follow the **logic/adapter split**:
   - **Logic module**: your domain (e.g., polling a calendar or API, computing state).
   - **Adapter module** (`main.py`): connects logic to the BusyBar display, handles CLI args, and lifecycle.
3. Add per-integration docs to your `README.md` — document config options, API tokens, and any platform-specific setup (e.g., macOS Calendar permission prompts).

## Configuration

`config.toml` is **not checked in** (it's in `.gitignore`). This repo follows a **no-secrets-by-construction** policy:

- All secrets (API tokens, credentials) go in `config.toml`, which you provide locally.
- The repo ships only `config.example.toml`, documenting all fields and defaults.
- CI/CD can inject secrets via environment-variable expansion in config parsing if needed.

## Cloud transport

By default, `BusyBarClient` talks to your BUSY Bar directly over the LAN
(`[device].host`). As of v1.6, it can automatically fall back to BUSY's
cloud relay if the local device becomes unreachable — USB unplugged,
Wi-Fi drop, the device off — and recover back to local on its own once
it's reachable again. This is entirely optional and off by default.

### Setting it up

1. Create a token at [cloud.busy.app](https://cloud.busy.app) → **API
  tokens** tab → create a new token with the **"BUSY Bar"** scope. This
  scope grants full control of exactly one linked device — there's no
  separate device ID to configure; the token itself identifies which
  device it talks to.
2. Add it to your `config.toml` (**never** `config.example.toml`, and
  never anything committed to the repo — see "Configuration" above):
   ```toml
   [device]
   host = "10.0.4.20"
   cloud_token = "paste-your-real-token-here"
   ```
3. Optionally set `transport` (default `"auto"`):
   - `"auto"` — local first, cloud fallback when the local device is
     unreachable and `cloud_token` is set. Recovers back to local
     automatically.
   - `"local"` — local only, never falls back (identical to pre-v1.6
     behavior; the default if you never set `cloud_token`).
   - `"cloud"` — forced cloud only, never attempts local. Mainly useful
     for deliberately exercising/debugging the cloud path.
4. `cloud_base_url` defaults to `https://api.busy.app/busybar` and
   normally doesn't need to change — see the base-URL note below.

### Rotating or revoking a token

Manage tokens from the same **API tokens** tab on cloud.busy.app.
Revoking a token takes effect **immediately and cannot be undone** — if
you're rotating, create and deploy the replacement token first, then
revoke the old one, rather than revoking first.

### What cloud fallback does NOT cover

Continuous status streaming (`/api/status/ws`) is local-only by design —
the cloud API has no equivalent, so a caller relying on the status
WebSocket will not get a cloud fallback for it. Everything else this
client uses (`draw`, `clear`, `status`, `get_busy`, `set_busy_simple`,
`play_audio`) is a synchronous request/response call and mirrors 1:1
over cloud.

### Verified against the live cloud API

The v1.6 launch tests were entirely mocked; the checklist that shipped
with that round has since been run against a real device and a real
token, with these results:

- **Base URL confirmed.** `https://api.busy.app/busybar` (the shipped
  `cloud_base_url` default) is correct and working — the `busylib-py`
  discrepancy noted during research (its own hardcoded default is the
  differently-hosted `https://proxy.busy.app`) does not apply to this
  client. No change needed to `cloud_base_url`.
- **Forced-cloud draw probe: 5/5 `DRAWN`.** Round-trip latency
  300–465ms, median 353ms — well inside the cloud transport's `(5, 15)`s
  timeout, and ample headroom under `calendar_countdown`'s 10s ambient
  redraw cadence (a cloud-relayed redraw comfortably completes well
  before the next one is due).
- **Auto-fallback is live in production.** Running with `transport =
  "auto"`; local→cloud degradation and cloud→local recovery transitions
  are logged at `INFO` exactly as designed (see `BusyBarClient`'s class
  docstring in [`src/busybar/client.py`](src/busybar/client.py)).

## What's inside

| Integration | Description |
|---|---|
| [`calendar_countdown`](integrations/calendar_countdown/) | Live countdown to your next macOS Calendar event. Four-stage escalation as an event approaches — `approach_minutes` (30m default), `notice_minutes` (15m, amber), `warn_minutes` (5m, red), and a final-minute LED blink — plus one audio chirp precisely at event start. The countdown itself turns teal while the event is in progress. Optional `auto_busy` starts a BUSY session automatically for the event's duration. |
| [`ci_status`](integrations/ci_status/) | GitHub Actions status via the REST API with ETag caching (near-zero steady-state quota cost). Failure and stuck-queue frames rotate calmly at the overlay tier — `CI FAIL owner/repo #42 · workflow` (PR number, or branch when there's no PR) — with a gentle red LED while a workflow is failing. While a run is active, the same rotation adds a running badge (ETA plus a "remain"/"left" label) alongside GitHub GraphQL/REST quota gauges. Optional account-wide watching auto-discovers and monitors every repo you own, not just an explicit list. |
