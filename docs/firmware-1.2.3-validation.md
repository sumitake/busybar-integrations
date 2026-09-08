# Firmware 1.2.3 qualification

Qualification date: 2026-09-08. Source baseline: `097e1bb`.
Device: firmware **1.2.3**, API **27.5.0**, build **2026-09-03**, firmware
commit `2cd7ec8abf8479ba3398241e99d291ec24f2a96f`.

## Automated checks

- Existing baseline: 374 passing tests.
- Implementation: 433 passing tests, including existing calendar/CI/Nyan
  behavior and new transport, discovery, presentation and diagnostic cases.
- Source distribution and wheel built successfully with `uv build`.
- Python compilation and `git diff --check` passed.
- Optional discovery dependency installed and exercised; ordinary client
  operation does not require it.
- Home Assistant YAML parsed with a `!secret` placeholder constructor. Jinja
  rendering checked malformed/unknown/valid snapshots, quoted/multiline
  notification text, and TTL inputs `0`, negative, malformed, normal and
  oversized. The notice is bounded to 1–60 seconds.

Tests cover local/cloud credential separation, redirect suppression, no
failover on HTTP rejection, no replay of uncertain audio/timer requests,
priority step-down, selective-delete fallback, expired/preempted elements,
bitmap-only local drawing, legacy/cloud cosmetic fallback, unknown BUSY
state, discovery cleanup/failure reporting, and screen conversion.

## Live device checks

| Capability | Positive observation |
|---|---|
| API and firmware detection | Local diagnostic report returned 1.2.3 / 27.5.0 with `complete: true`. |
| Explicit z-order | Overlapping green/red rectangles rendered the higher `z_index` layer despite reverse payload order. Pixel readback was `(0, 255, 0)`. |
| Selective cleanup | Deleting only the green rectangle exposed the remaining red rectangle: `(255, 0, 0)`. |
| Ownership guard | A different app name could not delete the remaining element; the red pixel remained. |
| Inline XPM2 icon | The fixed CI icon rendered; its expected white pixel read `(255, 255, 255)`. |
| Cleanup and BUSY state | Temporary probes used unique ownership, priority 80, five-second TTLs and scoped cleanup. Cleanup succeeded; BUSY remained `NOT_STARTED` before and after. |
| Discovery | The bar advertised one persistent USB-MAC-derived HTTP service with both USB and Wi-Fi IPv4 addresses. |
| Local route recovery | An unavailable primary address fell back to the explicit Wi-Fi route; `/api/transport` positively reported `wifi`. |
| Screen capture | Base64 BGR24 response decoded into a valid standard 72×16 BMP. |
| Asset subdirectory | `scripts/main.js` uploaded and exact file bytes matched an HTTP readback. |
| JavaScript runtime | USB-network TCP CLI ran the demo: three successful API-version polls, a 30-second stop message, and return to the shell prompt. |
| JavaScript persistence | The demo's own localStorage file retained `run_count: "1"`; read back through the CLI. |

No BUSY timer was started, no audio was played, and no authentication,
brightness, charging, Wi-Fi or Home Assistant configuration was changed by
these checks. The example JS file and its counter are the only retained demo
assets; it has no autostart and is no longer running.

## Corrections established during qualification

- Firmware advertises `busybar-<USB MAC>._http._tcp.local.` on port 80.
  The earlier `_busybar._tcp` assessment was incorrect.
- The API version field is `api_semver`.
- Selective DELETE uses `/api/display/draw`, with ownership in the query;
  the 1.2.3 body parser has an app-name shadowing bug.
- `/api/screen` claims `image/bmp` but actually sends base64 BGR24 pixels.
- The stock CLI is TCP port 23 over USB Ethernet, not a USB serial modem.
- The storage HTTP path buffer permits at most 63 characters. The demo's
  localStorage filename is longer; direct HTTP read returned 400 even though
  the file existed. CLI read positively confirmed its stored contents.

## Review and remaining activation boundary

Terra implemented and tested connectivity; Luna implemented diagnostics and
platform examples. Astra reviewed the combined code. Its concrete findings
were fixed: modern bitmap-only support without empty cloud fallback, honest
unavailable discovery reporting, and positive finite HA notification TTLs.
Primary review also corrected firmware wire contracts against source and live
responses and rejected a broker/transaction layer as unnecessary.

The first complete GitHub review batch on PR #22 (head `db05fce`, inventory
cutoff 2026-09-08 21:56 UTC) contained two actionable P2 findings. Both were
fixed together: explicit diagnostic `--host` now forces that sole local target,
and discovery may append newly found addresses during the current operation
without exceeding four total attempts or replaying an uncertain write. Tests
and live reads verified both corrections. Formal reviews, all inline threads,
issue comments and applicable check annotations were inventoried before the
patch; CodeRabbit's skipped review was not counted as approval.

The second complete GitHub batch (head `15acd5e`, inventory cutoff
2026-09-08 22:07 UTC) identified two further route-selection defects. The
primary recovery interval now starts at the actual primary attempt, so an
immediate one-shot operation uses the known-working fallback. A full four-route
configuration reserves its last attempt for an untried discovery candidate,
while preserving the fourth configured route when discovery has no candidate.
The preferred address uses the existing successful endpoint rather than a list
index. Astra reviewed this bounded design; no new retry service or state
machine was added. The four-attempt limit and uncertain-write stop remain.

A logical Gemini final repository advisory returned **PROCEED**. The earlier
follow-up design call was unavailable because of nested host sandbox failure;
that failed call was not counted as approval or replayed. A fresh final review
used approved host execution with native sandboxing preserved. The runtime
returned no native model identity, so the advisory is recorded as such rather
than claimed as independently attested model lineage.

This report qualifies the implementation and the bounded device probes. The
three long-running host integrations have not yet been switched to this branch.
Cloud and local-token behavior is covered by tests, not a new live credential
rotation. The HA example has not been loaded into a running HA instance. The
firmware JS runner remains experimental and this demo is not a replacement for
the host integrations.
