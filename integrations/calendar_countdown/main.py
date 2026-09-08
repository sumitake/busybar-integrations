import sys
from pathlib import Path

try:
    import busybar  # noqa: F401
except ImportError:  # bare clone / broken editable install: use the repo's src/
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import argparse
import logging
import time
from datetime import datetime, timezone

from busybar.client import BusyBarClient, DrawResult
from busybar.config import device_kwargs, load_config
from busybar.display import PRIORITY_AMBIENT, ambient_timeout
from busybar.presentation import modern_display, prepare_frame, commit_frame

from .logic import (ascii_safe, build_elements, select_active_event,
                    select_next_event, _minutes_left, select_priority,
                    select_led, resolve_led_value, LED_OFF_ELEMENTS, LED_OFF_COLOR,
                    should_chirp, commit_chirped, is_just_started,
                    next_sleep_seconds, CHIRP_STOCK_PATH, check_threshold_ordering)

APP = "calendar_countdown"
HEARTBEAT_SECONDS = 600
log = logging.getLogger(APP)


def run_once(client, fetch, cfg: dict, now: datetime, dry_run: bool,
            state: dict | None = None) -> str:
    """Render one complete expiring frame and remember confirmed display state.

    Firmware 1.2.3 can remove obsolete IDs while preserving common content at
    the same priority. Older firmware, type changes, and priority reductions
    use an app-scoped clear. Full scheduled redraws still renew timeouts and
    reclaim a canvas evicted by another app. Audio is attempted once per event
    edge, because a missing response does not prove it failed to play.
    """
    c = cfg["calendar_countdown"]
    timeout_s = ambient_timeout(c["poll_seconds"])
    events = fetch(c["lookahead_hours"])
    active = select_active_event(events, now)

    if c["auto_busy"] and not dry_run and active is not None:
        remaining_ms = int((active.end - now).total_seconds() * 1000)
        busy = client.get_busy()
        snapshot = busy.get("snapshot", {}) if isinstance(busy, dict) else {}
        if isinstance(snapshot, dict) and snapshot.get("type") == "NOT_STARTED":
            client.set_busy_simple(remaining_ms)

    # An in-progress event takes display priority over a later upcoming one.
    if active is not None:
        event, in_progress = active, True
    else:
        event, in_progress = select_next_event(
            events, now, c["lookahead_hours"], c["include_all_day"]), False

    if event is None:
        if not dry_run:
            # LED-off flush (v1.5.2): the event vanished (filtered out, or
            # was shorter than one poll interval) without ever passing
            # through the normal draw path below, which is the only other
            # place that would otherwise send an explicit LED-off. There's
            # nothing to draw, but if the LED is believed to still be lit
            # from an earlier poll, it must still be explicitly turned off
            # -- clear() alone can't carry the LED field (it's a bare
            # DELETE, no body), so a minimal placeholder draw carries it
            # instead. See LED_OFF_ELEMENTS/resolve_led_value's docstrings.
            if state is not None and state.get("led_on"):
                led_off_result = client.draw(APP, elements=LED_OFF_ELEMENTS,
                                             priority=PRIORITY_AMBIENT,
                                             led_notification_color=LED_OFF_COLOR)
                if led_off_result == DrawResult.DRAWN:
                    state["led_on"] = False
                # else: leave led_on=True so the next poll retries the
                # off-transition rather than assuming it landed.
            client.clear(APP)
        if state is not None:
            state["last_shape"] = None   # device is now genuinely blank
            state["next_start"] = None
        return "no upcoming event; cleared"

    # Recorded regardless of dry_run: this is pure bookkeeping about what
    # the calendar says, not a device action, so main()'s sleep-shortening
    # calculation stays accurate even across dry-run polls.
    if state is not None:
        state["next_start"] = None if in_progress else event.start

    label = f"{'active' if in_progress else 'upcoming'} {ascii_safe(event.title)!r}"
    if dry_run:
        return f"DRY-RUN would draw: {label} (in_progress={in_progress})"

    if state is not None:
        if should_chirp(event, in_progress, now, state, c["chirp"]):
            # Record the attempt before sending. A timeout can follow playback;
            # repeating it on the next calendar poll would duplicate the sound.
            commit_chirped(event, state)
            played = client.play_audio(APP, stock_path=CHIRP_STOCK_PATH)
            log.info("chirp played (%s) -> %s", CHIRP_STOCK_PATH, played)

    modern = modern_display(client)
    # v1.6 start-takeover: True for the first start_window_seconds after an
    # event begins (see is_just_started's docstring) -- holds the display
    # at PRIORITY_AMBIENT_URGENT and swaps in the full-panel takeover
    # animation, threaded into both build_elements and select_priority below.
    just_started = is_just_started(event, now, in_progress,
                                   c["start_window_seconds"], c["start_animation"])
    elements = build_elements(event, now, c, timeout_s, in_progress, just_started=just_started)
    minutes_left = _minutes_left(event, now, in_progress)
    priority = select_priority(minutes_left, c["approach_minutes"], c["notice_minutes"],
                               in_progress, just_started=just_started)
    led_should_be_on = select_led(minutes_left, c["imminent_minutes"], in_progress)
    led_was_on = state.get("led_on", False) if state is not None else False
    led = resolve_led_value(led_should_be_on, led_was_on)

    elements = prepare_frame(client, APP, elements, priority, state, modern=modern)

    result = client.draw(APP, elements=elements, priority=priority, led_notification_color=led)

    # A missing stock animation may reject the takeover. Fall back to the
    # ordinary countdown on ERROR; a busy owner or unreachable device does not
    # benefit from another lower-priority request.
    if just_started and result == DrawResult.ERROR:
        log.warning("start-takeover animation %r not drawable; falling back to "
                    "in-progress layout for this poll", c["start_animation"])
        elements = build_elements(event, now, c, timeout_s, in_progress, just_started=False)
        priority = select_priority(minutes_left, c["approach_minutes"], c["notice_minutes"],
                                   in_progress, just_started=False)
        elements = prepare_frame(client, APP, elements, priority, state, modern=modern)
        result = client.draw(APP, elements=elements, priority=priority, led_notification_color=led)
        label = f"{label} [start-anim fallback]"

    commit_frame(state, elements, priority, result)
    if state is not None and result == DrawResult.DRAWN:
        state["led_on"] = led_should_be_on
    return f"drew {label} -> {result.value}"


