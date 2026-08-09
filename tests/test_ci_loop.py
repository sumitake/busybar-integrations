from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations"))
from busybar.client import DrawResult
from busybar.display import PRIORITY_OVERLAY, OVERLAY_DWELL_SECONDS
from ci_status.logic import RepoState
from ci_status.main import run_once, next_poll_seconds, config_requires_repos

NOW = datetime(2026, 8, 3, 13, 37, tzinfo=timezone.utc)
CFG = {"ci_status": {"poll_seconds": 120, "repos": ["o/r"],
                     "show_green": False, "stale_queued_minutes": 0}}
# Full config including the v1.5 running-badge keys, for tests that exercise
# that path (the bare CFG above deliberately predates those keys, to prove
# run_once stays backward compatible with callers/configs that omit them --
# see test_running_detection_skipped_when_running_cache_omitted).
CFG_RUNNING = {"ci_status": {"poll_seconds": 120, "running_poll_seconds": 20,
                             "repos": ["o/r"], "show_green": False,
                             "stale_queued_minutes": 0, "show_running": True,
                             "show_quota": False}}
CFG_QUOTA = {"ci_status": {**CFG_RUNNING["ci_status"], "show_quota": True}}
CFG_GREEN = {"ci_status": {**CFG_RUNNING["ci_status"], "show_green": True}}


def _run(conclusion: str) -> dict:
    return {"workflow_id": 1, "name": "tests", "status": "completed",
            "conclusion": conclusion, "created_at": "2026-08-03T13:30:00Z"}


def _drawn_text(client) -> str:
    els = client.draw.call_args.kwargs.get("elements") or client.draw.call_args.args[1]
    return next(e["text"] for e in els if e.get("type") == "text")


def test_draws_red_on_failure():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = [_run("failure")]
    summary = run_once(client, poller, CFG, NOW, {}, dry_run=False)
    assert client.draw.call_args.kwargs["priority"] == PRIORITY_OVERLAY  # 21, not 60
    assert client.draw.call_args.kwargs["led_notification_color"] == "#FF0000FF"
    assert "FAIL" in summary


def test_failure_rotates_with_no_running_job():
    # No running_cache at all: a failure must still draw (Request B) -- the old
    # code drew a priority-60 alert here; now it's an overlay-tier frame.
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = [_run("failure")]
    run_once(client, poller, CFG, NOW, {}, dry_run=False, overlay_state={})
    assert client.draw.call_args.kwargs["priority"] == PRIORITY_OVERLAY


def test_failure_frame_respects_dwell_silence():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = [_run("failure")]
    overlay_state: dict = {}
    run_once(client, poller, CFG, NOW, {}, dry_run=False, overlay_state=overlay_state)
    client.draw.assert_called_once()
    client.reset_mock()
    soon = NOW + timedelta(seconds=OVERLAY_DWELL_SECONDS - 1)
    run_once(client, poller, CFG, soon, {}, dry_run=False, overlay_state=overlay_state)
    client.draw.assert_not_called(); client.clear.assert_not_called()


def test_led_turns_off_explicitly_when_failure_clears():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    overlay_state: dict = {}
    # Poll 1: failing -> LED red, led_was_on committed True
    poller.fetch_runs.return_value = [_run("failure")]
    run_once(client, poller, CFG, NOW, {}, dry_run=False, overlay_state=overlay_state)
    assert overlay_state["led_was_on"] is True
    client.reset_mock()
    # Poll 2: now green, nothing else to draw -> explicit off via LED_OFF_ELEMENTS
    poller.fetch_runs.return_value = [_run("success")]
    later = NOW + timedelta(seconds=OVERLAY_DWELL_SECONDS + 1)
    run_once(client, poller, CFG, later, {}, dry_run=False, overlay_state=overlay_state)
    assert client.draw.call_args.kwargs["led_notification_color"] == "#00000000"
    assert overlay_state["led_was_on"] is False


