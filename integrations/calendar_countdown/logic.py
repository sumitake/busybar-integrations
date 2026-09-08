from dataclasses import dataclass
from datetime import datetime, timedelta

# v1.5.2 escalation ladder: the shared priority tiers (see busybar/display.py
# for the full ladder contract and the two firmware facts it's built on).
from busybar.display import PRIORITY_AMBIENT, PRIORITY_AMBIENT_RAISED, PRIORITY_AMBIENT_URGENT

PANEL_WIDTH = 72
PANEL_HEIGHT = 16

# --- "Signal" state palette --------------------------------------
#
# Four states, ordered least -> most urgent, with `in_progress` orthogonal to
# the other three (an event currently happening, regardless of how it got
# there). Every per-state table below is keyed by the same four strings so a
# lookup miss (typo, new state forgotten in one table) raises KeyError loudly
# instead of silently drawing the wrong color.
STATE_NORMAL = "normal"
STATE_NOTICE = "notice"
STATE_WARNING = "warning"
STATE_IN_PROGRESS = "in_progress"
STATES = (STATE_NORMAL, STATE_NOTICE, STATE_WARNING, STATE_IN_PROGRESS)

BG_GRADIENT = {
    STATE_NORMAL: ["#061326FF", "#01030AFF"],
    STATE_NOTICE: ["#241704FF", "#060401FF"],
    STATE_WARNING: ["#2B0818FF", "#060208FF"],
    STATE_IN_PROGRESS: ["#04231CFF", "#010604FF"],
}

TITLE_COLOR = {
    STATE_NORMAL: "#F2F7FFFF",
    STATE_NOTICE: "#FFF2CEFF",
    STATE_WARNING: "#FFF0F3FF",
    STATE_IN_PROGRESS: "#83FFF3FF",
}

# The drain track's groove is a single fixed color in every state; only the
# fill on top of it (TRACK_FILL_GRADIENT / TRACK_FILL_IN_PROGRESS) changes.
TRACK_COLOR = "#142033FF"

TRACK_FILL_GRADIENT = {
    STATE_NORMAL: ["#1597FFFF", "#63FFD4FF"],
    STATE_NOTICE: ["#FF9F1CFF", "#FFE66DFF"],
    STATE_WARNING: ["#FF416EFF", "#FFC070FF"],
}
TRACK_FILL_IN_PROGRESS = "#24D6C5FF"

# Fixed regardless of state -- only drawn for upcoming events. No card
# behind it as of v1.4 ("airy" refinement): per operator design principle,
# a card/panel surface is only acceptable with strong luminance contrast
# against both the background and its own text (near-black surface + bright
# text, or an inverse chip -- bright surface + near-black text, as the CI
# failure badge does). The v1.3.1 cards (#062238/#062A22, etc.) were
# mid-luminance dim fills that read as glow-mud on emissive LEDs, so v1.4
# removes cards entirely rather than trying to re-tune their luminance.
TIME_TEXT_COLOR = "#B4CAE6FF"

# Fixed -- only drawn for the in-progress state (replaces time + ends).
ENDS_TEXT_COLOR = "#8CFFF4FF"
ENDS_TEXT = "ENDS"

DIVIDER_COLOR = {
    STATE_NORMAL: "#284469FF",
    STATE_NOTICE: "#C37A0CFF",
    STATE_WARNING: "#E02A4CFF",
    STATE_IN_PROGRESS: "#178C88FF",
}

DIGIT_COLOR = {
    STATE_NORMAL: "#5AF0FFFF",
    STATE_NOTICE: "#FFC247FF",
    STATE_WARNING: "#FF6987FF",
    STATE_IN_PROGRESS: "#64FFEAFF",
}

# --- geometry -----------------------------------------------------------------
#
# Firmware ink-offset gotcha (found via on-device frame captures after the
# first v1.3 pass mashed the layout): every font renders its ink ~2px below
# the element's `y`, uniformly across every font tested so far (tiny/small/
# large/extra_large/bold). The `_Y` constants below are already
# offset-corrected so ink lands where the name/row-comment implies -- do not
# "fix" them back to the visually-obvious value without re-verifying ink
# rows on-device. Rectangles have no such offset; a rectangle's ink starts
# exactly at its `y`.
#
# v1.4 "airy" refinement: more breathing room, no card surfaces (removed
# per the operator's luminance-contrast design principle -- see the
# TIME_TEXT_COLOR comment above), both numerals drop from extra_large (10px
# bold) to `large` (9px) so they stay equal-sized (never independently
# resized -- another operator design principle). Freeing the card rows
# opened up row 5 as a deliberate blank buffer row between the title (ink
# 0-4) and the track, which moved from y=5 to y=6.

