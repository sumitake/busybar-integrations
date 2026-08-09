"""Shared display priority ladder and dwell/redraw contracts for every
busybar integration.

## Two firmware facts, established empirically (not from the device's own
## OpenAPI documentation, which is wrong about the first one) during the
## v1.5 running-CI badge work. See the spec doc's "Probe findings" section
## (`docs/superpowers/specs/2026-08-03-calendar-ci-integrations-design.md`,
## v1.5) for the full probe transcripts.

1. **Equal priority from a different `application_name` is REJECTED, not
   an override.** The OpenAPI doc claims "equal-priority requests from a
   different application_name override whatever is on screen" -- probed
   directly against a live app at priority 20 and found every equal- or
   lower-priority draw from a different app_name returns `409 {"error":
   "Not drawn due to low priority"}`. Only a STRICTLY GREATER priority
   succeeds. Any two tiers that need to take turns on screen must be on
   different priority numbers, not the same one.

2. **Occluded elements are EVICTED, not restored.** When a higher-priority
   app's elements expire (their own `timeout`) or are explicitly cleared,
   a lower-priority app's still-live elements do NOT reappear -- the panel
   goes black and stays black until *some* app performs a fresh draw.
   Probed for both the timeout-expiry and explicit-clear cases; both
   evict. There is no cross-process coordination between integrations (each
   is an independent poller), so an ambient app can only "win back" the
   screen by drawing again on its own schedule and getting lucky with the
   timing -- it is never handed the screen back automatically.

## The ladder

Every integration draws under one of these tiers. Pick the tier that
matches your update pattern; don't invent a new priority number.
"""

PRIORITY_AMBIENT = 20
"""Persistent baseline apps (e.g. calendar_countdown). Contract: redraw at
least every `AMBIENT_REDRAW_SECONDS` seconds, with each element's `timeout`
set via `ambient_timeout(poll_seconds)` (1.5x the poll interval, so a dead
poller's display self-clears rather than sticking). Must tolerate being
evicted at any time by a higher-priority draw (fact 2 above) -- there is no
"resume where I left off"; the next scheduled redraw is the only way back
on screen. `AMBIENT_REDRAW_SECONDS` exists specifically so an overlay
tier's dwell gaps (see PRIORITY_OVERLAY) are short enough for an ambient
app to realistically land one of its redraws inside them.

Tuning history (v1.5): started at 15s against a 10s overlay dwell gap;
on-device re-measurement (130s window, 6 dwell cycles) showed the ambient
app only recovering 2 of 6 gaps -- dark gaps were still ~10s in the other
4, exceeding the "~5s" target. Dropped to 10s (matching the dwell gap
exactly, so an ambient redraw firing anywhere in the gap window has a much
better chance of landing inside it) and re-measured; see the spec doc's
v1.5 section for both rounds' verbatim results.
"""
AMBIENT_REDRAW_SECONDS = 10

PRIORITY_FILLER = 5
"""Decorative screen-filler (e.g. nyan_filler). Strictly below PRIORITY_AMBIENT
(20) AND below the firmware's built-in-app tier (10), but above the empty/stub
screen (0). Verified on-device (spec 2026-08-06 §5b, SPK-3): the panel's
black/resting state -- true idle AND the CI overlay's silence gap -- rests at
priority 0, so a priority-5 draw fills those gaps; a built-in app at priority 10
outranks it, so the filler never overrides the clock/desktop. Every other tier
(ambient 20, overlay 21, raised 25, alert 60, urgent 65, session 90) preempts
it. Draw with loop=true and re-assert every poll: a same-element redraw
continues the on-device loop (SPK-2), so unconditional per-poll redraw does not
stutter.
"""


def ambient_timeout(poll_seconds: float) -> int:
    """Element timeout (seconds) for an ambient-tier draw at the given poll
    interval: 1.5x poll, floored to an int (matches the existing
    calendar_countdown convention)."""
    return int(poll_seconds * 1.5)


PRIORITY_OVERLAY = 21
"""Short-dwell time-shared overlays (e.g. the running-CI badge). Must be
strictly greater than PRIORITY_AMBIENT (fact 1 above) -- equal priority
against a different app_name is rejected, not a hand-off. Contract: draw
with element `timeout` = `OVERLAY_DWELL_SECONDS`, then stay silent (no
draw, no clear) for at least one more dwell period before redrawing again,
so an ambient app's own redraw has a real chance to land in the gap (fact
2 above means the ambient app is never automatically restored -- it can
only reclaim the screen with its own fresh draw). Use `overlay_gap_elapsed`
to decide whether enough silence has passed.
"""
OVERLAY_DWELL_SECONDS = 10


def overlay_gap_elapsed(last_dwell_end, now) -> float:
    """Seconds elapsed since an overlay's last dwell ended. `last_dwell_end`
    is `None` (never drawn yet) treated as infinitely long ago, so the
    first draw is never gated. Callers redraw when this returns
    `>= OVERLAY_DWELL_SECONDS`.
    """
    if last_dwell_end is None:
        return float("inf")
    return (now - last_dwell_end).total_seconds()


