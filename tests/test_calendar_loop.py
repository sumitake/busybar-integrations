from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations"))
from calendar_countdown.logic import CalEvent, _format_countdown
from calendar_countdown.main import run_once, should_log_info
from busybar.client import DrawResult
from busybar.display import PRIORITY_AMBIENT

TZ = timezone.utc
NOW = datetime(2026, 8, 3, 13, 37, tzinfo=TZ)
CFG = {"calendar_countdown": {"poll_seconds": 60, "lookahead_hours": 12,
                              "warn_minutes": 5, "notice_minutes": 15,
                              "progress_window_minutes": 60,
                              "include_all_day": False,
                              "auto_busy": False, "calendars": [],
                              # v1.5.2 escalation ladder + chirp
                              "approach_minutes": 30, "imminent_minutes": 1,
                              "chirp": True,
                              # v1.6 stock-animation accents: run_once reads
                              # these unconditionally (is_just_started), so
                              # every fixture using CFG needs them present --
                              # same defaults as busybar.config.DEFAULTS and
                              # test_calendar_logic.py's own cfg.
                              "start_animation": "meeting_72x16",
                              "start_window_seconds": 60}}


def make_event(offset_min: int, dur_min: int = 30, title: str = "Standup") -> CalEvent:
    start = NOW + timedelta(minutes=offset_min)
    return CalEvent(title, start, start + timedelta(minutes=dur_min), False)


def test_draws_countdown_for_upcoming_event():
    client = Mock()
    client.draw.return_value = DrawResult.DRAWN
    # offset > approach_minutes (30) so this stays in the baseline "normal"
    # priority tier -- see the v1.5.2 escalation-ladder tests below for the
    # approach/notice/warn priority selection itself.
    event = make_event(40)
    summary = run_once(client, lambda hours: [event], CFG, NOW, dry_run=False)
    client.draw.assert_called_once()
    kwargs = client.draw.call_args.kwargs
    assert kwargs["priority"] == PRIORITY_AMBIENT == 20
    by_id = {el["id"]: el for el in kwargs["elements"]}
    # v1.4 "airy": no card elements.
    assert set(by_id) == {"bg", "title", "track", "track_fill", "track_tip", "time", "divider", "cd_text"}
    assert by_id["cd_text"]["text"] == _format_countdown((event.start - NOW).total_seconds() / 60)
    assert by_id["title"]["text"] == "STANDUP"   # uppercased after ascii_safe
    assert "drew" in summary

def test_draws_in_progress_event_targeting_active_over_upcoming():
    client = Mock()
    client.draw.return_value = DrawResult.DRAWN
    active = make_event(-5, dur_min=30, title="Active")
    upcoming = make_event(120, title="Later")
    summary = run_once(client, lambda hours: [active, upcoming], CFG, NOW, dry_run=False)
    kwargs = client.draw.call_args.kwargs
    by_id = {el["id"]: el for el in kwargs["elements"]}
    assert set(by_id) == {"bg", "title", "track", "track_fill", "track_tip", "ends", "divider", "cd_text"}
    assert by_id["title"]["text"] == "ACTIVE"
    assert by_id["cd_text"]["text"] == _format_countdown((active.end - NOW).total_seconds() / 60)
    assert "time" not in by_id
    assert "drew" in summary

def test_clears_when_no_event():
    client = Mock()
    run_once(client, lambda hours: [], CFG, NOW, dry_run=False)
    client.clear.assert_called_once_with("calendar_countdown")
    client.draw.assert_not_called()

def test_dry_run_never_touches_device():
    client = Mock()
    summary = run_once(client, lambda hours: [make_event(3)], CFG, NOW, dry_run=True)
    client.draw.assert_not_called()
    client.clear.assert_not_called()
    assert "DRY-RUN" in summary


# --- state-transition clearing ------------------------------------------------
#
# The upcoming and in-progress layouts use different element id sets
# (time_card+time vs ends) and the device's draw endpoint upserts by id
# rather than replacing an app's whole element set (confirmed on-device: a
# stale opaque time_card rendered behind the new "ends" text for ~30s after
# a transition, until captured/fixed -- see display-v1.3-report.md). `state`
# lets run_once clear only at the transition, not every poll.