TITLE_X = 2
TITLE_Y = -2   # ink rows 0-4 (small font, 5px); top-flush is deliberate (ribbon)
TITLE_WIDTH = 68   # 2px margin each side (x=2..70 of a 72px panel)

# Row 5 is a deliberate blank buffer -- nothing may ink it. Verified
# on-device (see the spec doc's v1.4 section and the report).
TRACK_Y = 6   # was 5 in v1.3.1; full width edge-to-edge is deliberate (horizon line)
TRACK_HEIGHT = 1

TIME_X = 2
TIME_Y = 5     # ink rows 7-15 (large font, 9px); bottom-flush at row 15 is deliberate

ENDS_X = 2
ENDS_Y = 6     # ink rows 8-14 (bold font, 7px); numerically shares y with track,
               # but text's +2 ink offset means ink starts at row 8, 1px clear of
               # track's ink at row 6 -- verify no contact on-device, not just by
               # the numbers matching.

DIVIDER_X = 34
DIVIDER_Y = 8   # ink rows 8-14 (rectangle, no offset); 1px clear of the track
DIVIDER_WIDTH = 2
DIVIDER_HEIGHT = 7   # ... above (row 7 blank) and the panel's bottom edge below (row 15 blank)

# The native `countdown` element's digits render only 5px tall in every mode
# (MM:SS and H:MM:SS both) -- confirmed with fresh-id on-device probes during
# the v1.3.1 correction. Large numerals are the operator's core requirement,
# so the native countdown element is unusable here; a plain `text` element
# replaces it (id "cd_text" -- not "countdown", since its element `type`
# differs and the draw endpoint's id-reuse-type-change quirk can serve stale
# pixel data across a type change), formatted by `_format_countdown` below.
# No `align` field: align re-anchors screen-relative, not layout-relative,
# and was implicated in the v1.3 mash. Fixed left position instead.
CD_TEXT_X = 39
CD_TEXT_Y = 5   # ink rows 7-15 (large font, 9px) -- same offset pattern as `time`,
                # since both are the same font size per the "numerals track together" rule

# Available width for cd_text's rendered string, right up to the panel's
# edge (there's nothing to its right in v1.4 -- no card, no divider on that
# side).
CD_TEXT_MAX_WIDTH = PANEL_WIDTH - CD_TEXT_X   # 33px

# Per-glyph advance width in px, `large` font (9px), measured on-device by
# differencing (e.g. width("001") - width("00") = the '1' glyph's advance).
# This font is NOT fixed-width: '1' measures 5px vs 7px for every other
# digit -- a ~30% difference that a single flat "px per char" constant
# cannot represent accurately enough to safely decide whether a given
# countdown string fits (see _format_countdown's docstring for why this
# matters here specifically). Values are rounded up from the raw
# differencing measurements (which came out ~1px lower than an additive
# reconstruction predicts, consistently, likely a one-time string-start
# left-bearing the differencing technique double-counts) -- i.e. this table
# deliberately over-estimates width slightly, the safe direction for a
# fits/doesn't-fit decision.
GLYPH_ADVANCE_PX = {
    "0": 7, "1": 5, "2": 7, "3": 7, "4": 7, "5": 7, "6": 7, "7": 7, "8": 7, "9": 7,
    "h": 6, "m": 8,
    # v1.5.1: extended for ci_status's running-badge ETA label feature --
    # "~" (the remaining-estimate prefix, e.g. "~4m") plus every letter
    # needed to also measure "remain"/"left" through this same table (see
    # ci_status/logic.py's _eta_label docstring for why the label -- which
    # actually renders in the *small* font -- is deliberately measured via
    # this *large*-font table anyway: on-device calibration found it's a
    # sizeable but safe overestimate of the label's real small-font width).
    # Same on-device successive-prefix differencing methodology as the
    # digits/h/m above; self-validated by re-measuring "0" via this
    # technique and getting the same 7px already in this table.
    "~": 7, "r": 5, "a": 6, "e": 6, "i": 4, "n": 6, "l": 4, "f": 5, "t": 5,
}


def _text_width_px(s: str) -> int:
    return sum(GLYPH_ADVANCE_PX[ch] for ch in s)


# Approximate px-per-character for the "small" bitmap font, used only to
# decide whether a title needs to scroll. Deliberately conservative (slightly
# wide) so borderline titles scroll rather than clip; carried over from v1.1
# where it was refined against on-device renders of the same font.
SMALL_FONT_CHAR_PX = 5
SCROLL_RATE = 2000
SCROLL_DELAY_MS = 800

