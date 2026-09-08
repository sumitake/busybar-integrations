# Calendar Countdown Integration

## What It Does

This integration polls your macOS calendar for upcoming events and displays a live countdown on the busybar device: a full-panel gradient background that itself signals urgency, an uppercase title row, a horizontal drain track, and large numerals (start time, or countdown) floating directly on the background — no card surfaces, for an airy, high-contrast look.

- **Signal background** — the whole panel is a top-to-bottom gradient whose colors shift with urgency: deep navy when the event is far off, warm orange approaching `notice_minutes`, red approaching `warn_minutes`, and teal while the event is in progress. Urgency reads at a glance before you even look at the countdown.
- **Title row** — the event title, uppercased, in a small font flush across the top. Long titles scroll; short ones sit static.
- **Drain track** — a thin horizontal line spanning the full width, edge to edge, below the title. It fills proportionally to time remaining within `progress_window_minutes` (default 60) and shrinks toward empty as the start time arrives. During an active event, the track drains against the event duration. A bright endpoint makes the remaining fraction easy to locate.
- **Time / Ends** — a large local `HH:MM` start time for an upcoming event, or a bold "ENDS" label once the event has begun. No card behind either — both float directly on the gradient background.
- **Countdown** — a large minutes-granular countdown, same size as the time/ends numeral (they always change together): `"54m"` under an hour, `"1h05m"` at/above an hour, falling back to hour-only (`"9h"`) whenever the combined form would run too wide for the space available — which font-width measurement shows can happen even for some single-digit-hour values, not just at 10+ hours. Counts to the event start while upcoming, or to its end once in progress; re-rendered each poll rather than ticking natively on-device, so it updates on the same cadence as the rest of the display (`poll_seconds`).
- **Four states** — `normal`, `notice` (within `notice_minutes` of start), `warning` (within `warn_minutes` of start), and `in_progress`, each with its own background gradient, title color, drain-track gradient, divider color, and digit color. See the [current design and device captures](../../docs/display-design.md) for the visual flow.

The integration looks ahead 12 hours by default and draws at the ambient tier (`busybar.display.PRIORITY_AMBIENT`, priority 20) on the display. If an active BUSY session exists on the device, the calendar event display is suppressed in favor of the busy state (priority 90). If the `ci_status` integration is also running with `show_running` and/or `show_quota` enabled, the calendar and the overlay rotation (running badge, GraphQL/REST quota frames) trade the screen back and forth for as long as a CI run is active — see `ci_status`'s README ("Display Priority Tiers" / alternation rhythm) for the measured numbers; the panel still goes fully dark for a few seconds in most cycles because the firmware never restores an occluded element on its own (see "Display Priority Tiers" below), but at the tuned 10s ambient poll the calendar now recovers the screen in roughly 3 of every 4 overlay dwell gaps rather than effectively never.

## Requirements

- **macOS 14+ (Sonoma)** with Calendar app enabled
- **Calendar permission granted** in System Settings > Privacy & Security > Calendars (must be answered once in a foreground terminal run before automating)
- **Device reachable** on your LAN (default `10.0.4.20` over USB-Ethernet; configurable for Wi-Fi)
- **Python 3.12+** and `uv` package manager

## Setup

### 1. Grant Calendar Permission

From the repository root, run the integration once to answer the calendar-permission prompt:

```bash
cd integrations
uv run python -m calendar_countdown.main --once --dry-run
```

This foreground run is required before automating — the permission prompt appears in the terminal and cannot be answered by a background agent. After the permission dialog, press ^C to exit.

### 2. Discover Calendar Names

To limit polling to specific calendars, first discover the exact names available on your system:

```bash
uv run python -m calendar_countdown.main --list-calendars
```

This prints the calendar names organized by account. Use these exact names in the config.

### 3. Configure

Copy the example config to your repository root:

```bash
cp config.example.toml config.toml
```

Edit `config.toml` and configure the `[calendar_countdown]` section:

```toml
[calendar_countdown]
poll_seconds = 10              # how often to check the calendar (default: 10 -- ambient-tier
                                # redraw cadence; see "Display Priority Tiers" below)
lookahead_hours = 12           # how far ahead to scan (default: 12)
warn_minutes = 5               # bar/countdown turn red within N minutes of start (default: 5)
notice_minutes = 15            # bar/countdown turn amber within N minutes of start (default: 15)
progress_window_minutes = 60   # drain track empties from full over this many minutes (default: 60)
include_all_day = false        # include all-day events (default: false)
auto_busy = false              # auto-mark as BUSY during events (default: false)
# calendars = ["Work"]         # optional: list calendar names to limit scope (discover with --list-calendars)
approach_minutes = 30          # v1.5.2 escalation ladder -- see "Escalation ladder" below
imminent_minutes = 1           # LED blinks on every draw inside this window
chirp = true                   # one-time audio chirp exactly at event start; false disables audio
```