def test_led_stays_off_omitted_when_already_clear():
    client = Mock()
    poller = Mock(); poller.fetch_runs.return_value = [_run("success")]
    run_once(client, poller, CFG, NOW, {}, dry_run=False, overlay_state={})
    client.clear.assert_called_once_with("ci_status")  # nothing to draw, LED never was on
    client.draw.assert_not_called()


def test_green_folds_into_rotation_at_overlay_tier():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = [_run("success")]
    cfg = {"ci_status": {**CFG["ci_status"], "show_green": True}}
    run_once(client, poller, cfg, NOW, {}, dry_run=False, overlay_state={})
    assert client.draw.call_args.kwargs["priority"] == PRIORITY_OVERLAY
    assert "ok" in _drawn_text(client)


def test_clears_when_green():
    client = Mock()
    poller = Mock(); poller.fetch_runs.return_value = [_run("success")]
    run_once(client, poller, CFG, NOW, {}, dry_run=False)
    client.clear.assert_called_once_with("ci_status")


def test_304_keeps_previous_state():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = None  # 304 / error
    cache = {}
    # Seed cache via an initial failing poll, then a 304 poll must still draw red.
    poller_seed = Mock(); poller_seed.fetch_runs.return_value = [_run("failure")]
    run_once(client, poller_seed, CFG, NOW, cache, dry_run=False)
    client.reset_mock(); client.draw.return_value = DrawResult.DRAWN
    run_once(client, poller, CFG, NOW, cache, dry_run=False)
    client.draw.assert_called_once()


def test_dry_run_touches_nothing():
    client = Mock()
    poller = Mock(); poller.fetch_runs.return_value = [_run("failure")]
    summary = run_once(client, poller, CFG, NOW, {}, dry_run=True)
    client.draw.assert_not_called(); client.clear.assert_not_called()
    assert "DRY-RUN" in summary


# --- running badge wiring -------------------------------------------------------

def _running_run(started_min_ago: float = 3, workflow_id: int = 1, name: str = "tests") -> dict:
    started = (NOW - timedelta(minutes=started_min_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"workflow_id": workflow_id, "name": name, "status": "in_progress",
           "run_started_at": started, "head_branch": "main", "pull_requests": []}


def test_draws_running_badge_at_priority_21_when_run_active():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]   # no failure/stuck
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = 10.0
    running_cache: dict = {}
    run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False, running_cache=running_cache)
    # 21 (PRIORITY_OVERLAY), not the literal priority=20 the feature brief
    # specified -- see busybar/display.py's PRIORITY_OVERLAY docstring for
    # the empirical (probe-verified) reason a strictly-higher priority is
    # required.
    assert client.draw.call_args.kwargs["priority"] == PRIORITY_OVERLAY == 21
    assert running_cache["o/r"] == [_running_run()]

def test_running_badge_fetches_median_for_selected_runs_workflow():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run(workflow_id=99)]
    poller.fetch_median_eta.return_value = None
    run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False, running_cache={})
    poller.fetch_median_eta.assert_called_once_with("o/r", 99)

def test_no_running_badge_when_show_running_false():
    cfg = {"ci_status": {**CFG_RUNNING["ci_status"], "show_running": False}}
    client = Mock()
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    run_once(client, poller, cfg, NOW, {}, dry_run=False, running_cache={})
    poller.fetch_running_runs.assert_not_called()
    client.clear.assert_called_once_with("ci_status")   # falls through to "all green"

def test_running_detection_skipped_when_running_cache_omitted():
    # Backward compatible: a caller (or an older-shaped cfg dict, like the
    # bare CFG above) that doesn't pass running_cache never touches
    # show_running/running_poll_seconds/show_quota -- no KeyError even
    # though CFG predates those keys.
    client = Mock()
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    summary = run_once(client, poller, CFG, NOW, {}, dry_run=False)
    poller.fetch_running_runs.assert_not_called()
    assert "cleared" in summary