def test_no_clear_on_first_draw_with_fresh_state():
    client = Mock()
    client.draw.return_value = DrawResult.DRAWN
    state = {}
    run_once(client, lambda hours: [make_event(23)], CFG, NOW, dry_run=False, state=state)
    client.clear.assert_not_called()
    # v1.6: state["in_progress"] was replaced by the unified shape tracker
    # (state["last_shape"]) -- see main.run_once's docstring. The upcoming
    # layout's id set (no icon: CFG has no "escalation_icons" key).
    assert state["last_shape"] == frozenset(
        {"bg", "title", "track", "track_fill", "track_tip", "time", "divider", "cd_text"})

def test_no_clear_across_polls_with_same_state():
    client = Mock()
    client.draw.return_value = DrawResult.DRAWN
    state = {}
    run_once(client, lambda hours: [make_event(23)], CFG, NOW, dry_run=False, state=state)
    run_once(client, lambda hours: [make_event(22)], CFG, NOW, dry_run=False, state=state)
    client.clear.assert_not_called()

def test_clears_on_upcoming_to_in_progress_transition():
    client = Mock()
    client.draw.return_value = DrawResult.DRAWN
    state = {}
    run_once(client, lambda hours: [make_event(23)], CFG, NOW, dry_run=False, state=state)
    active = make_event(-5, dur_min=30, title="Active")
    run_once(client, lambda hours: [active], CFG, NOW, dry_run=False, state=state)
    client.clear.assert_called_once_with("calendar_countdown")
    # v1.6: the in-progress layout's id set ("ends" instead of "time").
    assert state["last_shape"] == frozenset(
        {"bg", "title", "track", "track_fill", "track_tip", "ends", "divider", "cd_text"})

def test_clears_on_in_progress_to_upcoming_transition():
    client = Mock()
    client.draw.return_value = DrawResult.DRAWN
    state = {}
    active = make_event(-5, dur_min=30, title="Active")
    run_once(client, lambda hours: [active], CFG, NOW, dry_run=False, state=state)
    run_once(client, lambda hours: [make_event(23)], CFG, NOW, dry_run=False, state=state)
    client.clear.assert_called_once_with("calendar_countdown")

def test_no_transition_clear_when_state_omitted():
    # Backward-compatible default: callers (and the other tests above) that
    # don't pass state see the pre-fix behavior -- no extra clear calls.
    client = Mock()
    client.draw.return_value = DrawResult.DRAWN
    run_once(client, lambda hours: [make_event(23)], CFG, NOW, dry_run=False)
    active = make_event(-5, dur_min=30, title="Active")
    run_once(client, lambda hours: [active], CFG, NOW, dry_run=False)
    client.clear.assert_not_called()

def test_failed_draw_leaves_state_unchanged_and_retries_next_poll():
    # (a) A transition poll whose draw() doesn't land (UNREACHABLE here, but
    # the same reasoning applies to REJECTED/ERROR) must not commit `state`
    # -- otherwise no future poll would ever retry the clear+draw pair, and
    # a stale element set from before the transition would persist
    # indefinitely instead of just until its own bounded timeout.
    client = Mock()
    client.draw.return_value = DrawResult.DRAWN
    state = {}
    upcoming_shape = frozenset({"bg", "title", "track", "track_fill", "track_tip", "time", "divider", "cd_text"})
    in_progress_shape = frozenset({"bg", "title", "track", "track_fill", "track_tip", "ends", "divider", "cd_text"})
    run_once(client, lambda hours: [make_event(23)], CFG, NOW, dry_run=False, state=state)
    assert state["last_shape"] == upcoming_shape

    active = make_event(-5, dur_min=30, title="Active")
    fetch = lambda hours: [active]

    client.draw.return_value = DrawResult.UNREACHABLE
    run_once(client, fetch, CFG, NOW, dry_run=False, state=state)
    assert state["last_shape"] == upcoming_shape   # unchanged: draw never landed
    assert client.clear.call_count == 1    # transition was still detected and clear attempted
    assert client.draw.call_count == 2

    # Next poll: state still mismatches (unchanged above), so it retries
    # clear() then draw(); this time draw succeeds and state finally commits.
    client.draw.return_value = DrawResult.DRAWN
    run_once(client, fetch, CFG, NOW, dry_run=False, state=state)
    assert client.clear.call_count == 2
    assert client.draw.call_count == 3
    assert state["last_shape"] == in_progress_shape