### 4. Test Live

Once config is in place, test the integration with a dry-run:

```bash
uv run python -m calendar_countdown.main --once --dry-run
```

Verify that the output shows your next upcoming event with the correct countdown.

## Config Reference

| Key | Type | Default | Purpose |
|---|---|---|---|
| `poll_seconds` | integer | 10 | Polling interval in seconds (ambient-tier redraw cadence; raise it if 10s polling is more than your calendar setup needs, but see "Display Priority Tiers" below for the alternation tradeoff) |
| `lookahead_hours` | integer | 12 | Hours into the future to scan for events |
| `warn_minutes` | integer | 5 | Bar/countdown turn red when within N minutes of event start |
| `notice_minutes` | integer | 15 | Bar/countdown turn amber when within N minutes of event start |
| `progress_window_minutes` | integer | 60 | Drain track empties from full width over this many minutes before the event start |
| `include_all_day` | boolean | false | Include all-day events in the display |
| `auto_busy` | boolean | false | Automatically mark device BUSY during event time |
| `calendars` | array of strings | (all) | List of calendar names to monitor. Discover available names with `uv run python -m calendar_countdown.main --list-calendars` |
| `approach_minutes` | integer | 30 | v1.5.2 escalation ladder: inside this window (and outside `notice_minutes`) the draw priority rises above the overlay tier. See "Escalation ladder" below. |
| `imminent_minutes` | integer | 1 | Inside this window (event not yet started), the LED blinks on every draw. |
| `chirp` | boolean | true | Play a one-time audio chirp exactly at event start (T-0). Set false to disable audio entirely. |
| `escalation_icons` | boolean | true | Show animated calendar icons at the warn (5-min, `calendar_event_16x16`) and imminent (1-min, `calendar_reminder_16x16`) stages. Set false for text-only display. |
| `start_animation` | string | `"meeting_72x16"` | Full-panel stock animation for the first `start_window_seconds` after an event begins. Set to `""` to disable. The animation's baked word (e.g., "MEETING") displays for every event; no per-event-type mapping. |
| `start_window_seconds` | integer | 60 | Duration the start takeover animation holds after an event begins (and at urgent priority, aligned with the T-0 chirp). |

## Autostart

### Install LaunchAgent

From the repository root, run these commands to install the calendar integration as a background service that starts at login:

```bash
cd integrations/calendar_countdown
mkdir -p ~/Library/Logs/busybar
sed -e "s|__REPO__|$(git rev-parse --show-toplevel)|" -e "s|__UV__|$(command -v uv)|" -e "s|__HOME__|$HOME|" \
  com.busybar.calendar-countdown.plist > ~/Library/LaunchAgents/com.busybar.calendar-countdown.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.busybar.calendar-countdown.plist
```

The agent will start automatically at your next login and run continuously, polling your calendar at the interval specified in `config.toml`. The agent sets PYTHONPATH to the repo's src/ directory so the busybar package resolves even without a healthy editable install.

### Uninstall LaunchAgent