# --- v1.6 stock-animation accents: escalation icons + start-takeover -------
#
# The 16x16 warning animation owns x=0..15. The title and track use
# x=18..71 throughout warning/imminent; the lower row keeps IN + countdown.
ICON_EVENT = "calendar_event_16x16"
ICON_REMINDER = "calendar_reminder_16x16"
ICON_X, ICON_Y = 0, 0
ICON_TITLE_X = 18          # title shifts right of the 16x16 icon (icon occupies x=0..15)
CAL_ICON_ID = "cal_icon"
START_ANIM_ID = "cal_start_anim"


@dataclass
class CalEvent:
    title: str
    start: datetime
    end: datetime
    all_day: bool


def select_next_event(events: list[CalEvent], now: datetime,
                      lookahead_hours: int, include_all_day: bool) -> CalEvent | None:
    horizon = now + timedelta(hours=lookahead_hours)
    upcoming = [e for e in events
                if e.start >= now and e.start <= horizon
                and (include_all_day or not e.all_day)]
    return min(upcoming, key=lambda e: e.start) if upcoming else None


def select_active_event(events: list[CalEvent], now: datetime) -> CalEvent | None:
    active = [e for e in events if e.start <= now < e.end and not e.all_day]
    return min(active, key=lambda e: e.end) if active else None


def ascii_safe(s: str) -> str:
    cleaned = "".join(ch for ch in s if 0x20 <= ord(ch) <= 0x7E)
    cleaned = " ".join(cleaned.split())  # collapse runs left by stripped chars
    return cleaned or "event"


def _track_fill_width(minutes_left: float, window_minutes: int) -> int:
    """Drain-track fill width in px, anchored to the left (the filled portion
    shrinks from the right as the event approaches -- same "drains as it
    approaches" metaphor as the v1.1 vertical progress bar, ported to a
    horizontal track). >= window -> full width. <= 0 -> minimum visible
    sliver (1px). A non-positive window (misconfiguration) defensively reads
    as "always full" rather than raising or dividing by zero.
    """
    if window_minutes <= 0 or minutes_left >= window_minutes:
        return PANEL_WIDTH
    if minutes_left <= 0:
        return 1
    width = round(PANEL_WIDTH * minutes_left / window_minutes)
    return max(1, min(PANEL_WIDTH, width))


def _state_for(minutes_left: float, notice_minutes: int, warn_minutes: int,
               in_progress: bool) -> str:
    if in_progress:
        return STATE_IN_PROGRESS
    if minutes_left <= warn_minutes:
        return STATE_WARNING
    if minutes_left <= notice_minutes:
        return STATE_NOTICE
    return STATE_NORMAL


def _minutes_left(event: CalEvent, now: datetime, in_progress: bool) -> float:
    """Minutes until the moment that currently matters for `event`:
    time-to-end if it's in progress, time-to-start otherwise. Factored out
    of build_elements so main.run_once can compute the identical value
    (same `event`/`now`/`in_progress` in, same number out, no drift risk)
    for the v1.5.2 priority/LED/chirp decisions without duplicating this
    branch inline."""
    if in_progress:
        return (event.end - now).total_seconds() / 60
    return (event.start - now).total_seconds() / 60


# --- v1.5.2 escalation ladder: priority, LED, and event-start chirp -------------
#
# A persistent CI failure alert (busybar.display.PRIORITY_ALERT) used to
# permanently evict the calendar, hiding an imminent event with no way for
# the ambient tier to ever reclaim the screen -- an operator-reported UX
# gap. The fix is a state-DEPENDENT draw priority: as an upcoming event
# gets closer, calendar_countdown climbs the shared priority ladder so it
# can no longer be silently buried, first by the overlay-tier CI
# badge/quota rotation (PRIORITY_AMBIENT_RAISED, once inside
# `approach_minutes`) and then by a genuine alert itself
# (PRIORITY_AMBIENT_URGENT, once inside `notice_minutes`, covering both
# the existing NOTICE/WARNING visual states unchanged). See
# busybar.display's docstrings for the full priority-tier contracts and
# the eviction/409 interplay this creates with ci_status's own alert
# (worked through in the spec doc's v1.5.2 section), and IMMINENT_LED_COLOR
# below for the final-minute LED signal that rides alongside it -- ASSUMED
# (not verified) to survive a session's panel-level eviction; see
# busybar.display.PRIORITY_AMBIENT_URGENT's docstring for the caveat.
#
# Deliberately NOT elevated while in_progress: once a meeting has started
# you already know about it (you're either in it or conspicuously not) --
# the elevation exists to catch your attention BEFORE an event starts, not
# to keep fighting for the screen once it has. An alert regains the panel
# for an in-progress (or normal, un-approaching) event exactly as it did
# before this feature existed.
IMMINENT_LED_COLOR = "#E24B4AFF"