def test_clear_failure_does_not_block_state_commit_when_draw_succeeds():
    # (b) clear()'s own return value is intentionally ignored -- only
    # draw()'s result gates the state commit (see the comment in
    # main.run_once). If clear() fails but draw() still lands the new
    # element set, state should commit as transitioned: any leftover stale
    # ids are bounded by their own original timeout (a one-off, self-healing
    # gap), and gating on clear() too would make a persistently-failing
    # clear() retry every poll forever even once draws keep succeeding.
    client = Mock()
    client.clear.return_value = False
    client.draw.return_value = DrawResult.DRAWN
    state = {}
    run_once(client, lambda hours: [make_event(23)], CFG, NOW, dry_run=False, state=state)
    active = make_event(-5, dur_min=30, title="Active")
    run_once(client, lambda hours: [active], CFG, NOW, dry_run=False, state=state)
    client.clear.assert_called_once_with("calendar_countdown")
    assert state["last_shape"] == frozenset(
        {"bg", "title", "track", "track_fill", "track_tip", "ends", "divider", "cd_text"})

def test_state_reset_after_no_event_clear():
    client = Mock()
    client.draw.return_value = DrawResult.DRAWN
    state = {}
    run_once(client, lambda hours: [make_event(23)], CFG, NOW, dry_run=False, state=state)
    run_once(client, lambda hours: [], CFG, NOW, dry_run=False, state=state)  # clears, resets state
    assert state["last_shape"] is None
    client.clear.reset_mock()
    run_once(client, lambda hours: [make_event(23)], CFG, NOW, dry_run=False, state=state)
    # No stale elements remain after the "no event" clear -- no extra clear needed.
    client.clear.assert_not_called()


# --- log-noise control (v1.5: 10s default poll cadence) ------------------------
#
# At the ambient tier's shortened default poll interval (60s -> 10s), the
# old "log every summary at INFO" behavior would sixfold calendar.log's
# line rate for no new information on most polls (the summary is usually
# unchanged poll to poll). should_log_info decides INFO vs DEBUG.

def test_should_log_info_true_on_first_poll_no_prior_summary():
    assert should_log_info("drew X -> drawn", None, seconds_since_heartbeat=0) is True

def test_should_log_info_true_when_summary_changes():
    assert should_log_info("drew Y -> drawn", "drew X -> drawn", seconds_since_heartbeat=0) is True

def test_should_log_info_false_when_summary_unchanged_and_no_heartbeat_due():
    assert should_log_info("drew X -> drawn", "drew X -> drawn", seconds_since_heartbeat=1) is False

def test_should_log_info_true_on_heartbeat_even_if_unchanged():
    assert should_log_info("drew X -> drawn", "drew X -> drawn",
                           seconds_since_heartbeat=600, heartbeat_seconds=600) is True
    assert should_log_info("drew X -> drawn", "drew X -> drawn",
                           seconds_since_heartbeat=599, heartbeat_seconds=600) is False


# --- v1.5.2 escalation ladder + LED, end to end through run_once -----------------

from busybar.display import PRIORITY_AMBIENT_RAISED, PRIORITY_AMBIENT_URGENT
from calendar_countdown.logic import IMMINENT_LED_COLOR, CHIRP_STOCK_PATH

def test_run_once_draws_at_raised_priority_in_approach_window():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    event = make_event(20)   # inside approach_minutes(30), outside notice_minutes(15)
    run_once(client, lambda hours: [event], CFG, NOW, dry_run=False)
    assert client.draw.call_args.kwargs["priority"] == PRIORITY_AMBIENT_RAISED
    assert client.draw.call_args.kwargs["led_notification_color"] is None

def test_run_once_draws_at_urgent_priority_in_notice_window():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    event = make_event(10)   # inside notice_minutes(15)
    run_once(client, lambda hours: [event], CFG, NOW, dry_run=False)
    assert client.draw.call_args.kwargs["priority"] == PRIORITY_AMBIENT_URGENT
    assert client.draw.call_args.kwargs["led_notification_color"] is None