To stop the service and remove it from autostart:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.busybar.calendar-countdown.plist
rm ~/Library/LaunchAgents/com.busybar.calendar-countdown.plist
```

## Logs

Stdout and stderr are redirected to `~/Library/Logs/busybar/calendar.log`. View recent activity with:

```bash
tail -f ~/Library/Logs/busybar/calendar.log
```

At the default 10s poll cadence, most polls redraw the same event unchanged. To avoid multiplying the log's line rate versus the old 60s cadence sixfold, draw summaries are logged at `INFO` only when the summary actually changes or every 10 minutes (a heartbeat line), and at `DEBUG` otherwise -- `DEBUG` isn't emitted by the default log level, so routine unchanged polls don't appear in `calendar.log` at all. Run with `python -m logging` verbosity raised, or check the process's own stdout in the foreground, if you need to see every single poll.

## Display Priority Tiers

This integration draws at the **ambient** tier (`busybar.display.PRIORITY_AMBIENT`, priority 20) -- see `src/busybar/display.py` for the full shared priority ladder used across every busybar integration, and the design spec's "Display tier framework" section for the two firmware facts (measured, not assumed) that shape it: a different app can only preempt this one with a strictly higher priority (equal priority is rejected outright), and once preempted, this app's elements are evicted rather than restored -- the calendar only gets the screen back via its own next scheduled redraw, never automatically. The 10s default poll interval exists specifically so those redraws happen often enough to reliably interleave with the `ci_status` overlay tier (priority 21, ~10s dwell gaps, now rotating through the running badge plus two quota frames -- see `ci_status`'s README) during an active CI run. On-device re-measurement across three poll settings, sampled every 2s for ~130s against the live agent:

| Calendar `poll_seconds` | Dwell cycles recovered | Sample split (BADGE / BLANK / OTHER=calendar) |
|---|---|---|
| 60 (pre-v1.5 baseline) | 0 of 6 | 33 / 31 / 0 |
| 15 | 2 of 6 | 34 / 26 / 5 |
| 10 (current default) | 4 of 6 | 35 / 20 / 10 |

Matching the poll to the dwell gap exactly (10s) did not eliminate the dark gaps entirely -- the two timers still run independently with no cross-process coordination, so recovery timing within a gap varies (observed roughly 2-8s into a given 10s gap) and 2 of the 6 sampled cycles still showed no recovery at all -- but it took the calendar from "never recovers" to "recovers in most cycles." A separate on-device run exercising the full 3-frame overlay rotation (running badge -> GraphQL quota -> REST quota -> repeat) at the same 10s dwell showed the same pattern: the calendar reclaimed 3 of the 4 gap windows sampled. If your setup still shows the panel dark for more than a few seconds at a stretch, that is consistent with this measurement, not a bug; lowering `poll_seconds` further has diminishing returns since the elements' own render/transmit latency puts a floor on how tightly the two timers can align.

## Escalation ladder

An operator-reported UX gap: a persistent CI failure alert (`ci_status`, then drawn at `PRIORITY_ALERT`) permanently evicted the calendar, hiding an imminent event with no way for the calendar to ever reclaim the screen -- the ambient tier has no dwell/silence contract of its own the way the overlay tier does. v1.5.2's fix is a state-dependent draw priority: as an upcoming event gets closer, the calendar climbs `busybar/display.py`'s shared priority ladder so it can no longer be silently buried by the overlay-tier rotation. (As of the calm-rotation change, `ci_status` no longer draws at `PRIORITY_ALERT` at all -- its failure/stuck frames now rotate at the overlay tier (21) alongside the running badge and quota gauges, so the calendar's climb above that tier is what keeps an imminent event visible.)

**Assumed ordering.** The four thresholds are assumed to nest:
`approach_minutes > notice_minutes > warn_minutes >= imminent_minutes`.
This isn't enforced (nothing crashes if violated -- each threshold is
checked independently), but a violated ordering makes a tier
unreachable in practice (e.g. `approach_minutes <= notice_minutes` means
the "approach" priority tier is dead: anything inside it is already
inside `notice_minutes` too, which is checked first). `main()` logs a
one-time startup warning (`check_threshold_ordering`) if the configured
values violate this ordering -- check `calendar.log` after changing any
of these four keys.

| Window | Priority | Palette | LED | Notes |
|---|---|---|---|---|
| `normal` (beyond `approach_minutes`) | `PRIORITY_AMBIENT` (20) | normal | off | Baseline, unchanged from before v1.5.2. |
| `approach` (within `approach_minutes`, outside `notice_minutes`) | `PRIORITY_AMBIENT_RAISED` (25) | normal (unchanged) | off | Strictly above the overlay tier (21) -- the countdown can no longer be silently interrupted by the CI rotation (failure/stuck/running/quota frames all draw there). Purely a priority change; nothing looks different on screen. |
| `notice` (within `notice_minutes`) | `PRIORITY_AMBIENT_URGENT` (65) | amber | off | Above the overlay tier (21) where `ci_status`'s failure/stuck frames now draw, so an imminent event is never buried by a CI failure (and above the now-unused `PRIORITY_ALERT` (60) slot). |
| `warn` (within `warn_minutes`) | `PRIORITY_AMBIENT_URGENT` (65) | red | off | Same priority as `notice` -- they differ visually and (below) in LED, not in urgency toward the display arbitration. |
| *imminent window* (within `imminent_minutes`, part of `warn`) | `PRIORITY_AMBIENT_URGENT` (65) | red (same as `warn`) | **on**, every draw | Not a separate priority tier -- `imminent_minutes` governs only the LED. See "Audio and LED" below. |
| `in_progress` | `PRIORITY_AMBIENT` (20) | teal | off | Deliberately NOT elevated -- once a meeting has started you already know about it (you're either in it or conspicuously not); the elevation exists to catch your attention *before* an event starts, not to keep fighting for the screen once it has. An alert regains the panel here exactly as it did before this feature existed. |

**The eviction/409 interplay.** `PRIORITY_AMBIENT_URGENT`'s draw succeeding while a `ci_status` alert is showing evicts that alert's elements outright (the firmware evicts, never restores -- see `src/busybar/display.py`'s fact 2). `ci_status` itself doesn't need to know or care: it keeps trying to redraw its alert every poll per its own no-dwell contract, gets a `409` (`DrawResult.REJECTED`) while the calendar holds the higher tier, treats that as expected and silent (nothing new here -- the same handling that already existed for the overlay tier's own dwell gaps), and re-asserts itself the instant the calendar drops back down to `PRIORITY_AMBIENT` -- typically at the calendar's *next* poll after the event starts or leaves its notice window, so the reappearance lands within one `poll_seconds` of the calendar itself, not instantly. In between, the calendar transiently owns the panel -- expected, not a bug, and needs no cross-process coordination.

## Audio and LED (final-minute window)

Independent of the priority ladder above, two more signals fire during the final `imminent_minutes` before an event starts (default: the last 1 minute):

- **LED** (`led_notification_color`) blinks on *every* draw from `imminent_minutes` before start until the event actually starts, then stops (no LED once `in_progress`) -- turned off via an explicit off value on the exact transition poll, not by silently omitting the field (see "Guaranteed LED-off" below). The LED is a separate field from the drawn elements in the device's own API schema, and this integration is built on the ASSUMPTION -- **not independently verified** -- that it survives a BUSY/CUSTOM session's (`PRIORITY_SESSION`, 90) eviction of the panel the way the elements themselves don't. There is no API endpoint that exposes current LED state, so this can only be confirmed by a human watching the physical LED; see "Verifying the LED assumption" below.
- **Chirp**: a short audio tone plays exactly once per event, at the moment it starts (T-0) -- not during the final-minute countdown itself. It uses a firmware-shipped **stock sound** (`shared/calendar_event_starts.snd`) -- no asset generation, upload, or repo-committed audio file is needed or used. Playback always uses whatever volume is currently configured on the device; this integration never reads or sets `/api/audio/volume`. Set `chirp = false` to disable audio entirely.

  **A note on that `.snd` extension.** The firmware build pipeline converts `.wav` source files to `.snd` at packaging time, so the *runtime* filename under a stock sound directory is never `.wav`, even though the source asset is -- this is invisible from the OpenAPI spec or the firmware source tree; the only way to find it is a live storage listing against the actual device. An earlier version of this feature shipped with the wrong (`.wav`) extension: `POST /api/audio/play` returned `200` for it every time (the endpoint queues playback behind a short amp holdoff and returns success *before* the actual file open, so a missing/wrong file at that point is logged device-side only and never reaches the HTTP response) -- so the chirp was **silently non-functional** end to end until an operator noticed no sound was playing. Every chirp attempt is now logged at `INFO` (`chirp played (<path>) -> <True/False>`) specifically so a repeat of this -- a "successful" `play_audio` call that produces no actual sound -- at least leaves a trace to correlate against what you actually hear, since a `200`/`True` response alone does not prove audibility. If you ever change `CHIRP_STOCK_PATH` to a different stock sound, verify the exact filename against `GET /api/storage/list` on the real device first, not the firmware source tree.

**Timing precision.** The main loop normally sleeps for a full `poll_seconds` between polls, but when an upcoming event's start is sooner than that, it sleeps exactly until that start instead -- so the poll that detects the transition (and fires the chirp) lands within about a second of the real start time, not up to a full `poll_seconds` late.

**Guaranteed LED-off.** Because there's no way to confirm whether omitting `led_notification_color` turns off a previously-lit LED or merely leaves it as-is (unverifiable through the API -- see above), this integration never relies on omission for an on->off transition. It tracks whether the LED is believed to be lit across polls and, on the poll where it should turn off, sends an explicit off value (`#00000000`) instead of dropping the field -- correct regardless of which behavior the firmware actually has. This applies even when the imminent event disappears entirely without ever starting (filtered out, or shorter than one poll interval): with nothing left to draw, a minimal 1x1 transparent placeholder element carries the explicit off value before the panel is cleared.