def test_without_overlay_state_gate_always_open_draws_every_poll():
    # overlay_state omitted entirely -- run_once can't remember a previous
    # dwell, so the gate can't meaningfully close; every poll with an
    # active run draws. (This is the pre-dwell-gate behavior, preserved
    # for callers that don't care about the alternation mechanics.)
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False, running_cache={})
    run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False, running_cache={})
    assert client.draw.call_count == 2


# --- overlay dwell gate ----------------------------------------------------------

def test_first_overlay_draw_is_never_gated():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    summary = run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False,
                       running_cache={}, overlay_state={})
    client.draw.assert_called_once()
    assert "silent" not in summary

def test_second_poll_within_dwell_stays_silent():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    overlay_state: dict = {}
    run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state)
    client.draw.reset_mock()
    soon_after = NOW + timedelta(seconds=OVERLAY_DWELL_SECONDS - 1)
    summary = run_once(client, poller, CFG_RUNNING, soon_after, {}, dry_run=False,
                       running_cache={}, overlay_state=overlay_state)
    client.draw.assert_not_called()
    client.clear.assert_not_called()
    assert "silent" in summary

def test_poll_after_dwell_elapsed_draws_again():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    overlay_state: dict = {}
    run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state)
    client.draw.reset_mock()
    later = NOW + timedelta(seconds=2 * OVERLAY_DWELL_SECONDS + 1)
    run_once(client, poller, CFG_RUNNING, later, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state)
    client.draw.assert_called_once()

def test_dwell_state_does_not_commit_on_failed_draw():
    # If draw() doesn't land, overlay_state must not advance -- otherwise
    # the next poll would wait a full dwell for a "dwell" that never
    # actually rendered anything (same discipline as calendar_countdown's
    # transition-state DRAWN gate).
    client = Mock()
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    overlay_state: dict = {}

    client.draw.return_value = DrawResult.UNREACHABLE
    run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state)
    assert "last_dwell_end" not in overlay_state

    client.draw.return_value = DrawResult.DRAWN
    soon_after = NOW + timedelta(seconds=1)
    run_once(client, poller, CFG_RUNNING, soon_after, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state)
    # No dwell was ever successfully committed, so this immediate retry
    # (only 1s later) must still draw, not be gated.
    assert client.draw.call_count == 2

def test_overlay_state_resets_when_run_ends():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    overlay_state: dict = {}
    run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state)
    assert overlay_state.get("last_dwell_end") is not None

    poller.fetch_running_runs.return_value = []   # run finished
    run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False,
            running_cache={"o/r": [_running_run()]}, overlay_state=overlay_state)
    # Only the rotation bookkeeping (frame_index/last_dwell_end) resets
    # here -- the "run ended, nothing else to show" branch happens to
    # also reach the explicit client.clear()/"all green" path this same
    # poll (show_green is False in CFG_RUNNING), so last_shape correctly
    # becomes None too: the device really is blank now, this poll. The
    # critical-bug regression coverage -- last_shape surviving a
    # bookkeeping-only reset that does NOT clear the device this same
    # poll (e.g. an alert preempting the overlay without falling through
    # to the "nothing to show" branch) -- lives in
    # test_overlay_then_alert_clears_stale_overlay_shape below.
    assert "frame_index" not in overlay_state
    assert "last_dwell_end" not in overlay_state
    assert overlay_state["last_shape"] is None


# --- unified shape tracking across alert / quiet-green / overlay tiers ----------