def should_log_info(summary: str, last_logged_summary: str | None,
                    seconds_since_heartbeat: float,
                    heartbeat_seconds: int = HEARTBEAT_SECONDS) -> bool:
    """Log-noise control for the v1.5 poll-cadence drop (poll_seconds
    60 -> 10 as the ambient-tier default): at 10s polling, logging every
    summary at INFO would sixfold the audit log's line rate versus the
    old 60s cadence for no new information on most polls (the summary is
    usually identical poll to poll). INFO only when the summary actually
    changed since the last INFO line, or a heartbeat interval has elapsed
    (so a long unchanging run still leaves a periodic "yes, I'm alive"
    trail) -- DEBUG otherwise.
    """
    return summary != last_logged_summary or seconds_since_heartbeat >= heartbeat_seconds


def main() -> int:
    parser = argparse.ArgumentParser(description="BUSY Bar calendar countdown")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list-calendars", action="store_true",
                        help="print available calendar names and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from . import eventkit  # macOS-only import kept out of module scope for tests
    if not eventkit.ensure_access():
        log.error("Calendar access denied. Grant access in System Settings > "
                  "Privacy & Security > Calendars, then rerun.")
        return 1

    if args.list_calendars:
        for title, account in eventkit.list_calendars():
            print(f"{account}: {title}")
        print("Add the titles you want to [calendar_countdown] calendars in config.toml")
        return 0

    cfg = load_config()
    ordering_warning = check_threshold_ordering(cfg["calendar_countdown"])
    if ordering_warning is not None:
        log.warning(ordering_warning)
    client = BusyBarClient(**device_kwargs(cfg))
    # Drop any stale elements from a previous process. This also protects a
    # restart onto this version against every id change made across the
    # v1.3 -> v1.3.1 -> v1.4 line: v1.3.1 replaced the native "countdown"
    # element with a "cd_text" text element (a type change under upsert can
    # serve stale pixel data), and v1.4 removed "time_card"/"cd_card"
    # entirely. Neither removed id needs a drawn successor -- this startup
    # clear plus the transition-state clear above are sufficient, since a
    # deploy always restarts the process (fresh client.clear(APP) here) and
    # build_elements() simply never emits those ids again afterward.
    if not args.dry_run:
        client.clear(APP)
    fetch = lambda hours: eventkit.fetch_events(hours, cfg["calendar_countdown"]["calendars"])

    backoff = 5
    state: dict = {}
    last_logged_summary: str | None = None
    last_heartbeat = time.monotonic()
    while True:
        summary = run_once(client, fetch, cfg, datetime.now(timezone.utc), args.dry_run, state=state)
        now_monotonic = time.monotonic()
        if args.once or should_log_info(summary, last_logged_summary, now_monotonic - last_heartbeat):
            log.info(summary)
            last_logged_summary = summary
            last_heartbeat = now_monotonic
        else:
            log.debug(summary)
        if args.once:
            return 0
        if summary.endswith(DrawResult.UNREACHABLE.value):
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
        else:
            backoff = 5
            # v1.5.2 T-0 chirp precision: sleep exactly until the next
            # known event's start, not a full poll interval, when that's
            # sooner -- see next_sleep_seconds's docstring.
            next_start = state.get("next_start")
            seconds_until_start = ((next_start - datetime.now(timezone.utc)).total_seconds()
                                   if next_start is not None else None)
            time.sleep(next_sleep_seconds(cfg["calendar_countdown"]["poll_seconds"], seconds_until_start))


if __name__ == "__main__":
    raise SystemExit(main())