**Verifying the LED assumption.** To confirm the LED-during-a-session assumption on real hardware: with `notice_minutes`/`warn_minutes`/`imminent_minutes` set low enough to reach the imminent window quickly (or just wait for a real event to approach), start a BUSY/CUSTOM session on the device (the physical start button) while an event is inside its `imminent_minutes` window, and watch the LED. If it keeps blinking through the session, the assumption holds and no further action is needed. If it goes dark once the session starts, the assumption in `busybar/display.py`'s `PRIORITY_AMBIENT_URGENT` docstring is wrong and should be corrected (and the LED can no longer be relied on as a session-safe signal for this or any future integration).

**Once-per-event semantics.** The chirp fires on the transition edge only -- the poll where this *process* observes an event go from upcoming to started -- tracked in memory, keyed by `(start timestamp, title)`, not the start timestamp alone (two distinct events that happen to share the exact same start -- two all-day events both effectively at midnight, or two calendars firing something simultaneously -- are tracked independently, so chirping one never silently marks the other as already handled). It will not repeat on subsequent polls while the same event stays in progress. **Restart edge case**: this tracking is in-memory only, so a process restart during an event's final minute (or any time after it has already started) does not re-fire the chirp for that event -- the new process never observed it as "upcoming," so the edge is never detected. This is a deliberate tradeoff (documented, not a bug): the alternative (chirping on level-detection alone) would risk a spurious chirp on every restart during an active event.