def test_run_once_led_fires_in_imminent_window():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    event = make_event(0.5)   # inside imminent_minutes(1)
    run_once(client, lambda hours: [event], CFG, NOW, dry_run=False)
    assert client.draw.call_args.kwargs["priority"] == PRIORITY_AMBIENT_URGENT
    assert client.draw.call_args.kwargs["led_notification_color"] == IMMINENT_LED_COLOR

def test_run_once_in_progress_stays_baseline_no_led():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    active = make_event(-1, dur_min=30, title="Active")
    run_once(client, lambda hours: [active], CFG, NOW, dry_run=False)
    assert client.draw.call_args.kwargs["priority"] == PRIORITY_AMBIENT
    assert client.draw.call_args.kwargs["led_notification_color"] is None


# --- v1.5.2 LED stuck-on fix: guaranteed off-transition, both repro shapes -------
#
# Critical review finding: omitting led_notification_color is unverified as
# a way to turn off a previously-lit LED (no status endpoint exposes LED
# state, and the device's own OpenAPI doc -- already wrong once about
# priority arbitration -- only claims omission means "won't blink", not
# "turns off a lit one"). resolve_led_value sends an EXPLICIT off value on
# every on->off transition instead, tracked via state["led_on"], covering
# both the normal in_progress transition and the "event vanishes without
# ever reaching in_progress" edge case the review specifically named
# (all-day filtering, or an event shorter than one poll interval).

def test_run_once_led_off_explicit_on_normal_in_progress_transition():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    state: dict = {}
    imminent = make_event(0.5)   # inside imminent_minutes(1) -- LED on
    run_once(client, lambda hours: [imminent], CFG, NOW, dry_run=False, state=state)
    assert client.draw.call_args.kwargs["led_notification_color"] == IMMINENT_LED_COLOR
    assert state["led_on"] is True

    # Next poll: the same event has started (in_progress=True) -- LED must
    # turn off via an EXPLICIT value, not just an omitted field.
    started = CalEvent(imminent.title, imminent.start, imminent.start + timedelta(minutes=30), False)
    later = imminent.start + timedelta(seconds=1)
    run_once(client, lambda hours: [started], CFG, later, dry_run=False, state=state)
    led_sent = client.draw.call_args.kwargs["led_notification_color"]
    assert led_sent is not None and led_sent != IMMINENT_LED_COLOR   # an explicit off value
    assert state["led_on"] is False

    # A further poll, still in_progress: LED already off and staying off
    # -- the field can be safely omitted now (nothing to turn off).
    run_once(client, lambda hours: [started], CFG, later + timedelta(seconds=10), dry_run=False, state=state)
    assert client.draw.call_args.kwargs["led_notification_color"] is None

def test_run_once_led_off_explicit_when_event_vanishes_without_in_progress():
    # Repro shape 1 (the review's specific concern): the event disappears
    # entirely on the next poll -- filtered out (e.g. all-day toggling) or
    # simply shorter than one poll interval -- WITHOUT ever passing
    # through in_progress=True. The "no upcoming event" path is the only
    # other place besides the normal draw that can carry the LED field,
    # via an explicit flush draw before clear().
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    state: dict = {}
    imminent = make_event(0.5)
    run_once(client, lambda hours: [imminent], CFG, NOW, dry_run=False, state=state)
    assert state["led_on"] is True
    client.draw.reset_mock()

    later = NOW + timedelta(seconds=5)
    summary = run_once(client, lambda hours: [], CFG, later, dry_run=False, state=state)
    assert "cleared" in summary
    # An explicit LED-off flush draw must have happened (a real draw()
    # call carrying the off value), separate from -- and before -- clear().
    client.draw.assert_called_once()
    flush_kwargs = client.draw.call_args.kwargs
    assert flush_kwargs["led_notification_color"] is not None
    assert flush_kwargs["led_notification_color"] != IMMINENT_LED_COLOR
    client.clear.assert_called_once_with("calendar_countdown")
    assert state["led_on"] is False

def test_run_once_no_led_flush_when_led_was_already_off_and_event_vanishes():
    # No spurious flush draw when the LED wasn't on to begin with.
    client = Mock()
    state: dict = {}
    run_once(client, lambda hours: [make_event(40)], CFG, NOW, dry_run=False, state=state)   # normal, no LED
    assert state.get("led_on", False) is False
    client.draw.reset_mock(); client.clear.reset_mock()

    run_once(client, lambda hours: [], CFG, NOW, dry_run=False, state=state)
    client.draw.assert_not_called()   # no flush needed
    client.clear.assert_called_once_with("calendar_countdown")

