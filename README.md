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
   git clone https://github.com/sumitake/busybar-integrations.git
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

## Firmware 1.2.3 support

The local API is capability-probed through `GET /api/version` (`api_semver`).
API **27.5.0+** enables selective element cleanup, explicit drawing order
(`z_index`), and small inline XPM2 CI icons. Older/unknown firmware and cloud
relay retain the ordinary text/shape payloads. No new configuration is needed
for these display improvements; `[ci_status] bitmap_icons = false` disables
the cosmetic icons.

Calendar and CI still send complete, expiring frames on their established
cadence. At a same-priority layout change, selective cleanup removes obsolete
IDs while keeping common content visible. Type changes and priority reductions
use an app-scoped full clear. Neither path is an atomic frame transaction;
timeouts and subsequent full redraws provide recovery after preemption or a
failed request. Firmware 1.2.3 has an application-name parsing bug in the
selective DELETE body, so this client always puts ownership in the query.

The large calendar countdown and device-native Nyan animation remain in use.
The firmware's native countdown font is too small for the existing calendar
layout; Nyan already plays its uploaded animation on the bar. Firmware fixes
for Wi-Fi status streaming and networking benefit the existing local API
without adding another background listener.

### Inspect the device

```bash
uv run python -m busybar diagnose
uv run python -m busybar diagnose --host 10.0.4.20 --screen screen.bmp
```

If a macOS editable install reports `No module named busybar` (Python can
ignore a `.pth` file marked hidden), run from the repository root with
`PYTHONPATH=src uv run python -m busybar diagnose`. This uses the same source
modules without relying on the editable-install file.

Diagnostics read firmware/API versions, local transport, power and BUSY
snapshot availability. They never dump tokens/configuration, play audio,
start timers, or write device logs. An incomplete report exits nonzero.
Screen capture converts firmware 1.2.3's base64 BGR framebuffer into a standard
BMP; the endpoint's `image/bmp` header does not describe its actual wire data.

### Local tokens and USB/Wi-Fi recovery

```toml
[device]
host = "10.0.4.20"
fallback_hosts = ["192.0.2.20"] # replace with your bar's Wi-Fi address
local_token = ""              # preferably supply BUSYBAR_LOCAL_TOKEN instead
transport = "auto"
discover = false
# device_id = "001122aabbcc"  # USB MAC with colons removed, for opt-in discovery
```

Local tokens use `X-API-Token`; cloud tokens use `Authorization: Bearer`.
Redirects are disabled and credentials are kept separate. Token creation or
revocation is not automatic. Supply only addresses for the same trusted device;
mDNS and local HTTP are not a cryptographic device identity check.

Explicit local alternatives work without extra dependencies. Optional discovery
uses the firmware's actual HTTP service registration, not a proprietary service:
`busybar-<USB MAC>._http._tcp.local.` on port 80.

```bash
uv sync --extra discovery
uv run python -m busybar discover --timeout 3
```

Use the returned bare `device_id` with `discover = true`. Discovery scans are
short, close their resources, and refresh no more than once per minute. The
client tries at most four local addresses per operation, then the configured
cloud route for supported operations. A working fallback remains preferred
between recovery probes. Missing discovery support or a failed scan leaves
explicit hosts usable; the CLI distinguishes an unavailable scan from a
successful scan that found no devices.

HTTP rejections (including authentication errors and a higher-priority canvas)
do not trigger failover. Reads and display updates can use bounded fallback.
Audio and timer starts are not replayed after an uncertain send/read failure;
only a definite connection timeout permits another route. Calendar chirps are
attempted once per event edge, and `auto_busy` requires a positively observed
nested `NOT_STARTED` snapshot rather than treating unavailable state as idle.

### Platform examples

- [Home Assistant](examples/home_assistant/README.md): built-in REST sensors
  for the nested BUSY snapshot and an expiring notification command with a
  secret placeholder. Notices stay below urgent calendar and BUSY-session
  priority and expire within 1–60 seconds. This is optional YAML, not a custom
  integration or an automatic HA installation.
- [On-device JavaScript](examples/javascript/README.md): a finite 30-second
  health demo using fetch, timers, and one persisted run counter. Scripts can
  be uploaded into app asset subdirectories with `upload_asset(app,
  "scripts/main.js", data)`. The firmware runner is experimental; this example
  does not replace the host integrations or install persistent autostart.

Protocol references: [firmware 1.2.3 release](https://github.com/busy-app/busybar-firmware/releases/tag/1.2.3),
[display API](https://github.com/busy-app/busybar-firmware/blob/2cd7ec8abf8479ba3398241e99d291ec24f2a96f/applications/services/web_server/openapi/assets.yaml),
[HTTP service registration](https://github.com/busy-app/busybar-firmware/blob/2cd7ec8abf8479ba3398241e99d291ec24f2a96f/applications/services/web_server/web_server.c).

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

**Overlay dwell/rotation.** `ci_status`'s overlay-tier frames (failure/stuck/quiet-green frames, the running badge, and the GraphQL and REST quota gauges) each draw for one `OVERLAY_DWELL_SECONDS` (10s) dwell slot, then stay silent for at least one more dwell period before redrawing — giving `calendar_countdown`'s own ambient-tier redraws (also tuned to a 10s cadence) a real chance to land in the resulting gap. Because eviction is one-way, the two integrations trade the panel back and forth rather than alternating cleanly; see each integration's README for the measured recovery rates.

**No alert takeover.** `ci_status` no longer draws at `PRIORITY_ALERT`; failure and stuck-queue frames now rotate at `PRIORITY_OVERLAY` (21) alongside the running badge and quota gauges, under the calendar's ambient tiers. As an upcoming calendar event gets closer, `calendar_countdown` climbs from `PRIORITY_AMBIENT` (20) through `PRIORITY_AMBIENT_RAISED` (25, inside `approach_minutes`) to `PRIORITY_AMBIENT_URGENT` (65, inside `notice_minutes`/`warn_minutes`), which already sits strictly above the overlay tier — so an imminent event naturally outranks a CI failure, and the failure frame alternates with the calendar's own redraws rather than camping the panel.

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
WebSocket will not get a cloud fallback for it. Selective deletion, asset
uploads, capability probes, and the diagnostic screen are also local-only.
Ordinary `draw`, `clear`, `status`, and `get_busy` calls retain cloud support;
new bitmap/layer fields are omitted from cloud drawings. Bitmap-only frames
require verified modern local firmware and never fall back to an empty cloud
draw. `set_busy_simple` and `play_audio` can use cloud directly, but an uncertain
local send is not replayed through the relay.

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