def test_overlay_then_alert_clears_stale_overlay_shape():
    # Running badge draws first (shape {bg,title,track,track_fill,eta});
    # the next poll turns up a failure. Once the rotation's next dwell slot
    # lands on the failure frame (shape {bg,ci}), the stale
    # title/track/track_fill/eta ink from the badge must be cleared first --
    # not left to linger until its own ~1.5x-poll timeout. Both the failure
    # frame and the running badge are now part of the SAME dwell-gated
    # rotation (Request B), so `frame_index` is pinned to 0 to deterministically
    # land on the (newly added) failure slot rather than depend on where the
    # rotation's cursor happens to sit after the composition changed.
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    overlay_state: dict = {}
    run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state)
    client.clear.assert_not_called()   # nothing on screen before -- no clear needed yet

    poller.fetch_runs.return_value = [_run("failure")]
    overlay_state["frame_index"] = 0
    later = NOW + timedelta(seconds=2 * OVERLAY_DWELL_SECONDS + 1)
    summary = run_once(client, poller, CFG_RUNNING, later, {}, dry_run=False,
                       running_cache={}, overlay_state=overlay_state)
    client.clear.assert_called_once_with("ci_status")
    assert "FAIL" in summary
    assert client.draw.call_args.kwargs["priority"] == PRIORITY_OVERLAY

def test_alert_then_overlay_clears_stale_alert_shape():
    # Symmetric direction: an alert draws first (shape {bg,ci}); once it
    # resolves and a run is active, the running badge's shape ({bg,title,
    # track,track_fill,eta}) differs and must clear the alert's stale
    # elements first. The second poll must land at or beyond a full dwell
    # (Request B: the failure frame is dwell-gated like every other overlay
    # frame now, unlike the old unconditionally-drawing alert-tier path).
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("failure")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    overlay_state: dict = {}
    run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state)
    client.clear.assert_not_called()   # first-ever draw -- nothing to clear yet
    assert client.draw.call_args.kwargs["priority"] == PRIORITY_OVERLAY

    poller.fetch_runs.return_value = [_run("success")]   # alert resolves
    later = NOW + timedelta(seconds=2 * OVERLAY_DWELL_SECONDS + 1)
    run_once(client, poller, CFG_RUNNING, later, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state)
    client.clear.assert_called_once_with("ci_status")

def test_quiet_green_then_overlay_clears_stale_green_shape():
    # Quiet "CI ok" text (shape {ci}, no bg) draws first when show_green
    # is on and nothing is running; once a run starts, the badge's shape
    # differs (it has a bg + several more ids) and must clear first, or
    # the old green text -- drawn with a ~1.5x-poll timeout, e.g. 180s at
    # the default -- would linger behind/around the badge for minutes.
    # Green now draws at the overlay tier too, so the second poll must
    # land at or beyond a full dwell for its draw to even be attempted.
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = []   # nothing running yet
    overlay_state: dict = {}
    summary = run_once(client, poller, CFG_GREEN, NOW, {}, dry_run=False,
                       running_cache={}, overlay_state=overlay_state)
    client.clear.assert_not_called()
    assert overlay_state["last_shape"] == frozenset({"ci"})
    assert client.draw.call_args.kwargs["priority"] == PRIORITY_OVERLAY

    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    later = NOW + timedelta(seconds=2 * OVERLAY_DWELL_SECONDS + 1)
    run_once(client, poller, CFG_GREEN, later, {}, dry_run=False,
            running_cache={"o/r": []}, overlay_state=overlay_state)
    client.clear.assert_called_once_with("ci_status")


# --- overlay rotation (quota frames) ---------------------------------------------

def _quota_body(gql_remaining=2600, core_remaining=100):
    return {"resources": {
        "core": {"limit": 5000, "remaining": core_remaining, "reset": 2000000000, "used": 5000 - core_remaining},
        "graphql": {"limit": 5000, "remaining": gql_remaining, "reset": 2000000000, "used": 5000 - gql_remaining},
    }}