def test_run_once_led_flush_retries_next_poll_if_it_fails():
    client = Mock()
    state: dict = {}
    client.draw.return_value = DrawResult.DRAWN
    imminent = make_event(0.5)
    run_once(client, lambda hours: [imminent], CFG, NOW, dry_run=False, state=state)
    assert state["led_on"] is True

    client.draw.return_value = DrawResult.REJECTED   # the flush attempt fails
    later = NOW + timedelta(seconds=5)
    run_once(client, lambda hours: [], CFG, later, dry_run=False, state=state)
    assert state["led_on"] is True   # not committed -- must retry

    client.draw.return_value = DrawResult.DRAWN
    run_once(client, lambda hours: [], CFG, later + timedelta(seconds=5), dry_run=False, state=state)
    assert state["led_on"] is False   # retried and landed


# --- v1.5.2 chirp, end to end through run_once ------------------------------------

def test_run_once_chirps_exactly_once_on_start_transition():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    client.play_audio.return_value = True
    state: dict = {}
    upcoming = make_event(0.2)   # about to start
    run_once(client, lambda hours: [upcoming], CFG, NOW, dry_run=False, state=state)
    client.play_audio.assert_not_called()   # still upcoming -- no chirp yet

    # Same event's start timestamp must line up for edge detection -- build
    # the "started" version from the original event's own start directly,
    # not a fresh make_event() call (which would compute a different start).
    started = CalEvent(upcoming.title, upcoming.start, upcoming.start + timedelta(minutes=30), False)
    later = upcoming.start + timedelta(seconds=1)
    run_once(client, lambda hours: [started], CFG, later, dry_run=False, state=state)
    client.play_audio.assert_called_once_with("calendar_countdown", stock_path=CHIRP_STOCK_PATH)

    # A further poll, still in_progress, must not re-chirp.
    client.play_audio.reset_mock()
    run_once(client, lambda hours: [started], CFG, later + timedelta(seconds=10), dry_run=False, state=state)
    client.play_audio.assert_not_called()

def test_run_once_chirp_logs_info_on_successful_play(caplog):
    # v1.5.2.1 observability fix: a "successful" (True-returning) chirp
    # attempt must leave a log trace -- the silent-.wav bug was doubly
    # silent (no sound AND no log line) precisely because success wasn't
    # logged at all, only failures were (in client.play_audio itself).
    import logging
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    client.play_audio.return_value = True
    state: dict = {}
    upcoming = make_event(0.2)
    run_once(client, lambda hours: [upcoming], CFG, NOW, dry_run=False, state=state)
    started = CalEvent(upcoming.title, upcoming.start, upcoming.start + timedelta(minutes=30), False)
    later = upcoming.start + timedelta(seconds=1)
    with caplog.at_level(logging.INFO, logger="calendar_countdown"):
        run_once(client, lambda hours: [started], CFG, later, dry_run=False, state=state)
    matches = [rec.message for rec in caplog.records if "chirp played" in rec.message]
    assert len(matches) == 1
    assert CHIRP_STOCK_PATH in matches[0]
    assert "True" in matches[0]

def test_run_once_chirp_logs_info_on_failed_play(caplog):
    import logging
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    client.play_audio.return_value = False
    state: dict = {}
    upcoming = make_event(0.2)
    run_once(client, lambda hours: [upcoming], CFG, NOW, dry_run=False, state=state)
    started = CalEvent(upcoming.title, upcoming.start, upcoming.start + timedelta(minutes=30), False)
    later = upcoming.start + timedelta(seconds=1)
    with caplog.at_level(logging.INFO, logger="calendar_countdown"):
        run_once(client, lambda hours: [started], CFG, later, dry_run=False, state=state)
    matches = [rec.message for rec in caplog.records if "chirp played" in rec.message]
    assert len(matches) == 1
    assert "False" in matches[0]