PRIORITY_AMBIENT_RAISED = 25
"""An ambient app carrying near-term (but not yet imminent) user-critical
information may draw here instead of PRIORITY_AMBIENT (v1.5.2). Strictly
above PRIORITY_OVERLAY (21) -- so a raised-tier ambient draw can no longer
be silently interrupted by an overlay-tier dwell rotation (e.g. the
running-CI badge/quota frames) -- and strictly below PRIORITY_ALERT (60)
-- were anything to draw at that tier, it would still win over a
merely-approaching event, though as of v1.7 nothing in this repo does
(see PRIORITY_ALERT's own docstring). This tier
exists for the "approach" window: calendar_countdown uses it once an
event is within `approach_minutes` of starting but still outside its
`notice_minutes` window (see calendar_countdown.logic.select_priority).
Overlay and alert tiers must never draw here -- this is an ambient-only
elevation, not a general-purpose "important overlay" priority; an overlay
frame that wants to preempt alerts belongs at PRIORITY_AMBIENT_URGENT or
higher only if it is itself carrying ambient, not overlay, semantics
(none currently do).
"""

PRIORITY_ALERT = 60
"""Urgent, preempting states. Always wins over PRIORITY_AMBIENT,
PRIORITY_OVERLAY, and PRIORITY_AMBIENT_RAISED by virtue of being a
strictly higher number (fact 1 above) -- no dwell/silence contract; draw
immediately and keep redrawing every poll while the condition holds.

No integration in this repo currently draws here -- ci_status originally
drew its failure/stuck badges at this tier (through v1.6) but moved them
down to PRIORITY_OVERLAY (21) in v1.7, joining the calendar's calm
dwell/rotation model instead of unconditionally preempting it. This tier
remains defined as the ladder's alert slot for reference, and for any
future integration whose update pattern genuinely warrants an
unconditional preempt.
"""

PRIORITY_AMBIENT_URGENT = 65
"""An ambient app carrying IMMINENT user-critical information may draw
here instead of PRIORITY_AMBIENT (v1.5.2) -- strictly above
PRIORITY_ALERT (60), so it would preempt even a genuine, currently-active
draw at that tier were anything drawing there (fact 2 means that draw's
elements are evicted, not merely occluded-and-later-restored -- see the
eviction/409 interplay in the spec doc's v1.5.2 section for why this was
designed to be safe: a no-dwell-contract app at PRIORITY_ALERT keeps
trying to redraw every poll, gets a `409` REJECTED response while this
tier holds the screen, treats that as expected and silent, and
re-asserts itself the moment this tier drops back down -- no
cross-process coordination needed). Strictly below PRIORITY_SESSION (90)
-- a real BUSY/CUSTOM work session still wins.

This tier was originally added to close an operator-reported UX gap: at
the time (v1.5.2), ci_status's persistent CI failure alert -- then drawn
at PRIORITY_ALERT (60) -- was permanently evicting the calendar, hiding
imminent events with no way for the calendar to ever reclaim the screen
(an ambient app has no dwell/silence contract of its own to fall back on
the way the overlay tier does). As of v1.7, ci_status no longer draws at
PRIORITY_ALERT at all -- its failure/stuck frames moved down to
PRIORITY_OVERLAY (21), where they take turns in the same calm dwell/
rotation as everything else -- so this specific gap no longer arises in
practice. The tier itself, and calendar_countdown's use of it, are
unchanged: calendar_countdown elevates here once an event enters its
`notice_minutes` window and stays here through `warn_minutes`, reverting
to PRIORITY_AMBIENT once the event starts (see
calendar_countdown.logic.select_priority) -- deliberately NOT while
merely in_progress, since once a meeting has started you already know
about it; the elevation exists to catch your attention BEFORE it starts.

**The LED is ASSUMED to be the session-safe channel -- unverified,
requires operator observation.** A BUSY/CUSTOM session at
PRIORITY_SESSION (90) still outranks this tier for the *panel*, so an
urgent-ambient draw's `elements` can be evicted the same way an alert's
can. The device's own API schema describes `led_notification_color` as a
separate field from the drawn elements, and probing what's actually
checkable from this codebase (the 409 rejection response body, whether
any status endpoint exposes current LED state) turned up nothing that
either confirms or refutes whether it survives the same priority
eviction that evicts the elements -- there is no LED-state-observable
endpoint anywhere in the device's API, and this claim can only be
settled by a human actually watching the physical LED during a live
session test. Until that observation happens, treat "the LED gets
through a session" as a design assumption this codebase acts on (see
calendar_countdown, which sets the LED during its final-minute window --
`imminent_minutes` -- specifically because a session might otherwise
hide it), not a verified fact. See calendar_countdown's README for a
short recipe to verify this on the actual hardware.
"""

PRIORITY_SESSION = 90
"""Reference only -- the firmware's own BUSY/CUSTOM work-session tier.
No integration in this repo draws at this priority; it's documented here
so the full ladder (including the firmware-owned ceiling) is visible in
one place.
"""