def test_rotation_cycles_ci_badge_then_quota_frames():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    poller.fetch_rate_limit.return_value = _quota_body()
    overlay_state: dict = {}
    quota_cache: dict = {}
    seen = []
    t = NOW
    for _ in range(3):
        run_once(client, poller, CFG_QUOTA, t, {}, dry_run=False,
                running_cache={}, overlay_state=overlay_state, quota_cache=quota_cache)
        elements = client.draw.call_args.args[1]   # elements is positional, not a kwarg
        by_id = {e["id"]: e for e in elements}
        if "eta" in by_id:
            seen.append("ci_badge")
        else:
            seen.append("quota_gql" if by_id["title"]["text"] == "GITHUB GRAPHQL" else "quota_rest")
        t += timedelta(seconds=2 * OVERLAY_DWELL_SECONDS + 1)
    assert seen == ["ci_badge", "quota_gql", "quota_rest"]

def test_rotation_shape_change_clears_first():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    poller.fetch_rate_limit.return_value = _quota_body()
    overlay_state: dict = {}
    quota_cache: dict = {}
    # First dwell: ci_badge (shape "badge") -- no prior shape, no clear.
    run_once(client, poller, CFG_QUOTA, NOW, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state, quota_cache=quota_cache)
    client.clear.assert_not_called()
    # Second dwell: quota_gql (shape "quota") -- shape changed, must clear first.
    later = NOW + timedelta(seconds=2 * OVERLAY_DWELL_SECONDS + 1)
    run_once(client, poller, CFG_QUOTA, later, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state, quota_cache=quota_cache)
    client.clear.assert_called_once_with("ci_status")
    # Third dwell: quota_rest -- same shape as quota_gql, no clear needed.
    client.clear.reset_mock()
    later2 = later + timedelta(seconds=2 * OVERLAY_DWELL_SECONDS + 1)
    run_once(client, poller, CFG_QUOTA, later2, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state, quota_cache=quota_cache)
    client.clear.assert_not_called()

def test_quota_frame_skipped_without_crashing_when_fetch_fails():
    # No successful rate_limit fetch ever (and quota_cache starts empty),
    # so _refresh_quota keeps returning None -- build_overlay_sequence never
    # includes a quota frame in the rotation at all (it only folds in frames
    # whose data is already on hand), so the CI badge just keeps redrawing
    # in its place, no crash, no stale/placeholder quota numbers ever shown.
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    poller.fetch_rate_limit.return_value = None   # fetch fails, no cached data ever
    overlay_state: dict = {}
    quota_cache: dict = {}
    run_once(client, poller, CFG_QUOTA, NOW, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state, quota_cache=quota_cache)   # ci_badge
    later = NOW + timedelta(seconds=2 * OVERLAY_DWELL_SECONDS + 1)
    summary = run_once(client, poller, CFG_QUOTA, later, {}, dry_run=False,
                       running_cache={}, overlay_state=overlay_state, quota_cache=quota_cache)
    assert client.draw.call_count == 2   # ci_badge drawn again -- no quota frame ever entered rotation
    assert "eta" in {e["id"] for e in client.draw.call_args.args[1]}
    assert "drawn" in summary

def test_quota_stale_data_not_shown_after_5_minutes():
    # Cached quota data older than QUOTA_STALE_SECONDS is treated as
    # unavailable by _refresh_quota -- same as a fetch failure -- so it
    # never surfaces in the rotation; the CI badge draws again instead of
    # showing minutes-old numbers.
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    overlay_state: dict = {}
    quota_cache: dict = {"buckets": {"graphql": {"limit": 5000, "remaining": 2600, "used": 2400, "reset": 2000000000}},
                         "fetched_at": NOW - timedelta(minutes=6)}   # stale
    poller.fetch_rate_limit.return_value = None   # this poll's fetch also fails
    run_once(client, poller, CFG_QUOTA, NOW, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state, quota_cache=quota_cache)   # ci_badge dwell
    later = NOW + timedelta(seconds=2 * OVERLAY_DWELL_SECONDS + 1)
    run_once(client, poller, CFG_QUOTA, later, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state, quota_cache=quota_cache)
    assert client.draw.call_count == 2   # ci_badge drawn again -- stale quota data never shown
    assert "eta" in {e["id"] for e in client.draw.call_args.args[1]}