def test_run_once_chirp_disabled_never_fires():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    client.play_audio.return_value = True
    cfg = {"calendar_countdown": {**CFG["calendar_countdown"], "chirp": False}}
    state: dict = {}
    upcoming = make_event(0.2)
    run_once(client, lambda hours: [upcoming], cfg, NOW, dry_run=False, state=state)
    started = CalEvent(upcoming.title, upcoming.start, upcoming.start + timedelta(minutes=30), False)
    later = upcoming.start + timedelta(seconds=1)
    run_once(client, lambda hours: [started], cfg, later, dry_run=False, state=state)
    client.play_audio.assert_not_called()

def test_run_once_restart_mid_event_does_not_chirp():
    # Fresh state dict (as if the process just started) whose very first
    # poll already finds the event in_progress -- no chirp, matching
    # should_chirp's documented restart-safety edge case.
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    client.play_audio.return_value = True
    state: dict = {}
    active = make_event(-2, dur_min=30, title="Active")
    run_once(client, lambda hours: [active], CFG, NOW, dry_run=False, state=state)
    client.play_audio.assert_not_called()

def test_run_once_chirp_does_not_replay_after_unconfirmed_play():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    client.play_audio.return_value = False   # response may be lost after playback
    state: dict = {}
    upcoming = make_event(0.2)
    run_once(client, lambda hours: [upcoming], CFG, NOW, dry_run=False, state=state)
    started = CalEvent(upcoming.title, upcoming.start, upcoming.start + timedelta(minutes=30), False)
    later = upcoming.start + timedelta(seconds=1)
    run_once(client, lambda hours: [started], CFG, later, dry_run=False, state=state)
    assert client.play_audio.call_count == 1

    # A missing response is not evidence that the sound did not play.
    client.play_audio.return_value = True
    run_once(client, lambda hours: [started], CFG, later + timedelta(seconds=5), dry_run=False, state=state)
    assert client.play_audio.call_count == 1

def test_run_once_dry_run_never_chirps():
    client = Mock()
    state: dict = {}
    upcoming = make_event(0.2)
    run_once(client, lambda hours: [upcoming], CFG, NOW, dry_run=True, state=state)
    started = CalEvent(upcoming.title, upcoming.start, upcoming.start + timedelta(minutes=30), False)
    later = upcoming.start + timedelta(seconds=1)
    run_once(client, lambda hours: [started], CFG, later, dry_run=True, state=state)
    client.play_audio.assert_not_called()


# --- state["next_start"] bookkeeping (feeds main()'s sleep-shortening) -----------

def test_run_once_records_next_start_for_upcoming_event():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    state: dict = {}
    event = make_event(23)
    run_once(client, lambda hours: [event], CFG, NOW, dry_run=False, state=state)
    assert state["next_start"] == event.start

def test_run_once_next_start_none_when_in_progress():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    state: dict = {}
    active = make_event(-5, dur_min=30, title="Active")
    run_once(client, lambda hours: [active], CFG, NOW, dry_run=False, state=state)
    assert state["next_start"] is None

def test_run_once_next_start_none_when_no_event():
    client = Mock()
    state: dict = {}
    run_once(client, lambda hours: [], CFG, NOW, dry_run=False, state=state)
    assert state["next_start"] is None


# --- v1.6 stock-animation accents: just_started takeover + shape-tracker clear ---
#
# NOTE: this block's own `NOW` fixture is named `TAKEOVER_NOW`, not `NOW` --
# the module already defines `NOW` above (2026-08-03 13:37 UTC, read by
# every earlier test via make_event/CFG at call time), so reusing that name
# here would silently reassign it at import time and change every earlier
# test's clock.

from busybar.client import DrawResult
from busybar.display import PRIORITY_AMBIENT_URGENT
from integrations.calendar_countdown.main import run_once
from integrations.calendar_countdown.logic import CalEvent, START_ANIM_ID

class FakeClient:
    def __init__(self): self.draws=[]; self.clears=0
    def draw(self, app, elements, priority=50, led_notification_color=None):
        self.draws.append((elements, priority)); return DrawResult.DRAWN
    def clear(self, app): self.clears += 1; return True
    def get_busy(self): return {}
    def play_audio(self, *a, **k): return True
    def set_busy_simple(self, *a, **k): return True