LED_OFF_COLOR = "#00000000"
# ^ Explicit LED-off value (zero alpha, matching this device's
# #RRGGBBAA convention elsewhere -- e.g. RectangleElement's own
# transparent-fill default). Whether OMITTING led_notification_color
# entirely turns a previously-set LED off, or whether the LED is
# "sticky" until explicitly changed, is NOT verifiable through this
# API (no endpoint exposes current LED state, and the device's own
# OpenAPI doc -- already shown wrong about priority arbitration
# elsewhere in this codebase -- claims omission means "will not
# blink," which isn't the same claim as "turns off a currently-lit
# LED"). Sending this explicit off value on every on->off transition is
# the hypothesis-agnostic safe choice: correct whether omission alone
# would have worked or not, at the cost of one redundant field on the
# rare poll where a transition actually happens. See resolve_led_value.
LED_OFF_ELEMENTS = [{
    "id": "led_off_flush", "type": "rectangle", "x": 0, "y": 0,
    "width": 1, "height": 1, "fill": "solid", "fill_colors": ["#00000000"],
    "border_width": 0, "timeout": 5,
}]
# ^ A minimal (1x1, fully transparent, 5s self-expiring) placeholder
# element -- the draw endpoint's `elements` field requires at least one
# entry (minItems: 1 in the device's own schema), so there is no way to
# send a bare led_notification_color with no visible element at all.
# Used only on the "no upcoming event" path (main.run_once), where
# there is otherwise nothing to draw but the LED may still need an
# explicit off transition -- invisible against any background, and
# self-expires quickly regardless.


def resolve_led_value(led_should_be_on: bool, led_was_on: bool) -> str | None:
    """The actual `led_notification_color` to send THIS draw, combining
    "should the LED be on right now" (select_led) with the caller's own
    tracked previous-poll LED state. Returns `IMMINENT_LED_COLOR` while
    the LED should be on; `LED_OFF_COLOR` (explicit, not just an omitted
    field) on the exact poll where the LED transitions from on to off --
    see LED_OFF_COLOR's own docstring for why omission alone isn't
    trusted; `None` (omit the field) once already off and staying off,
    since there's nothing to turn off and omitting is the more compact
    request.

    This is intentionally the ONLY place that decides between
    IMMINENT_LED_COLOR / LED_OFF_COLOR / None -- main.run_once must call
    this for every path that can draw or otherwise signal the device
    (the normal event draw, AND the "no upcoming event" path via
    LED_OFF_ELEMENTS), tracking `led_was_on` in its own caller-owned
    state dict, committed only after a confirmed successful send (the
    same DRAWN-gated-commit discipline used throughout this codebase) --
    a vanishing event (an all-day filter, an event shorter than one poll
    interval) must still resolve to an explicit off, not silently skip
    the transition just because there's no "normal" draw to piggyback it
    on.
    """
    if led_should_be_on:
        return IMMINENT_LED_COLOR
    if led_was_on:
        return LED_OFF_COLOR
    return None


CHIRP_STOCK_PATH = "shared/calendar_event_starts.snd"
# ^ A firmware-shipped stock sound (see BusyBarClient.play_audio), not a
# generated/uploaded asset -- no asset generation, upload, or
# repo-committed binary is needed for the chirp; see the spec doc's
# v1.5.2 section for why the naming ("calendar event starts") is an
# exact semantic match for the T-0 chirp this feature fires.
#
# v1.5.2.1 CORRECTION: this was originally ".wav" -- the v1.5.2 on-device
# probe (POST /api/audio/play with that stock_path) returned 200, which
# was WRONGLY taken as confirmation the chirp was audible. It was not.
# Root cause, confirmed by operator ear-testing plus device storage
# forensics (a live GET of /api/storage/list against
# /ext/apps_assets/shared/sounds -- nothing in the source tree or the
# OpenAPI spec reveals this): the firmware build pipeline converts .wav
# SOURCE files to **.snd** at packaging time; the runtime filenames are
# .snd (e.g. calendar_event_starts.snd), never .wav. Compounding this,
# /api/audio/play returns 200 BEFORE the actual (deferred) file open --
# playback is queued behind a ~100ms amp holdoff, and an open failure at
# holdoff-fire is logged device-side only and otherwise swallowed. A
# `True` from play_audio therefore does NOT prove audible playback; a
# wrong filename (like the original ".wav" here) is indistinguishable
# from success at every software layer available to this codebase. The
# operator's ear-test matrix that confirmed this: stock ".wav" -> silent,
# an uploaded ".wav" asset -> audible, stock ".snd" -> audible. See
# BusyBarClient.play_audio's docstring for the same caveat stated at the
# API-client level, and the spec doc's firmware-facts section for the
# full writeup.