## Animation Accents

This integration uses device stock animations to accent the calendar escalation and event-start moments. See the design spec (`docs/superpowers/specs/2026-08-06-animation-accents-design.md`) for the technical background and verification results.

### Pre-Start Icons (Warn and Imminent Stages)

When `escalation_icons` is true (default), animated calendar icons accompany the text-based countdown:

- **Warn stage** (within `warn_minutes` of start, e.g., 5 minutes): a `calendar_event_16x16` icon animates at the top-left corner. The event title is shown to the right of the icon, across a reserved 52-pixel title lane above the countdown; the large countdown numeral remains at its usual position. The start-time text (`HH:MM`) is dropped to make room. Palette is red, priority is `PRIORITY_AMBIENT_URGENT` (65).
- **Imminent stage** (within `imminent_minutes` of start, e.g., 1 minute): the animated icon switches to `calendar_reminder_16x16` (calendar + bell). The title stays visible; the lower row reads `IN <1m` during the final positive fraction of a minute. Palette remains red, priority unchanged (65).

Setting `escalation_icons = false` reverts both stages to text-only display: title, start-time (`HH:MM`), and countdown numeral, with no icons.

### Start Takeover (First Minute After Event Begins)

When an event begins and `start_animation` is configured (default `"meeting_72x16"`), the display transitions to a full-panel animated takeover for the first `start_window_seconds` (default 60 seconds):

- **Display**: the entire panel shows the configured stock animation looping continuously (e.g., `meeting_72x16` displays an animated word "MEETING" and occupies ~98% of the 72×16 panel).
- **Priority**: held at `PRIORITY_AMBIENT_URGENT` (65) for the entire window — matching the T-0 chirp's urgency, ensuring the alarm is not silently buried by other displays.
- **Timing**: aligned with the audio chirp at T-0 (event start). The animation self-loops between the integration's own redraws (every `poll_seconds`), so motion is smooth across the window.
- **After the window**: reverts to the normal in-progress "ENDS" display at `PRIORITY_AMBIENT` (20).
- **Disabled**: set `start_animation = ""` to skip the takeover entirely — in-progress will behave as before, showing the "ENDS" label from T-0 without an animated accent.

**Note on the baked word.** Stock animations carry a fixed visual word (e.g., "MEETING" in the default `meeting_72x16`). This animation displays the same word for every event start, regardless of event title, calendar, or type. No per-event-type mapping or dynamic text insertion is performed — it is a configurable static announcement.

### Stock Animation Paths

All animations are device stock assets referenced by `stock_path` (e.g., `"shared/calendar_event_16x16.anim"`, `"shared/calendar_reminder_16x16.anim"`, `"shared/meeting_72x16.anim"`). No custom animation assets are uploaded or bundled with this integration; the device firmware ships all referenced stock animations at `/ext/apps_assets/shared/animations/`. The `start_animation` value must be a valid device stock animation name — examples include `meeting_72x16`, `booked_72x16`, `wave_invitation_72x16`. Setting `start_animation = ""` disables the takeover. Use a valid name: an invalid or misspelled animation name can't be drawn, and the integration falls back to the normal in-progress ("ENDS") layout for the takeover window rather than leaving the panel dark — so a typo degrades to a plain live countdown, not a blank screen, and is logged (`start-takeover animation '<name>' not drawable; falling back ...`).

See the design spec (`docs/superpowers/specs/2026-08-06-animation-accents-design.md`, § 3) for the full list of verified stock animations and their dimensions.