TAKEOVER_NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
def _cfg(**o):
    b={"poll_seconds":10,"lookahead_hours":12,"warn_minutes":5,"notice_minutes":15,
       "approach_minutes":30,"imminent_minutes":1,"progress_window_minutes":60,"include_all_day":False,
       "auto_busy":False,"calendars":[],"chirp":False,"escalation_icons":True,
       "start_animation":"meeting_72x16","start_window_seconds":60}
    b.update(o); return {"calendar_countdown": b}
def _fetch(ev):  # fetch(lookahead) -> [ev]
    return lambda hours: [ev]

def test_just_started_draws_takeover_at_urgent():
    ev = CalEvent("Standup", TAKEOVER_NOW - timedelta(seconds=15), TAKEOVER_NOW + timedelta(minutes=29), False)
    c = FakeClient(); st = {}
    run_once(c, _fetch(ev), _cfg(), TAKEOVER_NOW, dry_run=False, state=st)
    elements, priority = c.draws[-1]
    assert priority == PRIORITY_AMBIENT_URGENT
    assert any(e["id"] == START_ANIM_ID for e in elements)

def test_shape_change_triggers_clear():
    # First poll: warn stage (icon+title). Second poll: takeover (different id-set) -> clear.
    ev = CalEvent("Standup", TAKEOVER_NOW + timedelta(minutes=4), TAKEOVER_NOW + timedelta(minutes=34), False)
    c = FakeClient(); st = {}
    run_once(c, _fetch(ev), _cfg(), TAKEOVER_NOW, dry_run=False, state=st)            # warn
    ev2 = CalEvent("Standup", TAKEOVER_NOW - timedelta(seconds=10), TAKEOVER_NOW + timedelta(minutes=29), False)
    run_once(c, _fetch(ev2), _cfg(), TAKEOVER_NOW, dry_run=False, state=st)          # takeover
    assert c.clears >= 1   # id-set changed -> cleared before the takeover draw


# --- v1.6.1 start-takeover graceful degradation on a bad start_animation ---------
#
# A mistyped start_animation names a stock animation the device doesn't have,
# so the live device rejects the full-panel takeover draw with
# DrawResult.ERROR every poll. Without the fallback in main.run_once, `state`
# never commits and the panel stays DARK for the whole start_window_seconds
# (the takeover is the ONLY thing on screen). The fallback redraws the normal
# in-progress "ENDS" layout for that poll instead. These tests drive a
# FakeClient that returns a chosen DrawResult for the takeover draw (the one
# carrying START_ANIM_ID) and DRAWN for everything else.

IN_PROGRESS_SHAPE = frozenset({"bg", "title", "track", "track_fill", "track_tip", "ends", "divider", "cd_text"})


class ResultFakeClient:
    def __init__(self, takeover_result):
        self.takeover_result = takeover_result
        self.draws = []; self.clears = 0
    def draw(self, app, elements, priority=50, led_notification_color=None):
        self.draws.append((elements, priority))
        if any(e["id"] == START_ANIM_ID for e in elements):
            return self.takeover_result
        return DrawResult.DRAWN
    def clear(self, app): self.clears += 1; return True
    def get_busy(self): return {}
    def play_audio(self, *a, **k): return True
    def set_busy_simple(self, *a, **k): return True


def _just_started_event():
    # In-progress and within start_window_seconds (60) -> just_started True.
    return CalEvent("Standup", TAKEOVER_NOW - timedelta(seconds=15),
                    TAKEOVER_NOW + timedelta(minutes=29), False)


def test_start_takeover_error_falls_back_to_in_progress():
    c = ResultFakeClient(DrawResult.ERROR); st = {}
    summary = run_once(c, _fetch(_just_started_event()), _cfg(), TAKEOVER_NOW,
                       dry_run=False, state=st)
    # Exactly two draws: the failed takeover, then the in-progress fallback.
    assert len(c.draws) == 2
    takeover_elements, takeover_priority = c.draws[0]
    assert takeover_priority == PRIORITY_AMBIENT_URGENT
    assert any(e["id"] == START_ANIM_ID for e in takeover_elements)
    fallback_elements, fallback_priority = c.draws[1]
    assert fallback_priority == PRIORITY_AMBIENT     # in-progress baseline, not urgent
    fb_ids = frozenset(e["id"] for e in fallback_elements)
    assert START_ANIM_ID not in fb_ids
    assert fb_ids == IN_PROGRESS_SHAPE
    # Fallback landed (DRAWN) -> state commits the in-progress shape, so the
    # window-exit poll sees no shape change and doesn't re-clear.
    assert st["last_shape"] == IN_PROGRESS_SHAPE
    assert "fallback" in summary and summary.endswith("drawn")


