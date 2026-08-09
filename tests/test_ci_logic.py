from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations"))
from ci_status.logic import (
    RepoState, RunningInfo, QuotaInfo, FailingRun, evaluate_runs, build_ci_payload,
    build_overlay_payload, overlay_frame_sequence,
    OVERLAY_FRAME_CI_BADGE, OVERLAY_FRAME_QUOTA_GQL, OVERLAY_FRAME_QUOTA_REST,
    OVERLAY_FRAME_SHAPE,
    _pr_or_branch, select_running_run, compute_median_duration_minutes,
    _format_eta_text, _progress_width, _build_running_title,
    parse_rate_limit, _quota_headroom, _quota_used_width,
    resolve_repo_list, _eta_label, RUNNING_NUMERAL_X, RUNNING_LABEL_GAP_PX,
    compute_alert_fingerprint, update_snooze, RUN_SPINNER_ID,
)
from busybar.display import PRIORITY_OVERLAY, OVERLAY_DWELL_SECONDS, PRIORITY_ALERT
from calendar_countdown.logic import _text_width_px

NOW = datetime(2026, 8, 3, 13, 37, tzinfo=timezone.utc)


def run(workflow_id: int, name: str, status: str, conclusion: str | None,
        created_min_ago: int = 5, pr_number: int | None = None,
        head_branch: str | None = None) -> dict:
    created = (NOW - timedelta(minutes=created_min_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    d = {"workflow_id": workflow_id, "name": name, "status": status,
         "conclusion": conclusion, "created_at": created}
    if pr_number is not None:
        d["pull_requests"] = [{"number": pr_number}]
    if head_branch is not None:
        d["head_branch"] = head_branch
    return d


def _text_element(elements: list[dict]) -> dict:
    return next(e for e in elements if e["type"] == "text")


def _bg_element(elements: list[dict]) -> dict:
    return next(e for e in elements if e["type"] == "rectangle")


def _by_id(elements: list[dict]) -> dict:
    return {e["id"]: e for e in elements}


def running_run(workflow_id: int = 1, name: str = "tests", pr_number: int | None = 42,
                head_branch: str = "main", started_min_ago: float = 3) -> dict:
    started = (NOW - timedelta(minutes=started_min_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "workflow_id": workflow_id, "name": name, "status": "in_progress",
        "run_started_at": started, "head_branch": head_branch,
        "pull_requests": [{"number": pr_number}] if pr_number is not None else [],
    }


def success_run(started: str, updated: str) -> dict:
    return {"run_started_at": started, "updated_at": updated}


def running_info(**overrides) -> RunningInfo:
    defaults = dict(run=running_run(), repo="acme/widgets", other_count=0,
                    median_minutes=None, now=NOW)
    defaults.update(overrides)
    return RunningInfo(**defaults)


def quota_info(**overrides) -> QuotaInfo:
    defaults = dict(label="GITHUB REST", limit=5000, remaining=2500, used=2500,
                    reset_epoch=int(NOW.timestamp()) + 42 * 60, now=NOW)
    defaults.update(overrides)
    return QuotaInfo(**defaults)


def test_failure_detected_on_latest_run_only():
    runs = [run(1, "tests", "completed", "success"),          # newest for wf 1
            run(1, "tests", "completed", "failure", 60),      # older failure — ignore
            run(2, "lint", "completed", "failure", pr_number=42)]
    state = evaluate_runs("o/r", runs, NOW, 0)
    assert state.failing == [FailingRun("lint", "#42")] and state.stuck == []


def test_stuck_queued_detection_respects_threshold():
    runs = [run(1, "tests", "queued", None, created_min_ago=20, head_branch="main")]
    assert evaluate_runs("o/r", runs, NOW, 15).stuck == [FailingRun("tests", "main")]
    assert evaluate_runs("o/r", runs, NOW, 0).stuck == []       # disabled
    assert evaluate_runs("o/r", runs, NOW, 30).stuck == []      # under threshold


def test_failing_run_ref_empty_when_no_pr_or_branch():
    state = evaluate_runs("o/r", [run(1, "tests", "completed", "failure")], NOW, 0)
    assert state.failing == [FailingRun("tests", "")]


def test_evaluate_sorts_failing_by_workflow_then_ref():
    runs = [run(2, "zeta", "completed", "failure", pr_number=9),
            run(1, "alpha", "completed", "failure", pr_number=3)]
    state = evaluate_runs("o/r", runs, NOW, 0)
    assert state.failing == [FailingRun("alpha", "#3"), FailingRun("zeta", "#9")]


def test_payload_none_when_green_and_quiet():
    assert build_ci_payload([RepoState("o/r", [], [])], False, 180) is None


def test_payload_shows_green_glyph_when_enabled():
    payload = build_ci_payload([RepoState("o/r", [], [])], True, 180)
    assert payload["priority"] == PRIORITY_ALERT == 60
    text_el = _text_element(payload["elements"])
    assert text_el["color"] == "#00FF00FF"
    # quiet green case has no full-panel background badge
    assert not any(e["type"] == "rectangle" for e in payload["elements"])


def test_payload_red_badge_on_failure():
    payload = build_ci_payload([RepoState("o/r", [FailingRun("tests", "#42")], [])], False, 180)
    assert payload["priority"] == PRIORITY_ALERT and payload["led"] == "#FF0000FF"

    bg = _bg_element(payload["elements"])
    assert bg["x"] == 0 and bg["y"] == 0 and bg["width"] == 72 and bg["height"] == 16
    assert bg["radius"] == 2 and bg["fill"] == "solid"
    assert bg["fill_colors"] == ["#A32D2DFF"]
    # default 1px white border would outline the badge; must be disabled
    assert bg["border_width"] == 0

    text_el = _text_element(payload["elements"])
    assert "o/r" in text_el["text"] and "#42" in text_el["text"] and "tests" in text_el["text"]
    assert text_el["color"] == "#FFFFFFFF" and text_el["font"] == "bold"


def test_payload_amber_badge_on_stuck_only():
    payload = build_ci_payload([RepoState("o/r", [], [FailingRun("tests", "main")])], False, 180)
    assert payload["led"] is None
    assert _bg_element(payload["elements"])["fill_colors"] == ["#BA7517FF"]
    text_el = _text_element(payload["elements"])
    assert "stuck" in text_el["text"] and "main" in text_el["text"]


def test_failure_badge_takes_priority_over_stuck():
    payload = build_ci_payload([RepoState("o/r", [FailingRun("tests", "")], [FailingRun("lint", "")])], False, 180)
    assert _bg_element(payload["elements"])["fill_colors"] == ["#A32D2DFF"]


# --- PR number / branch fallback ----------------------------------------------

def test_pr_or_branch_uses_pr_number_when_present():
    assert _pr_or_branch({"pull_requests": [{"number": 42}], "head_branch": "feature-x"}) == "#42"

def test_pr_or_branch_falls_back_to_head_branch_when_no_pr():
    # Fork/push-triggered runs have an empty pull_requests array.
    assert _pr_or_branch({"pull_requests": [], "head_branch": "main"}) == "main"
    assert _pr_or_branch({"head_branch": "main"}) == "main"   # key absent entirely

def test_pr_or_branch_uses_first_pr_when_multiple():
    assert _pr_or_branch({"pull_requests": [{"number": 7}, {"number": 8}]}) == "#7"


# --- select_running_run: multi-repo, most-recent, +N --------------------------

def test_select_running_run_none_when_nothing_running():
    assert select_running_run({}) is None
    assert select_running_run({"o/r": []}) is None

def test_select_running_run_single_candidate_no_others():
    run_ = running_run(started_min_ago=5)
    result = select_running_run({"o/r": [run_]})
    assert result == (run_, "o/r", 0)

def test_select_running_run_picks_most_recently_started_across_repos():
    older = running_run(workflow_id=1, started_min_ago=10)
    newer = running_run(workflow_id=2, started_min_ago=2)
    result = select_running_run({"o/r1": [older], "o/r2": [newer]})
    assert result[0] is newer and result[1] == "o/r2"

def test_select_running_run_counts_others_across_all_repos():
    a = running_run(workflow_id=1, started_min_ago=1)   # most recent -> selected
    b = running_run(workflow_id=2, started_min_ago=5)
    c = running_run(workflow_id=3, started_min_ago=8)
    result = select_running_run({"o/r1": [a, b], "o/r2": [c]})
    assert result[0] is a and result[2] == 2   # +2 others

def test_select_running_run_ignores_non_in_progress_entries():
    stale = {**running_run(), "status": "completed"}
    live = running_run(started_min_ago=1)
    result = select_running_run({"o/r": [stale, live]})
    assert result[0] is live and result[2] == 0


# --- compute_median_duration_minutes -------------------------------------------

def test_median_duration_odd_count():
    runs = [success_run("2026-08-03T10:00:00Z", "2026-08-03T10:04:00Z"),   # 4 min
           success_run("2026-08-03T09:00:00Z", "2026-08-03T09:06:00Z"),   # 6 min
           success_run("2026-08-03T08:00:00Z", "2026-08-03T08:05:00Z")]  # 5 min
    assert compute_median_duration_minutes(runs) == 5.0

def test_median_duration_even_count_averages_middle_two():
    runs = [success_run("2026-08-03T10:00:00Z", "2026-08-03T10:04:00Z"),   # 4
           success_run("2026-08-03T09:00:00Z", "2026-08-03T09:06:00Z")]   # 6
    assert compute_median_duration_minutes(runs) == 5.0   # (4+6)/2

def test_median_duration_none_when_no_runs():
    assert compute_median_duration_minutes([]) is None

def test_median_duration_skips_runs_missing_timestamps():
    runs = [{"run_started_at": None, "updated_at": None},
           success_run("2026-08-03T10:00:00Z", "2026-08-03T10:04:00Z")]
    assert compute_median_duration_minutes(runs) == 4.0

def test_median_duration_caps_at_first_5():
    # 6 runs of varying duration; only the first 5 (per_page=5 upstream,
    # but this stays defensive) should count.
    runs = [success_run("2026-08-03T10:00:00Z", f"2026-08-03T10:{m:02d}:00Z")
           for m in (1, 2, 3, 4, 5, 99)]
    assert compute_median_duration_minutes(runs) == 3.0   # median of [1,2,3,4,5]


# --- ETA text formatting --------------------------------------------------------

def test_eta_text_with_history_uses_tilde_prefix():
    run_ = running_run(started_min_ago=10)
    assert _format_eta_text(run_, median_minutes=14, now=NOW) == "~4m"

def test_eta_text_reuses_format_countdown_for_hours():
    run_ = running_run(started_min_ago=5)
    assert _format_eta_text(run_, median_minutes=70, now=NOW) == "~1h05m"

def test_eta_text_shows_soon_when_floored_to_zero():
    run_ = running_run(started_min_ago=14)
    assert _format_eta_text(run_, median_minutes=14, now=NOW) == "soon"   # exactly at median
    run_over = running_run(started_min_ago=20)
    assert _format_eta_text(run_over, median_minutes=14, now=NOW) == "soon"   # overrun
    run_almost = running_run(started_min_ago=13.5)
    assert _format_eta_text(run_almost, median_minutes=14, now=NOW) == "soon"   # 0.5 min left

def test_eta_text_no_history_shows_elapsed_with_in_suffix():
    run_ = running_run(started_min_ago=3)
    assert _format_eta_text(run_, median_minutes=None, now=NOW) == "3m in"

def test_eta_text_no_history_reuses_format_countdown_for_hours():
    run_ = running_run(started_min_ago=65)
    assert _format_eta_text(run_, median_minutes=None, now=NOW) == "1h05m in"


# --- track progress width -------------------------------------------------------

def test_progress_width_full_when_median_unknown():
    assert _progress_width(elapsed_minutes=5, median_minutes=None) == 72

def test_progress_width_scales_with_elapsed_over_median():
    assert _progress_width(elapsed_minutes=7, median_minutes=14) == 36   # half -> half width

def test_progress_width_clamps_at_full_when_overrun():
    assert _progress_width(elapsed_minutes=20, median_minutes=14) == 72

def test_progress_width_clamped_min_one():
    assert _progress_width(elapsed_minutes=0, median_minutes=14) == 1
    assert _progress_width(elapsed_minutes=-1, median_minutes=14) == 1

def test_progress_width_full_when_median_non_positive():
    assert _progress_width(elapsed_minutes=5, median_minutes=0) == 72


# --- running badge title --------------------------------------------------------

def test_running_title_with_pr_number():
    run_ = running_run(name="tests", pr_number=42, head_branch="feature-x")
    assert _build_running_title(run_, "acme/widgets", 0) == "ACME/WIDGETS #42 TESTS"

def test_running_title_falls_back_to_branch():
    run_ = running_run(name="deploy", pr_number=None, head_branch="release-2.0")
    assert _build_running_title(run_, "acme/widgets", 0) == "ACME/WIDGETS RELEASE-2.0 DEPLOY"

def test_running_title_appends_plus_n_when_others_active():
    run_ = running_run(name="tests", pr_number=42)
    assert _build_running_title(run_, "acme/widgets", 3) == "ACME/WIDGETS #42 TESTS +3"

def test_running_title_no_suffix_when_alone():
    run_ = running_run(name="tests", pr_number=42)
    assert "+0" not in _build_running_title(run_, "acme/widgets", 0)


# --- build_overlay_payload: running badge (ci_badge frame) ---------------------

def test_overlay_ci_badge_shape():
    run_ = running_run(name="tests", pr_number=42, started_min_ago=3)
    info = running_info(run=run_, median_minutes=14)
    payload = build_overlay_payload(OVERLAY_FRAME_CI_BADGE, OVERLAY_DWELL_SECONDS, running=info)

    assert payload["priority"] == PRIORITY_OVERLAY == 21
    assert payload["led"] is None

    by_id = _by_id(payload["elements"])
    # v1.5.1: a fitting remain-estimate ETA ("~11m" here) picks up the
    # "eta_label" element too -- see test_eta_label_* below for the full
    # fit-decision and grammar-guard coverage.
    assert set(by_id) == {"bg", "title", "track", "track_fill", "eta", "eta_label"}
    assert [e["id"] for e in payload["elements"]] == \
        ["bg", "title", "track", "track_fill", "eta", "eta_label"]
    assert by_id["eta_label"]["text"] == "remain"

    bg = by_id["bg"]
    assert bg["fill"] == "gradient_v" and bg["border_width"] == 0
    assert bg["timeout"] == OVERLAY_DWELL_SECONDS == 10

    title = by_id["title"]
    assert title["text"] == "ACME/WIDGETS #42 TESTS"
    assert title["font"] == "small" and title["y"] == -2

    track = by_id["track"]
    assert track["y"] == 6 and track["width"] == 72 and track["border_width"] == 0

    track_fill = by_id["track_fill"]
    assert track_fill["fill"] == "solid"   # spec: "solid cyan", no gradient
    assert track_fill["width"] == _progress_width(3, 14)

    eta = by_id["eta"]
    assert eta["font"] == "large" and eta["y"] == 5   # numeral-floor rule: large font
    assert eta["text"] == _format_eta_text(run_, 14, NOW)

def test_overlay_ci_badge_shape_no_history_has_no_label():
    # No median history -> "3m in" (elapsed, not a remaining estimate) --
    # the label's grammar guard excludes this form, so the baseline
    # 5-element shape (no "eta_label") is what actually draws.
    run_ = running_run(name="tests", pr_number=42, started_min_ago=3)
    info = running_info(run=run_, median_minutes=None)
    payload = build_overlay_payload(OVERLAY_FRAME_CI_BADGE, OVERLAY_DWELL_SECONDS, running=info)
    by_id = _by_id(payload["elements"])
    assert set(by_id) == {"bg", "title", "track", "track_fill", "eta"}
    assert by_id["eta"]["text"] == "3m in"

def test_overlay_ci_badge_title_scrolls_when_long():
    run_ = running_run(name="a-very-long-workflow-name-that-will-not-fit", pr_number=12345, started_min_ago=1)
    info = running_info(run=run_, repo="acme/some-long-widgets-repo-name", median_minutes=None)
    payload = build_overlay_payload(OVERLAY_FRAME_CI_BADGE, OVERLAY_DWELL_SECONDS, running=info)
    title = _by_id(payload["elements"])["title"]
    assert title.get("scroll_rate") == 2000

def test_overlay_ci_badge_none_when_no_running_info():
    assert build_overlay_payload(OVERLAY_FRAME_CI_BADGE, OVERLAY_DWELL_SECONDS, running=None) is None


# --- build_overlay_payload: quota frames ----------------------------------------

def test_overlay_quota_gql_shape():
    info = quota_info(label="GITHUB GRAPHQL", limit=5000, remaining=2600, used=2400,
                      reset_epoch=int(NOW.timestamp()) + 42 * 60)
    payload = build_overlay_payload(OVERLAY_FRAME_QUOTA_GQL, OVERLAY_DWELL_SECONDS,
                                    quota_by_bucket={"graphql": info})
    assert payload["priority"] == PRIORITY_OVERLAY

    by_id = _by_id(payload["elements"])
    assert set(by_id) == {"bg", "title", "track", "track_fill", "pct", "reset"}
    assert [e["id"] for e in payload["elements"]] == \
        ["bg", "title", "track", "track_fill", "pct", "reset"]

    assert by_id["title"]["text"] == "GITHUB GRAPHQL"
    assert by_id["title"]["font"] == "small"
    assert by_id["pct"]["text"] == "52%"    # floor(2600/5000*100) = 52
    assert by_id["pct"]["font"] == "large"  # numeral-floor rule
    assert by_id["reset"]["text"] == "42m"
    assert by_id["reset"]["font"] == "large"
    assert by_id["track_fill"]["width"] == _quota_used_width(2400, 5000)
    assert by_id["track"]["y"] == 6 and by_id["track"]["border_width"] == 0

def test_overlay_quota_rest_uses_core_bucket():
    info = quota_info(label="GITHUB REST", limit=5000, remaining=100, used=4900)
    payload = build_overlay_payload(OVERLAY_FRAME_QUOTA_REST, OVERLAY_DWELL_SECONDS,
                                    quota_by_bucket={"core": info})
    by_id = _by_id(payload["elements"])
    assert by_id["title"]["text"] == "GITHUB REST"
    assert by_id["pct"]["text"] == "2%"

def test_overlay_quota_none_when_bucket_missing():
    assert build_overlay_payload(OVERLAY_FRAME_QUOTA_GQL, OVERLAY_DWELL_SECONDS,
                                 quota_by_bucket={}) is None
    assert build_overlay_payload(OVERLAY_FRAME_QUOTA_GQL, OVERLAY_DWELL_SECONDS,
                                 quota_by_bucket=None) is None
    # Wrong bucket present (core but not graphql) -- still None, not a
    # silent fallback to the wrong data.
    assert build_overlay_payload(OVERLAY_FRAME_QUOTA_GQL, OVERLAY_DWELL_SECONDS,
                                 quota_by_bucket={"core": quota_info()}) is None


# --- headroom color thresholds (boundaries 50/20) -------------------------------

def test_quota_headroom_high_above_50():
    assert _quota_headroom(50.1) == "high"
    assert _quota_headroom(100) == "high"

def test_quota_headroom_medium_at_and_below_50_down_to_20():
    assert _quota_headroom(50) == "medium"    # 50 itself is medium, not high
    assert _quota_headroom(35) == "medium"
    assert _quota_headroom(20) == "medium"    # 20 itself is medium, not low

def test_quota_headroom_low_below_20():
    assert _quota_headroom(19.9) == "low"
    assert _quota_headroom(0) == "low"


# --- used-fraction clamps --------------------------------------------------------

def test_quota_used_width_scales():
    assert _quota_used_width(2500, 5000) == 36   # half -> half width

def test_quota_used_width_clamped_min_one():
    assert _quota_used_width(0, 5000) == 1
    assert _quota_used_width(-1, 5000) == 1

def test_quota_used_width_full_when_limit_non_positive():
    assert _quota_used_width(10, 0) == 72

def test_quota_used_width_clamped_max_when_used_exceeds_limit():
    # Live-observed case (v1.5 on-device quota verification): a real
    # GitHub account's GraphQL bucket reported used=5150 > limit=5000 --
    # GitHub's point-based GraphQL cost accounting can transiently exceed
    # the nominal limit. round(72 * 5150 / 5000) == 74, which must clamp
    # to the panel width rather than overflow the track.
    assert _quota_used_width(5150, 5000) == 72


# --- parse_rate_limit ------------------------------------------------------------

def test_parse_rate_limit_extracts_core_and_graphql():
    data = {"resources": {
        "core": {"limit": 5000, "remaining": 4990, "reset": 1000, "used": 10},
        "graphql": {"limit": 5000, "remaining": 4800, "reset": 2000, "used": 200},
        "search": {"limit": 30, "remaining": 30, "reset": 3000},  # ignored bucket
    }}
    parsed = parse_rate_limit(data)
    assert parsed["core"] == {"limit": 5000, "remaining": 4990, "used": 10, "reset": 1000}
    assert parsed["graphql"] == {"limit": 5000, "remaining": 4800, "used": 200, "reset": 2000}
    assert "search" not in parsed

def test_parse_rate_limit_computes_used_when_absent():
    data = {"resources": {"core": {"limit": 5000, "remaining": 4990, "reset": 1000}}}
    assert parse_rate_limit(data)["core"]["used"] == 10

def test_parse_rate_limit_none_when_no_usable_bucket():
    assert parse_rate_limit({"resources": {}}) is None
    assert parse_rate_limit({}) is None
    assert parse_rate_limit({"resources": {"core": {"limit": 5000}}}) is None  # missing fields

def test_parse_rate_limit_returns_partial_result():
    data = {"resources": {"core": {"limit": 5000, "remaining": 100, "reset": 1000},
                          "graphql": {"limit": 5000}}}   # malformed, dropped
    parsed = parse_rate_limit(data)
    assert "core" in parsed and "graphql" not in parsed


# --- overlay_frame_sequence: round-robin sequencing -----------------------------

def test_overlay_frame_sequence_badge_only_when_quota_disabled():
    assert overlay_frame_sequence(False) == [OVERLAY_FRAME_CI_BADGE]

def test_overlay_frame_sequence_includes_quota_frames_when_enabled():
    assert overlay_frame_sequence(True) == \
        [OVERLAY_FRAME_CI_BADGE, OVERLAY_FRAME_QUOTA_GQL, OVERLAY_FRAME_QUOTA_REST]

def test_overlay_frame_shape_distinguishes_badge_from_quota():
    assert OVERLAY_FRAME_SHAPE[OVERLAY_FRAME_CI_BADGE] == "badge"
    assert OVERLAY_FRAME_SHAPE[OVERLAY_FRAME_QUOTA_GQL] == "quota"
    assert OVERLAY_FRAME_SHAPE[OVERLAY_FRAME_QUOTA_REST] == "quota"
    # The two quota frames share a shape (identical element id sets) --
    # only badge<->quota transitions need the id-shape-change clear.
    assert OVERLAY_FRAME_SHAPE[OVERLAY_FRAME_QUOTA_GQL] == OVERLAY_FRAME_SHAPE[OVERLAY_FRAME_QUOTA_REST]


# --- build_ci_payload: overlay precedence ---------------------------------------

def test_payload_overlay_takes_priority_over_quiet_green():
    overlay = build_overlay_payload(OVERLAY_FRAME_CI_BADGE, OVERLAY_DWELL_SECONDS, running=running_info())
    payload = build_ci_payload([RepoState("o/r", [], [])], True, 180, overlay=overlay)
    assert payload["priority"] == PRIORITY_OVERLAY   # overlay beats show_green
    assert payload is overlay

def test_payload_failure_takes_priority_over_overlay():
    overlay = build_overlay_payload(OVERLAY_FRAME_CI_BADGE, OVERLAY_DWELL_SECONDS, running=running_info())
    payload = build_ci_payload([RepoState("o/r", [FailingRun("tests", "")], [])], False, 180, overlay=overlay)
    assert payload["priority"] == PRIORITY_ALERT   # failure wins, not the overlay
    bg = _bg_element(payload["elements"])
    assert bg["fill_colors"] == ["#A32D2DFF"]

def test_payload_stuck_takes_priority_over_overlay():
    overlay = build_overlay_payload(OVERLAY_FRAME_QUOTA_GQL, OVERLAY_DWELL_SECONDS,
                                    quota_by_bucket={"graphql": quota_info()})
    payload = build_ci_payload([RepoState("o/r", [], [FailingRun("tests", "")])], False, 180, overlay=overlay)
    assert payload["priority"] == PRIORITY_ALERT
    bg = _bg_element(payload["elements"])
    assert bg["fill_colors"] == ["#BA7517FF"]

def test_payload_no_overlay_falls_through_to_quiet_or_green_as_before():
    assert build_ci_payload([RepoState("o/r", [], [])], False, 180, overlay=None) is None
    payload = build_ci_payload([RepoState("o/r", [], [])], True, 180, overlay=None)
    assert payload["priority"] == PRIORITY_ALERT


# --- resolve_repo_list (v1.5.1 account-wide watching) -----------------------------

def _account_repo(full_name, pushed_days_ago=1, archived=False):
    pushed = (NOW - timedelta(days=pushed_days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"full_name": full_name, "archived": archived, "pushed_at": pushed}

def test_resolve_repo_list_account_mode_off_is_just_repos_minus_exclude():
    result = resolve_repo_list(["o/a", "o/b"], ["o/b"], False,
                               [_account_repo("o/c")], 30, NOW)
    assert result == ["o/a"]   # account_repos ignored entirely when the mode is off

def test_resolve_repo_list_unions_explicit_and_discovered():
    account = [_account_repo("o/discovered")]
    result = resolve_repo_list(["o/explicit"], [], True, account, 30, NOW)
    assert result == ["o/discovered", "o/explicit"]

def test_resolve_repo_list_excludes_apply_in_account_mode():
    account = [_account_repo("o/a"), _account_repo("o/b")]
    result = resolve_repo_list([], ["o/b"], True, account, 30, NOW)
    assert result == ["o/a"]

def test_resolve_repo_list_filters_out_stale_pushed_repos():
    account = [_account_repo("o/fresh", pushed_days_ago=5),
              _account_repo("o/stale", pushed_days_ago=45)]
    result = resolve_repo_list([], [], True, account, active_within_days=30, now=NOW)
    assert result == ["o/fresh"]

def test_resolve_repo_list_active_within_days_boundary_is_inclusive():
    # Exactly at the cutoff (pushed_at == now - active_within_days) IS
    # included -- the exclusion test is `pushed < cutoff` (strict), so the
    # boundary instant itself counts as "within the window."
    account = [_account_repo("o/exact", pushed_days_ago=30)]
    result = resolve_repo_list([], [], True, account, active_within_days=30, now=NOW)
    assert result == ["o/exact"]

    # One second past the boundary is excluded.
    just_over = {"full_name": "o/just_over", "archived": False,
                "pushed_at": (NOW - timedelta(days=30, seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    result2 = resolve_repo_list([], [], True, [just_over], active_within_days=30, now=NOW)
    assert result2 == []

def test_resolve_repo_list_explicit_repos_never_filtered_by_staleness():
    # o/explicit was pushed 400 days ago -- would fail the active-window
    # filter if it were subject to it, but it's in `repos`, not discovered.
    account = [_account_repo("o/explicit", pushed_days_ago=400)]
    result = resolve_repo_list(["o/explicit"], [], True, account, active_within_days=30, now=NOW)
    assert result == ["o/explicit"]

def test_resolve_repo_list_archived_repos_excluded():
    account = [_account_repo("o/live"), _account_repo("o/dead", archived=True)]
    result = resolve_repo_list([], [], True, account, 30, NOW)
    assert result == ["o/live"]

def test_resolve_repo_list_none_account_repos_falls_back_to_repos_only():
    # e.g. discovery hasn't succeeded yet and there's no cached list.
    result = resolve_repo_list(["o/a"], [], True, None, 30, NOW)
    assert result == ["o/a"]

def test_resolve_repo_list_malformed_pushed_at_skipped_not_crashed():
    account = [{"full_name": "o/bad", "archived": False, "pushed_at": "not-a-date"},
              _account_repo("o/good")]
    result = resolve_repo_list([], [], True, account, 30, NOW)
    assert result == ["o/good"]

def test_resolve_repo_list_dedupes_explicit_and_discovered_overlap():
    account = [_account_repo("o/both")]
    result = resolve_repo_list(["o/both"], [], True, account, 30, NOW)
    assert result == ["o/both"]   # not ["o/both", "o/both"]

def test_resolve_repo_list_sorted_deterministic_order():
    account = [_account_repo("z/last"), _account_repo("a/first")]
    result = resolve_repo_list(["m/middle"], [], True, account, 30, NOW)
    assert result == ["a/first", "m/middle", "z/last"]


# --- _eta_label: fit decision + grammar guard (v1.5.1 ETA label) -----------------

def test_eta_label_remain_fits_short_eta():
    # "~59m" measures 29px; remain (35px) + 3px gap = 38 > budget(68) is
    # false only relative to eta width, i.e. 29+3+35=67 <= 68 -- fits.
    assert _text_width_px("~59m") == 29
    assert _eta_label("~59m") == "remain"

def test_eta_label_falls_back_to_left_when_remain_does_not_fit():
    # "~1h00m" measures 40px -- remain would need 40+3+35=78 > 68 (doesn't
    # fit), but left needs only 40+3+20=63 <= 68 (fits).
    assert _text_width_px("~1h00m") == 40
    assert _eta_label("~1h00m") == "left"

def test_eta_label_omitted_when_neither_fits():
    # Synthetic, deliberately-wide input -- _format_eta_text/_format_countdown
    # never actually produce a string this wide in practice (the h+mm full
    # form is itself capped by CD_TEXT_MAX_WIDTH and falls back to an
    # hour-only form well before reaching 45px), but _eta_label is a pure
    # function of its string argument and must degrade safely (omit, not
    # crash or draw an overflowing label) if it's ever fed one anyway --
    # this is the defensive "neither fits" boundary the brief asked for.
    synthetic = "~23h59m"
    assert _text_width_px(synthetic) == 49   # > 45 (the "only left fits" ceiling)
    assert _eta_label(synthetic) is None

def test_eta_label_excluded_on_soon():
    assert _eta_label("soon") is None

def test_eta_label_excluded_on_no_history_elapsed_form():
    assert _eta_label("3m in") is None
    assert _eta_label("1h05m in") is None   # longer elapsed form, still excluded

def test_eta_label_x_position_follows_eta_text_width():
    run_ = running_run(name="tests", pr_number=42, started_min_ago=1)
    info = running_info(run=run_, median_minutes=60)   # eta = 59m -> "~59m", label fits
    payload = build_overlay_payload(OVERLAY_FRAME_CI_BADGE, OVERLAY_DWELL_SECONDS, running=info)
    by_id = _by_id(payload["elements"])
    eta_text = by_id["eta"]["text"]
    assert by_id["eta_label"]["x"] == RUNNING_NUMERAL_X + _text_width_px(eta_text) + RUNNING_LABEL_GAP_PX
    assert by_id["eta_label"]["font"] == "small"
    assert by_id["eta_label"]["y"] == 9

def test_eta_label_falls_back_to_left_end_to_end_through_build_overlay_payload():
    run_ = running_run(name="tests", pr_number=42, started_min_ago=0)
    info = running_info(run=run_, median_minutes=60)   # eta = 60m -> "~1h00m"
    payload = build_overlay_payload(OVERLAY_FRAME_CI_BADGE, OVERLAY_DWELL_SECONDS, running=info)
    by_id = _by_id(payload["elements"])
    assert by_id["eta"]["text"] == "~1h00m"
    assert by_id["eta_label"]["text"] == "left"


# --- alert snooze via the device's native start button (v1.5.2) -----------------

def test_compute_alert_fingerprint_empty_when_all_green():
    assert compute_alert_fingerprint([RepoState("o/r", [], [])]) == frozenset()

def test_compute_alert_fingerprint_covers_failing_and_stuck():
    states = [RepoState("o/r", ["tests"], ["lint"])]
    fp = compute_alert_fingerprint(states)
    assert fp == frozenset({("o/r", "tests", "failing"), ("o/r", "lint", "stuck")})

def test_compute_alert_fingerprint_category_change_is_a_different_fingerprint():
    failing_fp = compute_alert_fingerprint([RepoState("o/r", ["tests"], [])])
    stuck_fp = compute_alert_fingerprint([RepoState("o/r", [], ["tests"])])
    assert failing_fp != stuck_fp

def test_compute_alert_fingerprint_multi_repo():
    states = [RepoState("o/a", ["tests"], []), RepoState("o/b", ["build"], [])]
    fp = compute_alert_fingerprint(states)
    assert fp == frozenset({("o/a", "tests", "failing"), ("o/b", "build", "failing")})


# --- update_snooze: the full state machine ---------------------------------------

FP_A = frozenset({("o/r", "tests", "failing")})
FP_B = frozenset({("o/r", "build", "failing")})   # a different fingerprint
EMPTY_FP = frozenset()

def test_update_snooze_disabled_always_passthrough():
    state = {}
    assert update_snooze(FP_A, True, NOW, 0, state) == (False, False)
    assert state.get("fingerprint") is None

def test_update_snooze_no_alert_no_session_is_a_noop():
    state = {}
    assert update_snooze(EMPTY_FP, False, NOW, 30, state) == (False, False)
    assert "fingerprint" not in state

def test_update_snooze_alert_alone_no_session_no_pending():
    state = {}
    assert update_snooze(FP_A, False, NOW, 30, state) == (False, False)
    assert "fingerprint" not in state

def test_update_snooze_first_ever_poll_with_session_already_active_does_not_pend():
    # Conservative default: session_was_active defaults to True on a
    # fresh/never-observed state, so the very first poll (even if
    # busy_active happens to be True) is never mistaken for a fresh
    # inactive->active transition -- see update_snooze's docstring.
    state = {}
    assert update_snooze(FP_A, True, NOW, 30, state) == (False, False)
    assert "fingerprint" not in state
    assert state["session_was_active"] is True

def test_update_snooze_genuine_transition_establishes_pending():
    state = {}
    update_snooze(FP_A, False, NOW, 30, state)   # observe inactive first
    result = update_snooze(FP_A, True, NOW, 30, state)   # now transitions to active
    assert result == (False, True)   # draw proceeds, LED suppressed
    assert state["fingerprint"] == FP_A
    assert "snooze_until" not in state

# --- Critical regression: unpolled (None) cycles must never corrupt
# session_was_active -- reviewer-reproduced bug. An earlier version wrote
# busy_active unconditionally every call, including a dummy False for
# gated-off (idle) polls; a run of idle polls would stamp
# session_was_active=False regardless of the device's real state, so a
# session that started (unobserved) during that idle stretch and was
# already running by the time an alert first appeared would be
# misread as a fresh transition and silently snoozed -- an
# unacknowledged failure. Fixed: busy_active=None means "not polled this
# cycle" and must NOT be committed.

def test_update_snooze_unpolled_cycles_do_not_corrupt_session_was_active():
    state = {}
    # Simulates several idle polls where get_busy() was never called
    # (main.run_once passes None in this case).
    update_snooze(EMPTY_FP, None, NOW, 30, state)
    update_snooze(EMPTY_FP, None, NOW, 30, state)
    update_snooze(EMPTY_FP, None, NOW, 30, state)
    assert state.get("session_was_active", True) is True   # untouched, still the conservative default

def test_update_snooze_session_predating_alert_does_not_snooze_reviewer_scenario():
    # The exact reviewer-reported scenario: idle polls (unpolled, None) ->
    # a session starts DURING that unpolled stretch (never observed) ->
    # an alert appears, triggering the first real poll, which correctly
    # observes busy_active=True -- but this must NOT be read as a fresh
    # transition, since the session predates the alert and the operator
    # never acknowledged it.
    state = {}
    update_snooze(EMPTY_FP, None, NOW, 30, state)   # idle poll 1, unpolled
    update_snooze(EMPTY_FP, None, NOW, 30, state)   # idle poll 2, unpolled
    # Session starts here, still unobserved (no alert yet, still not polling).
    t1 = NOW + timedelta(seconds=30)
    result = update_snooze(FP_A, True, t1, 30, state)   # alert appears -- first real poll
    assert result == (False, False)   # must NOT pend -- no acknowledged transition observed
    assert "fingerprint" not in state

def test_update_snooze_mirror_alert_first_then_session_starts_while_polled():
    # The mirror case (still correct, unaffected by the fix): the alert
    # appears FIRST (triggering real polling immediately), observes
    # inactive, and only THEN does the session start while polling
    # continues -- a genuine, fully-observed transition, so pending
    # DOES start, exactly as designed.
    state = {}
    update_snooze(FP_A, False, NOW, 30, state)   # alert showing, polled, session inactive
    t1 = NOW + timedelta(seconds=10)
    result = update_snooze(FP_A, True, t1, 30, state)   # still polled -- session starts
    assert result == (False, True)
    assert state["fingerprint"] == FP_A

def test_update_snooze_none_after_established_pending_defensive_stays_pending():
    # Defensive case documented in update_snooze: main.run_once's own
    # gating guarantees busy_active is never None while a fingerprint is
    # pending (a pending snooze always keeps polling), but the function
    # itself treats an (unexpected) None here as "stay pending" rather
    # than risk prematurely starting the timed snooze on an unpolled guess.
    state = {}
    update_snooze(FP_A, False, NOW, 30, state)
    update_snooze(FP_A, True, NOW, 30, state)   # now pending on FP_A
    assert state.get("snooze_until") is None
    result = update_snooze(FP_A, None, NOW, 30, state)
    assert result == (False, True)   # still pending, not prematurely timed
    assert "snooze_until" not in state

def test_update_snooze_stays_pending_while_session_continues():
    state = {}
    update_snooze(FP_A, False, NOW, 30, state)
    update_snooze(FP_A, True, NOW, 30, state)
    later = NOW + timedelta(minutes=2)
    assert update_snooze(FP_A, True, later, 30, state) == (False, True)
    assert "snooze_until" not in state

def test_update_snooze_session_end_starts_timed_snooze():
    state = {}
    update_snooze(FP_A, False, NOW, 30, state)
    update_snooze(FP_A, True, NOW, 30, state)
    session_end = NOW + timedelta(minutes=5)
    result = update_snooze(FP_A, False, session_end, 30, state)
    assert result == (True, False)
    assert state["snooze_until"] == session_end + timedelta(minutes=30)

def test_update_snooze_suppresses_through_the_timed_window():
    state = {}
    update_snooze(FP_A, False, NOW, 30, state)
    update_snooze(FP_A, True, NOW, 30, state)
    session_end = NOW + timedelta(minutes=5)
    update_snooze(FP_A, False, session_end, 30, state)
    mid_snooze = session_end + timedelta(minutes=10)
    assert update_snooze(FP_A, False, mid_snooze, 30, state) == (True, False)

def test_update_snooze_expires_and_realerts_if_still_failing():
    state = {}
    update_snooze(FP_A, False, NOW, 30, state)
    update_snooze(FP_A, True, NOW, 30, state)
    session_end = NOW + timedelta(minutes=5)
    update_snooze(FP_A, False, session_end, 30, state)
    after_expiry = session_end + timedelta(minutes=31)
    assert update_snooze(FP_A, False, after_expiry, 30, state) == (False, False)
    assert "fingerprint" not in state

def test_update_snooze_fingerprint_change_during_pending_reelerts():
    state = {}
    update_snooze(FP_A, False, NOW, 30, state)
    update_snooze(FP_A, True, NOW, 30, state)   # pending on FP_A
    later = NOW + timedelta(minutes=1)
    # A different fingerprint appears while still pending on FP_A --
    # clears the old pending. Session is still active (level, not a fresh
    # edge for FP_B), so FP_B does NOT get a fresh pending either.
    result = update_snooze(FP_B, True, later, 30, state)
    assert result == (False, False)
    assert "fingerprint" not in state

def test_update_snooze_fingerprint_change_during_timed_snooze_realerts_immediately():
    state = {}
    update_snooze(FP_A, False, NOW, 30, state)
    update_snooze(FP_A, True, NOW, 30, state)
    session_end = NOW + timedelta(minutes=5)
    update_snooze(FP_A, False, session_end, 30, state)   # timed snooze on FP_A
    mid_snooze = session_end + timedelta(minutes=10)
    # A DIFFERENT failure shows up while FP_A is still timed-snoozed and
    # no session is running -- must alert immediately, not stay suppressed.
    result = update_snooze(FP_B, False, mid_snooze, 30, state)
    assert result == (False, False)
    assert "fingerprint" not in state

def test_update_snooze_resolved_then_new_clears_snooze():
    state = {}
    update_snooze(FP_A, False, NOW, 30, state)
    update_snooze(FP_A, True, NOW, 30, state)
    session_end = NOW + timedelta(minutes=5)
    update_snooze(FP_A, False, session_end, 30, state)   # timed on FP_A
    mid_snooze = session_end + timedelta(minutes=10)
    # FP_A resolved entirely (nothing failing) -- also a fingerprint change.
    result = update_snooze(EMPTY_FP, False, mid_snooze, 30, state)
    assert result == (False, False)
    assert "fingerprint" not in state

def test_update_snooze_session_starting_mid_alert_after_continuous_polling():
    # The "normal" full flow, polled continuously (no gating gaps): alert
    # appears while no session is running, then a session starts.
    state = {}
    assert update_snooze(FP_A, False, NOW, 30, state) == (False, False)
    t1 = NOW + timedelta(seconds=10)
    assert update_snooze(FP_A, False, t1, 30, state) == (False, False)   # still no session
    t2 = t1 + timedelta(seconds=10)
    assert update_snooze(FP_A, True, t2, 30, state) == (False, True)   # session starts -- pending


# --- CI running-badge spinner (v1.6, task 4) ---------------------------------------
#
# NOTE: uses SPINNER_NOW (not the module-level NOW) -- a distinct name is
# used deliberately here rather than reassigning NOW, since NOW is read at
# call time (late-bound) by dozens of test functions throughout this file;
# rebinding it at module scope after this point would silently change the
# "now" every earlier-defined test observes when pytest actually calls them.

SPINNER_NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


def _running():
    return RunningInfo(run={"name": "build", "run_started_at": "2026-08-06T11:58:00Z",
                            "pull_requests": [{"number": 42}], "workflow_id": 1},
                       repo="me/repo", other_count=0, median_minutes=8.0, now=SPINNER_NOW)


def test_spinner_present_and_title_reserved_when_on():
    p = build_overlay_payload(OVERLAY_FRAME_CI_BADGE, 10, running=_running(), show_spinner=True)
    els = p["elements"]
    spin = next(e for e in els if e["id"] == RUN_SPINNER_ID)
    assert spin["type"] == "animation" and spin["stock_path"] == "shared/spinner_front_8x8.anim"
    assert spin["x"] == 64 and spin["y"] == 0
    title = next(e for e in els if e["id"] == "title")
    assert title["width"] == 60   # reserved so the scrolling title never runs under the spinner

def test_no_spinner_and_full_title_when_off():
    p = build_overlay_payload(OVERLAY_FRAME_CI_BADGE, 10, running=_running(), show_spinner=False)
    els = p["elements"]
    assert not any(e["id"] == RUN_SPINNER_ID for e in els)
    assert next(e for e in els if e["id"] == "title")["width"] == 68  # unchanged