def check_threshold_ordering(cfg: dict) -> str | None:
    """Sanity-checks the escalation ladder's ASSUMED ordering:
    `approach_minutes > notice_minutes > warn_minutes >= imminent_minutes`.
    Returns a warning message naming the first violated invariant (or
    `None` if the config is sane) -- the caller (main()) should log this
    ONCE at startup, not every poll.

    A violated ordering doesn't crash anything -- select_priority and
    select_led each evaluate their own thresholds independently and will
    still produce SOME answer -- but the ladder's intended meaning
    ("closer to the event = more urgent") breaks down in ways that are
    easy to misconfigure by accident. Concretely: `select_priority` checks
    `<= notice_minutes` before `<= approach_minutes`, so if
    `approach_minutes <= notice_minutes`, the "approach" tier
    (PRIORITY_AMBIENT_RAISED) becomes a dead branch that's never actually
    reached -- anything inside `approach_minutes` is already inside
    `notice_minutes` too and matches that check first. Similarly
    `notice_minutes <= warn_minutes` would make the NOTICE/amber visual
    state (`_state_for`) unreachable, and `warn_minutes < imminent_minutes`
    would mean the LED's imminent window extends beyond the red WARNING
    state that's supposed to contain it.
    """
    approach = cfg.get("approach_minutes")
    notice = cfg.get("notice_minutes")
    warn = cfg.get("warn_minutes")
    imminent = cfg.get("imminent_minutes")
    if None in (approach, notice, warn, imminent):
        return None   # an old-style cfg dict missing v1.5.2 keys -- nothing to check
    if approach <= notice:
        return (f"[calendar_countdown] approach_minutes ({approach}) should be greater than "
               f"notice_minutes ({notice}) -- the escalation ladder assumes approach_minutes > "
               f"notice_minutes > warn_minutes >= imminent_minutes; as configured, the 'approach' "
               f"priority tier (PRIORITY_AMBIENT_RAISED) may never actually be reached.")
    if notice <= warn:
        return (f"[calendar_countdown] notice_minutes ({notice}) should be greater than "
               f"warn_minutes ({warn}) -- the escalation ladder assumes approach_minutes > "
               f"notice_minutes > warn_minutes >= imminent_minutes; as configured, the NOTICE "
               f"(amber) visual state may never actually be reached.")
    if warn < imminent:
        return (f"[calendar_countdown] warn_minutes ({warn}) should be >= imminent_minutes "
               f"({imminent}) -- the escalation ladder assumes approach_minutes > notice_minutes > "
               f"warn_minutes >= imminent_minutes; as configured, the LED's imminent window "
               f"extends beyond the red WARNING state meant to contain it.")
    return None


def is_just_started(event: CalEvent, now: datetime, in_progress: bool,
                    start_window_seconds: int, start_animation: str) -> bool:
    """True for the first `start_window_seconds` after an event begins, when a
    start-takeover animation is configured. The window aligns with the T-0
    chirp and holds the display at urgent priority as a 'running late' alarm."""
    if not in_progress or not start_animation:
        return False
    return (now - event.start).total_seconds() < start_window_seconds


def select_priority(minutes_left: float, approach_minutes: int, notice_minutes: int,
                    in_progress: bool, just_started: bool = False) -> int:
    """The draw priority for this poll (v1.5.2 escalation ladder) --
    deliberately a SEPARATE ladder from `_state_for`'s visual-palette
    selection, not a 1:1 mapping of it: the "approach" window changes
    priority without changing the palette at all (still STATE_NORMAL
    colors -- see build_elements), and the NOTICE and WARNING visual
    states share the SAME priority (both must be able to preempt a
    persistent alert -- the whole point of this tier) even though they're
    visually distinct.

    - just_started (v1.6): PRIORITY_AMBIENT_URGENT (65), checked first --
      the start-takeover window (see is_just_started) is itself a "running
      late" alarm and must be able to preempt a persistent alert exactly
      like the NOTICE/WARNING tiers below do.
    - in_progress (and not just_started): PRIORITY_AMBIENT (20) -- see the
      module-level comment above for why elevation doesn't apply here.
    - <= notice_minutes (covers both NOTICE and WARNING visually):
      PRIORITY_AMBIENT_URGENT (65) -- strictly above PRIORITY_ALERT, so a
      persistent CI failure/stuck alert no longer permanently buries an
      imminent event.
    - <= approach_minutes (but > notice_minutes): PRIORITY_AMBIENT_RAISED
      (25) -- strictly above PRIORITY_OVERLAY, so the countdown can no
      longer be silently interrupted by the running-CI badge/quota
      rotation during this window, but still strictly below PRIORITY_ALERT
      -- a genuine alert still wins over a merely-approaching event.
    - otherwise (normal, > approach_minutes): PRIORITY_AMBIENT (20).
    """
    if just_started:
        return PRIORITY_AMBIENT_URGENT
    if in_progress:
        return PRIORITY_AMBIENT
    if minutes_left <= notice_minutes:
        return PRIORITY_AMBIENT_URGENT
    if minutes_left <= approach_minutes:
        return PRIORITY_AMBIENT_RAISED
    return PRIORITY_AMBIENT