def test_start_takeover_rejected_does_not_fall_back():
    # REJECTED (a strictly-higher-priority app owns the screen) must NOT
    # trigger the fallback: the in-progress layout draws at a LOWER priority
    # and would be rejected too -- a pointless second draw. Only ERROR falls
    # back.
    c = ResultFakeClient(DrawResult.REJECTED); st = {}
    summary = run_once(c, _fetch(_just_started_event()), _cfg(), TAKEOVER_NOW,
                       dry_run=False, state=st)
    assert len(c.draws) == 1                         # takeover only, no fallback draw
    assert any(e["id"] == START_ANIM_ID for e in c.draws[0][0])
    assert st.get("last_shape") is None              # not DRAWN -> not committed, retries next poll
    assert summary.endswith("rejected")


def test_start_takeover_unreachable_does_not_fall_back():
    # UNREACHABLE (device down) is handled by main()'s backoff loop, not by a
    # second (equally-unreachable) draw.
    c = ResultFakeClient(DrawResult.UNREACHABLE); st = {}
    summary = run_once(c, _fetch(_just_started_event()), _cfg(), TAKEOVER_NOW,
                       dry_run=False, state=st)
    assert len(c.draws) == 1
    assert st.get("last_shape") is None
    assert summary.endswith("unreachable")


def test_start_takeover_fallback_is_per_poll_not_latched():
    # Not latched: once the operator fixes the config value (or the stock
    # animation later appears), the very next poll draws the takeover again
    # with no restart -- matching the module's retry-not-assume discipline.
    c = ResultFakeClient(DrawResult.ERROR); st = {}
    run_once(c, _fetch(_just_started_event()), _cfg(), TAKEOVER_NOW,
             dry_run=False, state=st)                # poll 1: falls back
    assert st["last_shape"] == IN_PROGRESS_SHAPE

    c.takeover_result = DrawResult.DRAWN             # animation now drawable
    c.draws.clear()
    later = TAKEOVER_NOW + timedelta(seconds=10)     # still inside the 60s window
    run_once(c, _fetch(_just_started_event()), _cfg(), later, dry_run=False, state=st)
    assert len(c.draws) == 1                         # takeover, drawn straight away
    assert any(e["id"] == START_ANIM_ID for e in c.draws[0][0])
    assert st["last_shape"] == frozenset({"bg", START_ANIM_ID})


def test_modern_calendar_layers_and_same_shape_priority_stepdown():
    client = Mock()
    client.supports_display_v2 = True
    client.draw.return_value = DrawResult.DRAWN
    state = {}
    run_once(client, lambda _: [make_event(3)], CFG, NOW, False, state)
    assert state["last_priority"] == 65
    # A rescheduled event has the same IDs but a lower priority. Without a
    # scoped clear, the firmware rejects this same-app priority reduction.
    run_once(client, lambda _: [make_event(40)], CFG, NOW, False, state)
    client.clear.assert_called_once_with("calendar_countdown")
    assert state["last_priority"] == 20
    elements = client.draw.call_args.kwargs["elements"]
    assert [e["z_index"] for e in elements] == list(range(0, len(elements) * 10, 10))


def test_auto_busy_requires_confirmed_nested_idle_snapshot():
    cfg = {"calendar_countdown": {**CFG["calendar_countdown"], "auto_busy": True}}
    client = Mock()
    client.draw.return_value = DrawResult.DRAWN
    event = make_event(-5)
    for response in (None, {}, {"snapshot": None}, {"snapshot": {"type": "SIMPLE"}}):
        client.get_busy.return_value = response
        run_once(client, lambda _: [event], cfg, NOW, False)
    client.set_busy_simple.assert_not_called()
    client.get_busy.return_value = {"snapshot": {"type": "NOT_STARTED"}, "timestamp": 1}
    run_once(client, lambda _: [event], cfg, NOW, False)
    client.set_busy_simple.assert_called_once_with(25 * 60 * 1000)