# --- cadence switch (next_poll_seconds) -----------------------------------------

def test_next_poll_seconds_shortens_while_a_run_is_active():
    running_cache = {"o/r": [_running_run()]}
    assert next_poll_seconds(CFG_RUNNING["ci_status"], running_cache) == 20

def test_next_poll_seconds_reverts_when_idle():
    running_cache = {"o/r": []}
    assert next_poll_seconds(CFG_RUNNING["ci_status"], running_cache) == 120

def test_next_poll_seconds_reverts_when_repo_never_polled():
    assert next_poll_seconds(CFG_RUNNING["ci_status"], {}) == 120

def test_next_poll_seconds_checks_across_all_configured_repos():
    cfg = {**CFG_RUNNING["ci_status"], "repos": ["o/r1", "o/r2"]}
    running_cache = {"o/r1": [], "o/r2": [_running_run()]}
    assert next_poll_seconds(cfg, running_cache) == 20


# --- account-wide repo watching (v1.5.1) -----------------------------------------

CFG_ACCOUNT = {"ci_status": {**CFG_RUNNING["ci_status"], "watch_account_repos": True,
                             "repos": [], "repos_exclude": [], "active_within_days": 30,
                             "repo_refresh_minutes": 60}}

def _account_repo(full_name, pushed_days_ago=1, archived=False):
    pushed = (NOW - timedelta(days=pushed_days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"full_name": full_name, "archived": archived, "pushed_at": pushed}

def test_account_mode_polls_discovered_repos():
    client = Mock()
    poller = Mock()
    poller.fetch_account_repos.return_value = [_account_repo("o/discovered")]
    poller.fetch_runs.return_value = [_run("success")]
    repo_cache: dict = {}
    run_once(client, poller, CFG_ACCOUNT, NOW, {}, dry_run=False, repo_cache=repo_cache)
    poller.fetch_runs.assert_called_once_with("o/discovered")

def test_account_mode_off_never_calls_discovery():
    client = Mock()
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    repo_cache: dict = {}
    cfg = {"ci_status": {**CFG_RUNNING["ci_status"], "watch_account_repos": False}}
    run_once(client, poller, cfg, NOW, {}, dry_run=False, repo_cache=repo_cache)
    poller.fetch_account_repos.assert_not_called()

def test_repo_cache_omitted_falls_back_to_pre_v1_5_1_behavior():
    # No repo_cache passed at all -- exactly cfg["ci_status"]["repos"] is
    # polled, discovery never runs, even with watch_account_repos true.
    client = Mock()
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    cfg = {"ci_status": {**CFG_ACCOUNT["ci_status"], "repos": ["o/explicit"]}}
    run_once(client, poller, cfg, NOW, {}, dry_run=False)
    poller.fetch_account_repos.assert_not_called()
    poller.fetch_runs.assert_called_once_with("o/explicit")


# --- account repo list refresh timing ---------------------------------------------

def test_stale_repo_cache_re_enumerates():
    client = Mock()
    poller = Mock()
    poller.fetch_account_repos.return_value = [_account_repo("o/a")]
    poller.fetch_runs.return_value = [_run("success")]
    repo_cache: dict = {"repos": [_account_repo("o/old")],
                        "fetched_at": NOW - timedelta(minutes=61)}   # older than 60min default
    run_once(client, poller, CFG_ACCOUNT, NOW, {}, dry_run=False, repo_cache=repo_cache)
    poller.fetch_account_repos.assert_called_once()
    assert repo_cache["fetched_at"] == NOW

def test_fresh_repo_cache_does_not_re_enumerate():
    client = Mock()
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    repo_cache: dict = {"repos": [_account_repo("o/a")], "fetched_at": NOW - timedelta(minutes=5)}
    run_once(client, poller, CFG_ACCOUNT, NOW, {}, dry_run=False, repo_cache=repo_cache)
    poller.fetch_account_repos.assert_not_called()
    poller.fetch_runs.assert_called_once_with("o/a")   # still uses the cached list

def test_enumeration_failure_keeps_previous_list():
    client = Mock()
    poller = Mock()
    poller.fetch_account_repos.return_value = None   # enumeration fails this poll
    poller.fetch_runs.return_value = [_run("success")]
    repo_cache: dict = {"repos": [_account_repo("o/previously_known")],
                        "fetched_at": NOW - timedelta(minutes=61)}
    run_once(client, poller, CFG_ACCOUNT, NOW, {}, dry_run=False, repo_cache=repo_cache)
    # Still watching the previously-cached repo -- never fell back to empty.
    poller.fetch_runs.assert_called_once_with("o/previously_known")
    assert repo_cache["repos"] == [_account_repo("o/previously_known")]

def test_enumeration_failure_with_no_prior_list_logs_warning_and_watches_nothing(caplog):
    client = Mock()
    poller = Mock()
    poller.fetch_account_repos.return_value = None
    repo_cache: dict = {}   # never successfully populated
    import logging
    with caplog.at_level(logging.WARNING, logger="ci_status"):
        run_once(client, poller, CFG_ACCOUNT, NOW, {}, dry_run=False, repo_cache=repo_cache)
    poller.fetch_runs.assert_not_called()
    assert any("enumeration failed" in rec.message for rec in caplog.records)


# --- state_cache / running_cache pruning for dropped repos -----------------------

def test_state_cache_pruned_when_repo_excluded():
    client = Mock()
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    cfg = {"ci_status": {**CFG_ACCOUNT["ci_status"], "repos_exclude": ["o/gone"]}}
    poller.fetch_account_repos.return_value = [_account_repo("o/gone"), _account_repo("o/stays")]
    state_cache = {"o/gone": RepoState("o/gone", ["tests"], [])}   # stale alert from before
    repo_cache: dict = {}
    run_once(client, poller, cfg, NOW, state_cache, dry_run=False, repo_cache=repo_cache)
    assert "o/gone" not in state_cache
    assert "o/stays" in state_cache

def test_running_cache_pruned_when_repo_ages_out():
    client = Mock()
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = []
    poller.fetch_median_eta.return_value = None
    poller.fetch_account_repos.return_value = [_account_repo("o/fresh", pushed_days_ago=1)]
    running_cache = {"o/aged_out": [_running_run()]}   # from a repo no longer in the window
    repo_cache: dict = {}
    run_once(client, poller, CFG_ACCOUNT, NOW, {}, dry_run=False,
            running_cache=running_cache, repo_cache=repo_cache)
    assert "o/aged_out" not in running_cache

def test_dropped_repo_forgets_poller_etag_state():
    client = Mock()
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    cfg = {"ci_status": {**CFG_ACCOUNT["ci_status"], "repos_exclude": ["o/gone"]}}
    poller.fetch_account_repos.return_value = [_account_repo("o/stays")]
    state_cache = {"o/gone": RepoState("o/gone", [], [])}
    repo_cache: dict = {}
    run_once(client, poller, cfg, NOW, state_cache, dry_run=False, repo_cache=repo_cache)
    poller.forget_repo.assert_called_once_with("o/gone")

def test_no_pruning_when_effective_list_is_unchanged():
    # Sanity check: the pruning step must not evict a repo that's still
    # in the effective list just because it was cached from an earlier poll.
    client = Mock()
    poller = Mock()
    poller.fetch_runs.return_value = [_run("failure")]
    state_cache = {"o/r": RepoState("o/r", ["tests"], [])}
    run_once(client, poller, CFG_RUNNING, NOW, state_cache, dry_run=False)
    assert "o/r" in state_cache
    poller.forget_repo.assert_not_called()


# --- next_poll_seconds sees auto-discovered repos ---------------------------------

def test_next_poll_seconds_shortens_for_auto_discovered_repo_not_in_explicit_repos():
    # o/discovered was never in cfg_ci["repos"] at all -- next_poll_seconds
    # must still see it via running_cache's own keys, not cfg_ci["repos"].
    running_cache = {"o/discovered": [_running_run()]}
    assert next_poll_seconds(CFG_ACCOUNT["ci_status"], running_cache) == 20

def test_next_poll_seconds_reverts_when_all_running_cache_entries_empty():
    running_cache = {"o/a": [], "o/b": []}
    assert next_poll_seconds(CFG_RUNNING["ci_status"], running_cache) == 120


# --- config_requires_repos: main()'s startup validation, extracted for testability

def test_config_requires_repos_errors_when_empty_and_account_mode_off():
    cfg = {"ci_status": {"repos": [], "watch_account_repos": False}}
    err = config_requires_repos(cfg)
    assert err is not None
    assert "No repos configured" in err

def test_config_requires_repos_ok_when_empty_but_account_mode_on():
    cfg = {"ci_status": {"repos": [], "watch_account_repos": True}}
    assert config_requires_repos(cfg) is None

def test_config_requires_repos_ok_when_nonempty_and_account_mode_off():
    cfg = {"ci_status": {"repos": ["o/r"], "watch_account_repos": False}}
    assert config_requires_repos(cfg) is None

def test_config_requires_repos_ok_when_watch_account_repos_key_absent():
    # Backward compat: an old-style cfg dict without the v1.5.1 key at
    # all (predates .get's default) must not crash, and behaves like
    # watch_account_repos=False.
    cfg = {"ci_status": {"repos": ["o/r"]}}
    assert config_requires_repos(cfg) is None
    cfg_empty = {"ci_status": {"repos": []}}
    assert config_requires_repos(cfg_empty) is not None


# --- v1.5.2: REJECTED handling during calendar priority elevation ---------------
#
# calendar_countdown can now draw at PRIORITY_AMBIENT_RAISED (25, inside its
# approach window) or PRIORITY_AMBIENT_URGENT (65, inside its notice/warn
# window, above the top alert-priority tier (60)), evicting ci_status's own
# elements. ci_status's own next redraw attempt at its own priority then gets
# a 409 (DrawResult.REJECTED) while the calendar holds the higher tier --
# expected and silent per busybar.client's own DrawResult.REJECTED
# docstring. This test confirms ci_status's run_once tolerates that cleanly:
# no crash, no state/shape committed on a REJECTED draw, and a full recovery
# once the calendar drops back down and the next draw actually lands.

def test_overlay_dwell_rejected_during_calendar_elevation_resumes_after():
    client = Mock()
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    overlay_state: dict = {}

    # Poll 1: calendar is in its approach window (PRIORITY_AMBIENT_RAISED=25
    # > overlay's 21) -- the running-badge dwell draw is rejected.
    client.draw.return_value = DrawResult.REJECTED
    summary1 = run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False,
                        running_cache={}, overlay_state=overlay_state)
    assert "rejected" in summary1
    assert "last_dwell_end" not in overlay_state   # dwell never actually started
    assert "last_shape" not in overlay_state

    # Poll 2: calendar has dropped back down -- the SAME dwell slot (never
    # consumed, since the rejected attempt didn't commit) draws successfully.
    # This is the "no crash, rotation resumes after" requirement -- an
    # immediate retry lands, not a wait for a phantom dwell that never
    # actually rendered anything.
    client.draw.return_value = DrawResult.DRAWN
    soon_after = NOW + timedelta(seconds=1)
    summary2 = run_once(client, poller, CFG_RUNNING, soon_after, {}, dry_run=False,
                        running_cache={}, overlay_state=overlay_state)
    assert "drawn" in summary2
    assert overlay_state.get("last_dwell_end") is not None
    assert overlay_state["last_shape"] == frozenset({"bg", "title", "track", "track_fill", "eta"})
    assert client.draw.call_count == 2   # both attempts drew (1st rejected, 2nd landed) -- no crash anywhere