def select_led(minutes_left: float, imminent_minutes: int, in_progress: bool) -> bool:
    """Whether the LED should be on RIGHT NOW, purely a function of the
    current moment -- True on every poll from `imminent_minutes` before
    start until the event actually starts (a continuous blink through the
    final window, not a one-shot), False the instant `in_progress` is
    true (no LED once the event has started; the LED's job is to announce
    the imminent start, not to keep announcing an event already
    underway). Independent of `select_priority`: the LED is a separate
    hardware channel from the drawn elements' priority arbitration
    entirely, and per PRIORITY_AMBIENT_URGENT's docstring is assumed to
    still get through even when a BUSY/CUSTOM session (PRIORITY_SESSION,
    90) owns the whole panel -- unverified beyond the request payload
    itself; see that docstring's caveat.

    Deliberately does NOT decide the actual `led_notification_color`
    value to send -- that also depends on whether the LED was already on
    last poll (see resolve_led_value), which this function has no
    knowledge of and shouldn't need to: it answers "should it be on now,"
    the caller (resolve_led_value, called from main.run_once) answers
    "what do I need to SEND to make that true, given what was sent
    before."
    """
    return not in_progress and minutes_left <= imminent_minutes


def _chirp_key(event: CalEvent) -> tuple[datetime, str]:
    """The identity should_chirp/commit_chirped track an event by:
    `(start, ascii-safe title)`, not `start` alone. Two distinct events
    that happen to share the exact same start timestamp (all-day events
    sharing midnight, or two calendars both firing something at the same
    moment) would otherwise collide on a single set entry -- one event's
    "seen upcoming" or "chirped" marker would incorrectly apply to the
    other. `ascii_safe` matches the same sanitization already applied to
    titles elsewhere in this module, so the key is stable regardless of
    non-ASCII characters in the raw title."""
    return (event.start, ascii_safe(event.title))


def _prune_chirp_state(chirp_state: dict, now: datetime, max_age_hours: int = 24) -> None:
    """Drops entries whose start timestamp is older than `max_age_hours`
    from both tracked sets, so a long-running process's chirp bookkeeping
    doesn't grow without bound over weeks/months of uptime. Called on
    every should_chirp check; cheap (a couple of set comprehensions over
    what is in practice a small number of distinct events)."""
    cutoff = now - timedelta(hours=max_age_hours)
    for key in ("seen_upcoming", "chirped"):
        if key in chirp_state:
            chirp_state[key] = {k for k in chirp_state[key] if k[0] >= cutoff}


def should_chirp(event: CalEvent, in_progress: bool, now: datetime,
                 chirp_state: dict, chirp_enabled: bool) -> bool:
    """True exactly on the poll where THIS PROCESS observes `event`
    transition from upcoming to started -- edge detection, not level
    detection. `chirp_state` is a caller-owned dict (same pattern as every
    other cache in this codebase) tracking two sets keyed by
    `_chirp_key(event)` (`(start, ascii-safe title)`, not `start` alone --
    see that function's docstring for why): "seen_upcoming" (events this
    process has observed with in_progress=False at some earlier poll) and
    "chirped" (events already fired for). Every call records `event` into
    "seen_upcoming" when it's not yet in progress, REGARDLESS of
    `chirp_enabled` -- so the bookkeeping stays accurate even if chirp is
    toggled on mid-run. The True/False decision itself only fires when:
    `chirp_enabled`, `in_progress` is true THIS poll, `event`'s key was
    previously seen upcoming, and it hasn't already been chirped.

    This is the mechanism behind two required behaviors: (1) a process
    that starts up mid-event (in_progress=True on the very first poll it
    ever sees for that event) never added that event's key to
    "seen_upcoming", so the transition is never detected and no chirp
    fires for it -- restarting during an event's final minute, or any
    time after it started, does not produce a spurious chirp. (2) a
    continuously-running process chirps exactly once per event, on the
    single poll where the transition is observed, never again on
    subsequent in_progress polls for the same event.

    Does NOT itself mark anything "chirped" -- see commit_chirped, which
    the caller must invoke only once the actual audio play call is
    confirmed to have succeeded, so a transient failure retries on the
    next poll rather than silently skipping the chirp forever (the same
    DRAWN-gated-commit discipline used throughout this codebase for
    display state).
    """
    _prune_chirp_state(chirp_state, now)
    key = _chirp_key(event)
    if not in_progress:
        chirp_state.setdefault("seen_upcoming", set()).add(key)
        return False
    if not chirp_enabled:
        return False
    seen_upcoming = chirp_state.get("seen_upcoming", set())
    chirped = chirp_state.get("chirped", set())
    return key in seen_upcoming and key not in chirped


def commit_chirped(event: CalEvent, chirp_state: dict) -> None:
    """Marks `event` as chirped -- call only after confirming the actual
    `client.play_audio` call succeeded (see should_chirp's docstring)."""
    chirp_state.setdefault("chirped", set()).add(_chirp_key(event))


def next_sleep_seconds(poll_seconds: float, seconds_until_start: float | None) -> float:
    """The sleep duration before the next poll (v1.5.2 chirp T-0
    precision). Normally just `poll_seconds`, but if the currently-known
    upcoming event's start is sooner than a full poll interval away
    (`seconds_until_start` is not None and strictly between 0 and
    `poll_seconds`), sleeps exactly until that start instead -- so the
    poll that detects the upcoming -> in_progress transition (and fires
    the chirp) lands within about a second of the real start time, rather
    than up to a full `poll_seconds` late. `seconds_until_start` of `None`
    (no upcoming event), `<= 0` (already started or passed), or
    `>= poll_seconds` (not imminent enough to matter) all fall through to
    the normal interval unchanged.
    """
    if seconds_until_start is not None and 0 < seconds_until_start < poll_seconds:
        return seconds_until_start
    return poll_seconds


def _title_fits(title: str, width_px: int) -> bool:
    return len(title) * SMALL_FONT_CHAR_PX <= width_px


def _format_countdown(minutes_left: float) -> str:
    """Minutes-granular countdown text for the `cd_text` element.

    Floored (not rounded) to whole minutes, so the display always reads "at
    least this much time remains" rather than rounding up past the actual
    remaining time -- matches how a countdown is conventionally read ("1m"
    means just under 2 minutes left, not "close to 1 minute"). Negative
    input (defensive only; callers shouldn't produce it) clamps to 0.

    Under 60 minutes: "<M>m" (e.g. "54m"). At/above 1 hour: tries the full
    "<H>h<MM>m" form (e.g. "1h05m", minutes zero-padded to 2 digits) and
    falls back to "<H>h" (minutes dropped) only if the full form's *actual*
    rendered width (via GLYPH_ADVANCE_PX) would exceed CD_TEXT_MAX_WIDTH.

    This is a per-string width check, not a fixed hour cutoff (e.g. "only
    hours >= 10 drop minutes"), because on-device measurement (v1.4
    re-verification) found a fixed cutoff is unsafe: this font's '1' glyph
    is ~30% narrower than every other digit (5px vs 7px advance), so
    whether a given single-digit-hour string fits depends on which actual
    digits appear, not just the hour count. Measured examples: "1h05m" and
    "1h59m" fit (the hours digit's own "1" is enough headroom); "9h11m"
    fits too (two "1"s in the minutes make up for a non-"1" hours digit);
    but "2h05m", "5h55m", "9h59m", and even "0h00m" do NOT fit (34px
    measured against a 33px budget) despite all being single-digit-hour,
    5-glyph strings -- the same nominal "shape" the original fixed-cutoff
    design assumed was uniformly safe. Two-digit hours (10+) always fail
    the width check on their own digits alone and correctly fall through to
    the hour-only form as a natural consequence, with no special case
    needed.
    """
    total_minutes = max(0, int(minutes_left))
    hours, minutes = divmod(total_minutes, 60)
    if hours == 0:
        return f"{minutes}m"
    full_form = f"{hours}h{minutes:02d}m"
    if _text_width_px(full_form) <= CD_TEXT_MAX_WIDTH:
        return full_form
    return f"{hours}h"


def build_elements(event: CalEvent, now: datetime, cfg: dict, timeout_s: int,
                   in_progress: bool, just_started: bool = False) -> list[dict]:
    """Compose a title, horizon and equal-size time readouts on the native canvas.

    The urgent 16px animation has a reserved lane. Its title stays visible in
    both warning stages, with a compact IN label beside the large countdown.
    Text y coordinates include the firmware's measured two-pixel ink offset.
    Full expiring frames still use the existing priority and redraw contracts.
    """
    minutes_left = _minutes_left(event, now, in_progress)
    state = _state_for(minutes_left, cfg["notice_minutes"], cfg["warn_minutes"], in_progress)
    bg = {"id": "bg", "type": "rectangle", "x": 0, "y": 0,
          "width": PANEL_WIDTH, "height": PANEL_HEIGHT, "fill": "gradient_v",
          "fill_colors": BG_GRADIENT[STATE_IN_PROGRESS if just_started else state],
          "border_width": 0, "timeout": timeout_s}
    if just_started:
        return [bg, {"id": START_ANIM_ID, "type": "animation",
                     "stock_path": f"shared/{cfg['start_animation']}.anim",
                     "x": 0, "y": 0, "loop": True, "timeout": timeout_s}]

    show_icon = not in_progress and cfg.get("escalation_icons") and state == STATE_WARNING
    title_x = ICON_TITLE_X if show_icon else TITLE_X
    title_width = PANEL_WIDTH - title_x - 2
    title_text = ascii_safe(event.title).upper()
    title = {"id": "title", "type": "text", "text": title_text, "font": "small",
             "color": TITLE_COLOR[state], "x": title_x, "y": TITLE_Y,
             "width": title_width, "timeout": timeout_s}
    if not _title_fits(title_text, title_width):
        title.update(scroll_rate=SCROLL_RATE, scroll_start_delay=SCROLL_DELAY_MS,
                     scroll_repeat_delay=SCROLL_DELAY_MS)

    track_x = ICON_TITLE_X if show_icon else 0
    track_width = PANEL_WIDTH - track_x
    if in_progress:
        duration = (event.end - event.start).total_seconds()
        fraction = (event.end - now).total_seconds() / duration if duration > 0 else 1.0
        fill_width = max(1, min(track_width, round(track_width * fraction)))
        fill = {"fill": "gradient_h", "fill_colors": ["#159987FF", TRACK_FILL_IN_PROGRESS]}
    else:
        fill_width = max(1, min(track_width, round(
            _track_fill_width(minutes_left, cfg["progress_window_minutes"]) * track_width / PANEL_WIDTH)))
        fill = {"fill": "gradient_h", "fill_colors": TRACK_FILL_GRADIENT[state]}
    track = {"id": "track", "type": "rectangle", "x": track_x, "y": TRACK_Y,
             "width": track_width, "height": TRACK_HEIGHT, "fill": "solid",
             "fill_colors": [TRACK_COLOR], "border_width": 0, "timeout": timeout_s}
    track_fill = {**track, "id": "track_fill", "width": fill_width, **fill}
    tip_width = min(2, fill_width)
    track_tip = {**track, "id": "track_tip", "x": track_x + fill_width - tip_width,
                 "width": tip_width, "fill_colors": ["#E9FFFFFF"]}
    elements = [bg, title, track, track_fill, track_tip]

    if in_progress:
        elements.append({"id": "ends", "type": "text", "text": ENDS_TEXT, "font": "bold",
                         "color": ENDS_TEXT_COLOR, "x": ENDS_X, "y": ENDS_Y,
                         "timeout": timeout_s})
    elif show_icon:
        elements.append({"id": "starts_label", "type": "text", "text": "IN", "font": "small",
                         "color": TITLE_COLOR[state], "x": 20, "y": 9, "timeout": timeout_s})
    else:
        elements.append({"id": "time", "type": "text", "text": f"{event.start.astimezone():%H:%M}",
                         "font": "large", "color": TIME_TEXT_COLOR, "x": TIME_X, "y": TIME_Y,
                         "timeout": timeout_s})
    if not show_icon:
        elements.append({"id": "divider", "type": "rectangle", "x": DIVIDER_X,
                         "y": DIVIDER_Y, "width": DIVIDER_WIDTH, "height": DIVIDER_HEIGHT,
                         "fill": "solid", "fill_colors": [DIVIDER_COLOR[state]],
                         "border_width": 0, "timeout": timeout_s})

    # Keep the calendar's final-minute cue separate from shared ETA formatting.
    countdown = "<1m" if not in_progress and 0 < minutes_left < 1 else _format_countdown(minutes_left)
    elements.append({"id": "cd_text", "type": "text", "text": countdown, "font": "large",
                     "color": DIGIT_COLOR[state], "x": CD_TEXT_X, "y": CD_TEXT_Y,
                     "width": CD_TEXT_MAX_WIDTH, "timeout": timeout_s})
    if show_icon:
        icon = ICON_REMINDER if minutes_left <= cfg["imminent_minutes"] else ICON_EVENT
        elements.append({"id": CAL_ICON_ID, "type": "animation", "stock_path": f"shared/{icon}.anim",
                         "x": ICON_X, "y": ICON_Y, "loop": True, "timeout": timeout_s})
    return elements
