import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Reuse the calendar integration's generic text/formatting helpers rather
# than reimplementing them: ascii_safe (device text sanitization),
# _format_countdown (minutes-granular "~4m" / "~1h05m" formatting, reused
# verbatim by the running badge's ETA text and the quota frames' reset-in
# text per the v1.5 spec), _title_fits (scroll-vs-static decision,
# parameterized by width so it's not calendar-geometry-specific),
# _text_width_px (per-glyph width via GLYPH_ADVANCE_PX, v1.5.1: reused by
# the running badge's ETA "remain"/"left" label fit decision --
# see _eta_label below), SCROLL_RATE/SCROLL_DELAY_MS (the same scroll
# timing), and PANEL_WIDTH/PANEL_HEIGHT (hardware constants, not actually
# calendar-specific despite living in that module). Geometry and palette
# below are ci_status's own -- only the algorithms are shared, not the
# layout constants, since the two integrations' layouts are visually
# similar (v1.4 "airy" language) but structurally distinct.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from calendar_countdown.logic import (  # noqa: E402
    ascii_safe, _format_countdown, _title_fits, _text_width_px,
    SCROLL_RATE, SCROLL_DELAY_MS, PANEL_WIDTH, PANEL_HEIGHT,
)

# Shared display priority ladder (v1.5) -- see busybar/display.py for the
# full contract and the two firmware facts it's built on (equal priority
# from a different app is rejected, not an override; occluded elements are
# evicted, not restored). Formerly local constants here (RUNNING_PRIORITY,
# RUNNING_BADGE_TIMEOUT_S); now PRIORITY_OVERLAY (and, in main.py,
# OVERLAY_DWELL_SECONDS) from the shared module so future integrations
# inherit the same contract instead of re-deriving it.
from busybar.display import PRIORITY_OVERLAY, PRIORITY_ALERT  # noqa: E402

FAILING = {"failure", "timed_out", "startup_failure"}


@dataclass(frozen=True)
class FailingRun:
    workflow: str   # r["name"]
    ref: str        # _pr_or_branch(r): "#42", the branch, or ""


@dataclass
class RepoState:
    repo: str
    failing: list[FailingRun]
    stuck: list[FailingRun]


@dataclass
class RunningInfo:
    """Everything build_overlay_payload needs to render the running-CI
    badge frame, pre-computed by the caller (main.run_once) so the render
    logic here stays pure and testable without network mocking."""
    run: dict
    repo: str
    other_count: int
    median_minutes: float | None
    now: datetime


@dataclass
class QuotaInfo:
    """Everything build_overlay_payload needs to render one quota frame
    (GraphQL or REST bucket), pre-computed by the caller -- same pattern
    and rationale as RunningInfo."""
    label: str        # "GITHUB GRAPHQL" or "GITHUB REST"
    limit: int
    remaining: int
    used: int
    reset_epoch: int
    now: datetime


def _parse_ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def resolve_repo_list(repos: list[str], repos_exclude: list[str], watch_account_repos: bool,
                      account_repos: list[dict] | None, active_within_days: int,
                      now: datetime) -> list[str]:
    """The effective set of repos to poll this cycle (v1.5.1 account-wide
    watching).

    In account mode (`watch_account_repos`), the watch list is the union
    of auto-discovered account repos -- filtered to non-archived and
    pushed within `active_within_days` -- and the explicit `repos` list.
    An explicitly configured repo is always included regardless of its
    own push recency: the user named it on purpose, so staleness
    filtering shouldn't silently drop it. `account_repos` (raw dicts from
    `RestPoller.fetch_account_repos`, or the caller's cached copy of an
    earlier successful fetch) may be `None` or empty -- e.g. enumeration
    hasn't succeeded yet, or `watch_account_repos` is off -- in which
    case the result is just `repos` minus `repos_exclude`, same as
    pre-v1.5.1 behavior.

    `repos_exclude` is applied last, unconditionally, in both modes --
    always a no-op when empty, not something that only matters in
    account mode -- so a user can silence a noisy repo without leaving
    account mode, or (less commonly) without editing an explicit `repos`
    list either.

    Caveat (documented in the README too): a repo whose most recent push
    predates `active_within_days` is excluded from account-mode
    discovery even if it has a *schedule*-triggered workflow run more
    recent than that -- `pushed_at` is a repo-level field, not aware of
    scheduled/dispatched runs that don't touch the git history. Such a
    repo is only observed if named explicitly in `repos`.

    Returns a sorted, deduplicated list -- the raw account-repo discovery
    order (by `pushed_at`) isn't meaningful to callers, and a stable,
    deterministic order makes both testing and reading `--dry-run` output
    easier.
    """
    names = set(repos)
    if watch_account_repos and account_repos:
        cutoff = now - timedelta(days=active_within_days)
        for r in account_repos:
            if r.get("archived"):
                continue
            pushed_at = r.get("pushed_at")
            if not pushed_at:
                continue
            try:
                pushed = _parse_ts(pushed_at)
            except (ValueError, TypeError):
                continue
            if pushed < cutoff:
                continue
            full_name = r.get("full_name")
            if full_name:
                names.add(full_name)
    names -= set(repos_exclude)
    return sorted(names)


def evaluate_runs(repo: str, runs: list[dict], now: datetime,
                  stale_queued_minutes: int) -> RepoState:
    latest: dict[int, dict] = {}
    for r in runs:  # API returns newest first; keep first seen per workflow
        latest.setdefault(r["workflow_id"], r)
    failing, stuck = [], []
    for r in latest.values():
        if r.get("conclusion") in FAILING:
            failing.append(FailingRun(r["name"], _pr_or_branch(r)))
        elif r.get("status") == "queued" and stale_queued_minutes > 0:
            age_min = (now - _parse_ts(r["created_at"])).total_seconds() / 60
            if age_min >= stale_queued_minutes:
                stuck.append(FailingRun(r["name"], _pr_or_branch(r)))
    key = lambda f: (f.workflow, f.ref)
    return RepoState(repo=repo, failing=sorted(failing, key=key),
                     stuck=sorted(stuck, key=key))


# --- overlay tier: shared row template (v1.5) -----------------------------------
#
# Firmware priority-arbitration finding that shapes this whole section
# (empirical probe, see the implementation report and spec doc's v1.5
# section for the full write-up): the OpenAPI doc claims "equal-priority
# requests from a different application_name override whatever is on
# screen," but probing found this is FALSE on firmware 1.1.1 -- a
# different-application_name draw at the SAME priority as the currently
# showing app is REJECTED (409 "low priority"); only a STRICTLY GREATER
# priority succeeds. This is why PRIORITY_OVERLAY (21) is strictly greater
# than the calendar's PRIORITY_AMBIENT (20) -- see busybar/display.py.
#
# Second finding: occluded elements do NOT reappear once the occluder's
# elements expire or are cleared -- they are evicted, not merely hidden.
# There is no cross-process coordination between integrations; an ambient
# app can only win the screen back with its own fresh draw landing in a
# gap. `busybar.display.overlay_gap_elapsed` plus the dwell/silence
# contract exist to make that gap real and predictable (see main.py's
# rotation loop for how the gate is applied).
#
# Geometry: the v1.4 "airy" row template (title ribbon / blank buffer row /
# full-width horizon-line track / numeral row) shared by every overlay-tier
# frame (the running badge and both quota frames below) -- independently
# declared here rather than imported from the calendar, since the two
# integrations' layouts are visually similar but structurally distinct.
OVERLAY_TITLE_X = 2
OVERLAY_TITLE_Y = -2      # ink rows 0-4 (small font); see calendar_countdown.logic
                          # for the underlying +2px text ink-offset model this assumes
OVERLAY_TITLE_WIDTH = 68  # 2px margin each side of a 72px panel
OVERLAY_TRACK_Y = 6       # row 5 is a deliberate blank buffer, same as v1.4 calendar
OVERLAY_TRACK_HEIGHT = 1
OVERLAY_NUMERAL_Y = 5     # ink rows 7-15 (large font, 9px) -- every overlay-tier
                          # numeral uses this same y and the `large` font ("numerals
                          # track together": never independently resized)

# Back-compat aliases (pre-quota-frame names) -- kept because they're
# reasonably self-documenting at their one remaining call site
# (_build_running_elements) and renaming them there too would be pure churn.
RUNNING_TITLE_X, RUNNING_TITLE_Y, RUNNING_TITLE_WIDTH = OVERLAY_TITLE_X, OVERLAY_TITLE_Y, OVERLAY_TITLE_WIDTH
RUNNING_TRACK_Y, RUNNING_TRACK_HEIGHT = OVERLAY_TRACK_Y, OVERLAY_TRACK_HEIGHT
RUNNING_NUMERAL_Y = OVERLAY_NUMERAL_Y
RUNNING_NUMERAL_X = OVERLAY_TITLE_X  # the running badge's single numeral sits at the
                                    # same left margin as the title/quota "pct" numeral

# Running-badge spinner (v1.6, config-gated via cfg["ci_status"]["running_spinner"]):
# an animated 8x8 stock asset in the panel's top-right corner. RUNNING_TITLE_WIDTH_SPINNER
# reserves that corner from the scrolling title so it never runs underneath the spinner --
# used in place of RUNNING_TITLE_WIDTH, in both the title element's own width and the
# _title_fits scroll-decision call, whenever show_spinner is on (quota frames never get a
# spinner, so OVERLAY_TITLE_WIDTH there is untouched).
RUN_SPINNER_ID = "run_spinner"
SPINNER_STOCK = "shared/spinner_front_8x8.anim"
RUNNING_TITLE_WIDTH_SPINNER = 60   # reserve the top-right 8x8 corner (spinner at x=64)


# --- running-job badge -----------------------------------------------------------

# Palette: a distinct cyan/blue "running" theme, following the same
# luminance-contrast principle established in the v1.4 calendar work (near-
# black surface + bright saturated text; no mid-luminance "dim-mud" fills).
# The track's own groove color is a decorative element, not a text-bearing
# surface, so it's allowed to be a touch brighter than the panel background
# without violating that rule -- same treatment as the calendar's TRACK_COLOR.
RUNNING_BG_GRADIENT = ["#031A2EFF", "#00060DFF"]
RUNNING_TITLE_COLOR = "#7FDBFFFF"
RUNNING_TRACK_COLOR = "#0F2A42FF"
RUNNING_TRACK_FILL_COLOR = "#29B6F6FF"   # spec: "solid cyan" -- one flat color, no gradient
RUNNING_NUMERAL_COLOR = "#66E1FFFF"

# ETA label (v1.5.1): a muted gray-blue, deliberately desaturated and
# dimmer than the bright cyan numeral it sits beside -- a secondary-tier
# color, following the same hierarchy already established between
# RUNNING_TITLE_COLOR and RUNNING_NUMERAL_COLOR (the numeral is the
# brighter/more-saturated of the two), just pushed one step further.
# Confirmed legible on-device against RUNNING_BG_GRADIENT.
RUNNING_LABEL_COLOR = "#8FA3B3FF"
RUNNING_LABEL_GAP_PX = 3   # breathing room between the eta numeral and the label
# Same 2px right margin convention as OVERLAY_TITLE_WIDTH (68 = 72 - 2*2).
RUNNING_LABEL_BUDGET_PX = PANEL_WIDTH - RUNNING_NUMERAL_X - 2
# ink rows 11-15 (small font, y=9) -- baseline-aligned with the large
# numeral's own ink rows 7-15 (OVERLAY_NUMERAL_Y=5): the calendar's
# established "+2px ink-offset" model means a small-font element at y=Y
# inks starting at Y+2, so y=9 -> ink starts at row 11, landing the
# label's bottom edge (row 15) flush with the numeral's own bottom edge
# rather than vertically centered against it. Verified on-device.
RUNNING_LABEL_Y = 9


def _pr_or_branch(run: dict) -> str:
    """PR number ("#42") if the run belongs to a pull request, else the
    branch it ran on (fork/push-triggered runs have an empty
    pull_requests array)."""
    prs = run.get("pull_requests") or []
    if prs:
        return f"#{prs[0]['number']}"
    return run.get("head_branch") or ""


def select_running_run(running_by_repo: dict[str, list[dict]]) -> tuple[dict, str, int] | None:
    """Pick the most-recently-started in_progress run across every
    configured repo. Returns (run, repo, other_running_count) or None if
    nothing is running. `other_running_count` is every other currently-
    running run (any repo, any workflow) besides the selected one -- the
    "+N" the title badge shows."""
    candidates = [(r, repo) for repo, runs in running_by_repo.items()
                 for r in runs if r.get("status") == "in_progress"]
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0].get("run_started_at", ""), reverse=True)
    best_run, best_repo = candidates[0]
    return best_run, best_repo, len(candidates) - 1


def compute_median_duration_minutes(successful_runs: list[dict]) -> float | None:
    """Median of (updated_at - run_started_at) in minutes across up to the
    first 5 runs given (the caller already requests per_page=5, but this
    stays defensive rather than trusting that). None if no run has both
    timestamps -- "no history", not an error."""
    durations = []
    for r in successful_runs[:5]:
        started, updated = r.get("run_started_at"), r.get("updated_at")
        if not started or not updated:
            continue
        durations.append((_parse_ts(updated) - _parse_ts(started)).total_seconds() / 60)
    if not durations:
        return None
    durations.sort()
    n = len(durations)
    mid = n // 2
    if n % 2 == 1:
        return durations[mid]
    return (durations[mid - 1] + durations[mid]) / 2


def _elapsed_minutes(run: dict, now: datetime) -> float:
    return (now - _parse_ts(run["run_started_at"])).total_seconds() / 60


def _format_eta_text(run: dict, median_minutes: float | None, now: datetime) -> str:
    """`~4m` / `~1h05m` (reuses _format_countdown verbatim, tilde-prefixed
    to mark it as an estimate) when history exists; `soon` once the
    estimate floors to under a minute remaining (covers both "exactly at
    or past the median" and "a few seconds under a minute left" -- neither
    reads sensibly as "~0m"); `<elapsed> in` (e.g. "3m in") when there's no
    history to estimate from at all.
    """
    elapsed = _elapsed_minutes(run, now)
    if median_minutes is None:
        return f"{_format_countdown(elapsed)} in"
    eta = max(0.0, median_minutes - elapsed)   # spec: "floored at 0"
    if int(eta) <= 0:
        return "soon"
    return f"~{_format_countdown(eta)}"


def _eta_label(eta_text: str) -> str | None:
    """"remain" / "left" / None, appended after a remaining-estimate ETA
    numeral (v1.5.1).

    Grammar guard: applies ONLY to the remaining-estimate forms
    _format_eta_text produces ("~4m", "~1h05m", "~10h", ...) -- every one
    of which is "~"-prefixed by construction. Never on "soon" (already
    imminent -- a label would read as nonsense, "soon remain") and never
    on the no-history elapsed form ("3m in" -- that's elapsed time, not a
    remaining estimate, so "remain"/"left" would be actively wrong, not
    just superfluous). The prefix check is the entire guard; there is no
    other "~"-prefixed eta_text this function ever sees.

    Fit is decided via the (extended) large-font GLYPH_ADVANCE_PX table
    -- used here as a deliberately conservative proxy for the label's
    real width even though the label itself renders in the *small* font,
    not `large`. On-device calibration: "remain" measures 22px in the
    actual small font vs. 35px as estimated through this large-font
    table; "left" measures 11px vs. 20px estimated. The large-font
    table always overestimates a small-font string's width on this
    device, so reusing it here is safe -- the failure mode is
    occasionally omitting a label that would have visually fit, never
    drawing one that clips. Prefers "remain"; falls back to "left" if
    "remain" plus the eta numeral doesn't fit within
    RUNNING_LABEL_BUDGET_PX; omits the label entirely if even "left"
    doesn't fit.
    """
    if not eta_text.startswith("~"):
        return None
    eta_width = _text_width_px(eta_text)
    for label in ("remain", "left"):
        if eta_width + RUNNING_LABEL_GAP_PX + _text_width_px(label) <= RUNNING_LABEL_BUDGET_PX:
            return label
    return None


def _progress_width(elapsed_minutes: float, median_minutes: float | None) -> int:
    """Track-fill width in px: elapsed/median of the panel width, clamped
    to [1, PANEL_WIDTH]. Full width when the ratio is unknown (no history)
    or the run has overrun its median -- same defensive shape as the
    calendar's _track_fill_width (full-on-bad-denominator rather than
    raising or dividing by zero)."""
    if median_minutes is None or median_minutes <= 0:
        return PANEL_WIDTH
    if elapsed_minutes <= 0:
        return 1
    width = round(PANEL_WIDTH * elapsed_minutes / median_minutes)
    return max(1, min(PANEL_WIDTH, width))


def _build_running_title(run: dict, repo: str, other_count: int) -> str:
    """"REPO #PR WORKFLOW" (or "REPO branch-name WORKFLOW" for fork/push
    runs), with a "+N" suffix when other runs are also active."""
    ref = _pr_or_branch(run)
    workflow = run.get("name") or ""
    parts = [p for p in (repo, ref, workflow) if p]
    text = " ".join(parts)
    if other_count > 0:
        text = f"{text} +{other_count}"
    return ascii_safe(text).upper()


def _build_running_elements(info: RunningInfo, timeout_s: int, show_spinner: bool = False) -> list[dict]:
    """v1.4-language badge: gradient bg, title ribbon (scrolls if it
    doesn't fit), full-width horizon-line track repurposed as elapsed/
    median progress, and a large ETA numeral -- no card surfaces, no
    native countdown element, same design lineage as the v1.4 calendar
    layout. Draw order is z-order, first = behind.

    `show_spinner` (v1.6, config-gated): when true, reserves the panel's
    top-right 8x8 corner from the title ribbon (RUNNING_TITLE_WIDTH_SPINNER
    in place of RUNNING_TITLE_WIDTH, for both the element's own width and
    the scroll-fit decision, so the two stay consistent) and appends an
    animated spinner element there. Defaults to False so existing callers
    are unaffected.
    """
    title_text = _build_running_title(info.run, info.repo, info.other_count)
    eta_text = _format_eta_text(info.run, info.median_minutes, info.now)
    elapsed = _elapsed_minutes(info.run, info.now)
    track_width = _progress_width(elapsed, info.median_minutes)
    title_width = RUNNING_TITLE_WIDTH_SPINNER if show_spinner else RUNNING_TITLE_WIDTH

    bg_element = {
        "id": "bg", "type": "rectangle", "x": 0, "y": 0,
        "width": PANEL_WIDTH, "height": PANEL_HEIGHT,
        "fill": "gradient_v", "fill_colors": RUNNING_BG_GRADIENT,
        "border_width": 0, "timeout": timeout_s,
    }
    title_element = {
        "id": "title", "type": "text", "text": title_text, "font": "small",
        "color": RUNNING_TITLE_COLOR, "x": RUNNING_TITLE_X, "y": RUNNING_TITLE_Y,
        "width": title_width, "timeout": timeout_s,
    }
    if not _title_fits(title_text, title_width):
        title_element.update({
            "scroll_rate": SCROLL_RATE,
            "scroll_start_delay": SCROLL_DELAY_MS,
            "scroll_repeat_delay": SCROLL_DELAY_MS,
        })
    track_element = {
        "id": "track", "type": "rectangle", "x": 0, "y": RUNNING_TRACK_Y,
        "width": PANEL_WIDTH, "height": RUNNING_TRACK_HEIGHT, "fill": "solid",
        "fill_colors": [RUNNING_TRACK_COLOR], "border_width": 0, "timeout": timeout_s,
    }
    track_fill_element = {
        "id": "track_fill", "type": "rectangle", "x": 0, "y": RUNNING_TRACK_Y,
        "width": track_width, "height": RUNNING_TRACK_HEIGHT, "fill": "solid",
        "fill_colors": [RUNNING_TRACK_FILL_COLOR], "border_width": 0, "timeout": timeout_s,
    }
    numeral_element = {
        "id": "eta", "type": "text", "text": eta_text, "font": "large",
        "color": RUNNING_NUMERAL_COLOR, "x": RUNNING_NUMERAL_X, "y": RUNNING_NUMERAL_Y,
        "timeout": timeout_s,
    }
    elements = [bg_element, title_element, track_element, track_fill_element, numeral_element]

    # ETA label (v1.5.1): appended only when _eta_label decides it fits
    # (grammar guard + width check -- see its docstring). This changes
    # the badge's own element-id shape (adds "eta_label") between polls
    # where the label appears/disappears as the ETA text's own width
    # changes over the run's lifetime -- no special handling needed here
    # for that: main.py's unified shape tracker (frozenset of element
    # ids on whatever was last actually drawn) already detects any shape
    # change and clears first, generically, not just at tier boundaries.
    label = _eta_label(eta_text)
    if label is not None:
        elements.append({
            "id": "eta_label", "type": "text", "text": label, "font": "small",
            "color": RUNNING_LABEL_COLOR,
            "x": RUNNING_NUMERAL_X + _text_width_px(eta_text) + RUNNING_LABEL_GAP_PX,
            "y": RUNNING_LABEL_Y, "timeout": timeout_s,
        })

    if show_spinner:
        elements.append({"id": RUN_SPINNER_ID, "type": "animation", "stock_path": SPINNER_STOCK,
                         "x": 64, "y": 0, "loop": True, "timeout": timeout_s})
    return elements


# --- API-quota overlay frames -----------------------------------------------------
#
# GET /rate_limit is exempt from GitHub's own rate limiting (free to call),
# so it's fetched fresh every poll while the overlay tier is active -- no
# ETag caching needed or attempted (see RestPoller.fetch_rate_limit).
#
# Palette: headroom-themed (not per-bucket -- the same three-tier scheme
# applies to both GraphQL and REST), following the same near-black-surface
# + bright-saturated-text contrast principle as everywhere else in this
# codebase. "high" (>50% remaining) reads as calm green-teal, "medium"
# (20-50%) as a caution amber, "low" (<20%) as an alert-adjacent red --
# distinct hue families from RUNNING_*'s cyan/blue so a glance tells quota
# frames apart from the CI badge even before reading the title. Per the
# brief's "same structure as other states" framing, theming covers bg,
# track_fill (the moving/informative part of the track), and both
# numerals; the track's own groove stays a single fixed neutral color
# (QUOTA_TRACK_COLOR), matching the "groove is decorative, not themed"
# convention used for RUNNING_TRACK_COLOR and the calendar's TRACK_COLOR.
QUOTA_HEADROOM_HIGH = "high"
QUOTA_HEADROOM_MEDIUM = "medium"
QUOTA_HEADROOM_LOW = "low"

QUOTA_TRACK_COLOR = "#12241EFF"

QUOTA_BG_GRADIENT = {
    QUOTA_HEADROOM_HIGH: ["#031F17FF", "#000A08FF"],
    QUOTA_HEADROOM_MEDIUM: ["#231400FF", "#0A0400FF"],
    QUOTA_HEADROOM_LOW: ["#2E0509FF", "#0A0101FF"],
}
QUOTA_TITLE_COLOR = {
    QUOTA_HEADROOM_HIGH: "#6FFFCFFF",
    QUOTA_HEADROOM_MEDIUM: "#FFCB6BFF",
    QUOTA_HEADROOM_LOW: "#FF6B7AFF",
}
QUOTA_TRACK_FILL_COLOR = {
    QUOTA_HEADROOM_HIGH: "#33FFC1FF",
    QUOTA_HEADROOM_MEDIUM: "#FFB300FF",
    QUOTA_HEADROOM_LOW: "#FF3B4EFF",
}
QUOTA_NUMERAL_COLOR = {
    QUOTA_HEADROOM_HIGH: "#7CFFE0FF",
    QUOTA_HEADROOM_MEDIUM: "#FFD98CFF",
    QUOTA_HEADROOM_LOW: "#FF8A96FF",
}

# Two numerals share the row (percentage remaining on the left, reset-in on
# the right) -- the only overlay frame that does, since the running badge
# has just one. No divider element between them (the brief didn't ask for
# one); RESET_X leaves both enough room for their respective worst cases
# ("100%" on the left, an hours-form countdown on the right).
QUOTA_PCT_X = OVERLAY_TITLE_X   # 2 -- same left margin as every other overlay element
QUOTA_RESET_X = 40


def _quota_headroom(remaining_pct: float) -> str:
    """>50% remaining -> "high"; 20-50% inclusive -> "medium"; <20% ->
    "low". The 50 boundary belongs to "medium" (a literal reading of the
    brief's "20-50%" as an inclusive range); the 20 boundary also belongs
    to "medium" (i.e. "low" is strictly less than 20, the tightest/most
    urgent tier only firing once headroom has genuinely dropped below the
    named threshold, not merely reached it)."""
    if remaining_pct < 20:
        return QUOTA_HEADROOM_LOW
    if remaining_pct <= 50:
        return QUOTA_HEADROOM_MEDIUM
    return QUOTA_HEADROOM_HIGH


def _quota_used_width(used: float, limit: float) -> int:
    """Track-fill width in px: used/limit of the panel width, clamped to
    [1, PANEL_WIDTH]. Same defensive shape as _progress_width/
    _track_fill_width -- full width when the denominator is unusable."""
    if limit <= 0:
        return PANEL_WIDTH
    if used <= 0:
        return 1
    width = round(PANEL_WIDTH * used / limit)
    return max(1, min(PANEL_WIDTH, width))


def parse_rate_limit(data: dict) -> dict[str, dict] | None:
    """Extract the `core` (REST) and `graphql` buckets from a raw
    `GET /rate_limit` response into `{"core": {...}, "graphql": {...}}`
    (`used` is computed defensively as `limit - remaining` if the API
    response doesn't include it directly). Only well-formed buckets are
    included -- a response with one usable bucket and one malformed/absent
    one still returns the usable one, rather than discarding both. `None`
    if neither bucket could be parsed at all, signaling "this whole fetch
    was unusable" to the caller (which should skip quota frames that
    cycle, not crash or show stale data -- see RestPoller.fetch_rate_limit
    and main.py's staleness handling).
    """
    resources = (data or {}).get("resources") or {}
    result: dict[str, dict] = {}
    for key in ("core", "graphql"):
        bucket = resources.get(key)
        if not bucket or "limit" not in bucket or "remaining" not in bucket or "reset" not in bucket:
            continue
        limit, remaining = bucket["limit"], bucket["remaining"]
        used = bucket.get("used")
        if used is None:
            used = max(0, limit - remaining)
        result[key] = {"limit": limit, "remaining": remaining, "used": used, "reset": bucket["reset"]}
    return result or None


def _build_quota_elements(info: QuotaInfo, timeout_s: int) -> list[dict]:
    """Same v1.4-language row template as the running badge (gradient bg,
    title ribbon, full-width track), but themed by remaining-quota
    headroom instead of CI state, and with two numerals sharing the bottom
    row (percentage remaining on the left, reset-in on the right) instead
    of one. Draw order is z-order, first = behind.
    """
    remaining_pct = (info.remaining / info.limit * 100) if info.limit > 0 else 0.0
    headroom = _quota_headroom(remaining_pct)
    pct_text = f"{int(remaining_pct)}%"   # floored, not rounded -- same numeral-floor
                                          # convention as _format_countdown throughout
    reset_in_minutes = (info.reset_epoch - info.now.timestamp()) / 60
    reset_text = _format_countdown(reset_in_minutes)
    used_width = _quota_used_width(info.used, info.limit)

    bg_element = {
        "id": "bg", "type": "rectangle", "x": 0, "y": 0,
        "width": PANEL_WIDTH, "height": PANEL_HEIGHT,
        "fill": "gradient_v", "fill_colors": QUOTA_BG_GRADIENT[headroom],
        "border_width": 0, "timeout": timeout_s,
    }
    title_text = ascii_safe(info.label).upper()
    title_element = {
        "id": "title", "type": "text", "text": title_text, "font": "small",
        "color": QUOTA_TITLE_COLOR[headroom], "x": OVERLAY_TITLE_X, "y": OVERLAY_TITLE_Y,
        "width": OVERLAY_TITLE_WIDTH, "timeout": timeout_s,
    }
    if not _title_fits(title_text, OVERLAY_TITLE_WIDTH):
        title_element.update({
            "scroll_rate": SCROLL_RATE,
            "scroll_start_delay": SCROLL_DELAY_MS,
            "scroll_repeat_delay": SCROLL_DELAY_MS,
        })
    track_element = {
        "id": "track", "type": "rectangle", "x": 0, "y": OVERLAY_TRACK_Y,
        "width": PANEL_WIDTH, "height": OVERLAY_TRACK_HEIGHT, "fill": "solid",
        "fill_colors": [QUOTA_TRACK_COLOR], "border_width": 0, "timeout": timeout_s,
    }
    track_fill_element = {
        "id": "track_fill", "type": "rectangle", "x": 0, "y": OVERLAY_TRACK_Y,
        "width": used_width, "height": OVERLAY_TRACK_HEIGHT, "fill": "solid",
        "fill_colors": [QUOTA_TRACK_FILL_COLOR[headroom]], "border_width": 0, "timeout": timeout_s,
    }
    pct_element = {
        "id": "pct", "type": "text", "text": pct_text, "font": "large",
        "color": QUOTA_NUMERAL_COLOR[headroom], "x": QUOTA_PCT_X, "y": OVERLAY_NUMERAL_Y,
        "timeout": timeout_s,
    }
    reset_element = {
        "id": "reset", "type": "text", "text": reset_text, "font": "large",
        "color": QUOTA_NUMERAL_COLOR[headroom], "x": QUOTA_RESET_X, "y": OVERLAY_NUMERAL_Y,
        "timeout": timeout_s,
    }
    return [bg_element, title_element, track_element, track_fill_element, pct_element, reset_element]


# --- overlay rotation --------------------------------------------------------------

OVERLAY_FRAME_CI_BADGE = "ci_badge"
OVERLAY_FRAME_QUOTA_GQL = "quota_gql"
OVERLAY_FRAME_QUOTA_REST = "quota_rest"

# Element id sets differ between the CI badge ("eta") and either quota frame
# ("pct", "reset") -- the draw endpoint upserts by id within an
# application_name (the same firmware behavior that required the v1.3.1
# calendar transition-clear fix), so switching between these two *shapes*
# without an explicit clear would leave a stale numeral element from the
# previous shape rendered alongside the new one. quota_gql and quota_rest
# share an identical id set, so switching between *those* needs no clear.
# NOTE: this dict is documentation/reference only -- main.py's actual
# clear-gate does NOT consult it. It instead compares the *literal*
# frozenset of element ids on each drawn payload (frozenset(e["id"] for e
# in payload["elements"])), unified across every tier that can draw to
# APP (alert, quiet-green, and both overlay frame kinds), not just these
# two overlay shapes -- see run_once's docstring in ci_status/main.py for
# why a badge/quota-only mapping here wasn't enough (it missed the
# alert<->overlay and green<->overlay seams entirely).
OVERLAY_FRAME_SHAPE = {
    OVERLAY_FRAME_CI_BADGE: "badge",
    OVERLAY_FRAME_QUOTA_GQL: "quota",
    OVERLAY_FRAME_QUOTA_REST: "quota",
}


def overlay_frame_sequence(show_quota: bool) -> list[str]:
    """The rotation order for the overlay tier's dwell slots. The running
    badge always leads (and is the only frame at all when show_quota is
    off), so a run's very first overlay draw is always the CI badge, never
    a quota frame."""
    if show_quota:
        return [OVERLAY_FRAME_CI_BADGE, OVERLAY_FRAME_QUOTA_GQL, OVERLAY_FRAME_QUOTA_REST]
    return [OVERLAY_FRAME_CI_BADGE]


def build_overlay_payload(frame_name: str, timeout_s: int, *,
                         running: RunningInfo | None = None,
                         quota_by_bucket: dict[str, QuotaInfo] | None = None,
                         show_spinner: bool = False) -> dict | None:
    """Build the {"elements", "priority", "led"} payload for one overlay-
    tier dwell slot, or `None` if this frame's data isn't available this
    cycle -- the caller must treat `None` as "skip this dwell slot
    entirely" (no draw, no clear), never substitute stale or placeholder
    content. This is what lets rate_limit fetch failures silently drop a
    quota frame from rotation for a cycle instead of crashing or showing
    minutes-old numbers (see main.py's 5-minute staleness check, which
    is what actually keeps `quota_by_bucket` fresh enough to trust here).

    `show_spinner` (v1.6) is threaded through ONLY on the CI-badge branch
    -- quota frames never get a spinner, regardless of this flag. Defaults
    to False so existing callers are unaffected.
    """
    if frame_name == OVERLAY_FRAME_CI_BADGE:
        if running is None:
            return None
        return {"elements": _build_running_elements(running, timeout_s, show_spinner=show_spinner),
                "priority": PRIORITY_OVERLAY, "led": None}
    if frame_name in (OVERLAY_FRAME_QUOTA_GQL, OVERLAY_FRAME_QUOTA_REST):
        bucket_key = "graphql" if frame_name == OVERLAY_FRAME_QUOTA_GQL else "core"
        info = (quota_by_bucket or {}).get(bucket_key)
        if info is None:
            return None
        return {"elements": _build_quota_elements(info, timeout_s),
                "priority": PRIORITY_OVERLAY, "led": None}
    return None


def _text_element(text: str, color: str, timeout_s: int, font: str = "normal") -> dict:
    return {"id": "ci", "type": "text", "text": text, "font": font,
            "x": 0, "y": 4, "width": 72, "color": color,
            "scroll_rate": 2000, "scroll_start_delay": 1000,
            "scroll_repeat_delay": 2000, "timeout": timeout_s}


def _fail_line(repo: str, fr: FailingRun) -> str:
    """"owner/repo #42 · workflow" (the ref is dropped when empty)."""
    ref = f" {fr.ref}" if fr.ref else ""
    return f"{repo}{ref} · {fr.workflow}"


def _badge_elements(text: str, bg_color: str, text_color: str, timeout_s: int) -> list[dict]:
    """Full-panel rounded-rect background + bold scrolling text over it."""
    # border_width=0: RectangleElement defaults to a 1px white border, which
    # would draw an unwanted white outline around the badge (verified on-device).
    bg = {"id": "bg", "type": "rectangle", "x": 0, "y": 0, "width": 72, "height": 16,
          "radius": 2, "fill": "solid", "fill_colors": [bg_color], "border_width": 0,
          "timeout": timeout_s}
    return [bg, _text_element(text, text_color, timeout_s, font="bold")]


def build_ci_payload(states: list[RepoState], show_green: bool, timeout_s: int,
                     overlay: dict | None = None, suppress_alert: bool = False,
                     suppress_led: bool = False) -> dict | None:
    """Precedence: failure > stuck > overlay (whichever frame the caller's
    rotation picked -- the running badge or a quota frame) > quiet green >
    nothing. Failure and stuck stay at PRIORITY_ALERT (60, unchanged) and
    are evaluated first specifically so they always win even if an overlay
    condition is also true in the same poll -- an active alert must never
    be preempted by "just" a status update. `overlay`, when given, is a
    fully pre-built payload dict from `build_overlay_payload` (already
    carrying its own `priority`/`elements`/`led`) so this function's job is
    purely precedence, not rendering.

    `suppress_alert` (v1.5.2 snooze) skips the failure/stuck branches
    entirely when true -- the caller (main.run_once, via update_snooze)
    has decided this exact alert fingerprint is currently snoozed, so
    precedence falls through to overlay/quiet-green/nothing exactly as if
    nothing were failing: "running/quota rotation and green behavior
    unaffected" per the snooze design. `suppress_led` (also v1.5.2) blanks
    the failure branch's LED specifically, without suppressing the alert
    draw itself -- used during the snooze-PENDING phase (a BUSY session is
    active but hasn't ended yet): the alert's own element draw still
    proceeds as normal (and gets naturally rejected by the session's
    higher priority, same as always), but the LED -- a separate channel
    that is NOT gated by the same priority arbitration and would otherwise
    keep blinking through the session -- is silenced once the operator has
    visibly acknowledged the alert by starting a session. `suppress_led`
    has no effect on the stuck branch (its LED is already always `None`).
    """
    failures = [(s.repo, fr) for s in states for fr in s.failing]
    stuck = [(s.repo, fr) for s in states for fr in s.stuck]
    if failures and not suppress_alert:
        text = "CI FAIL " + " ".join(_fail_line(repo, fr) for repo, fr in failures)
        led = None if suppress_led else "#FF0000FF"
        return {"elements": _badge_elements(text, "#A32D2DFF", "#FFFFFFFF", timeout_s),
                "priority": PRIORITY_ALERT, "led": led}
    if stuck and not suppress_alert:
        text = "CI stuck " + " ".join(_fail_line(repo, fr) for repo, fr in stuck)
        return {"elements": _badge_elements(text, "#BA7517FF", "#0B0B0BFF", timeout_s),
                "priority": PRIORITY_ALERT, "led": None}
    if overlay is not None:
        return overlay
    if show_green:
        return {"elements": [_text_element("CI ok", "#00FF00FF", timeout_s)],
                "priority": PRIORITY_ALERT, "led": None}
    return None


# --- alert snooze via the device's native start button (v1.5.2) -----------------
#
# Raw physical button events aren't API-observable (confirmed: the status
# WebSocket is screen-only), but the BUSY session it starts is, via
# client.get_busy(). The snooze rule rides on that: an alert showing at the
# moment a session starts is treated as "the operator saw it and pressed
# the button" -- once the session ends, that exact failure/stuck fingerprint
# is suppressed for `snooze_minutes`. Any change to the fingerprint (a new
# failure, a different workflow, resolved-then-new) immediately clears the
# snooze and re-alerts; so does the snooze's own expiry if the same
# fingerprint is still failing.

def compute_alert_fingerprint(states: list[RepoState]) -> frozenset:
    """The identity of "what's currently alerting" -- a frozenset of
    `(repo, workflow, category)` triples, category being "failing" or
    "stuck" so a repo:workflow pair moving between the two categories
    counts as a fingerprint change (not silently treated as "the same
    alert"), matching the snooze rule's "ANY fingerprint change... clear
    snooze, alert immediately." Empty (falsy) when nothing is failing or
    stuck.
    """
    return (frozenset((s.repo, name, "failing") for s in states for name in s.failing)
           | frozenset((s.repo, name, "stuck") for s in states for name in s.stuck))


def update_snooze(alert_fingerprint: frozenset, busy_active: bool | None, now: datetime,
                  snooze_minutes: int, snooze_state: dict) -> tuple[bool, bool]:
    """Advances the snooze state machine by one poll and returns
    `(suppress_alert, suppress_led)` for THIS poll. `snooze_state` is a
    caller-owned dict (same pattern as every other cache in this codebase),
    mutated in place, with up to three keys: `"session_was_active"`
    (tracked every call, for edge-detecting the inactive->active
    transition -- see below), `"fingerprint"` (the alert fingerprint a
    pending-or-active snooze applies to), and `"snooze_until"` (absent
    while pending -- session still running -- set to a datetime once the
    session ends and the timed snooze begins).

    State machine:
    1. **Pending** starts only on a genuine inactive -> active transition
       (edge, not level -- see below) while an alert is currently showing:
       records `fingerprint`, no `snooze_until` yet. Returns
       `(False, True)` -- the alert draw itself still proceeds as normal
       (and will be naturally rejected by the session's own higher
       priority, same as always), but its LED is suppressed, since LED is
       a separate channel not gated by that same priority arbitration and
       would otherwise keep blinking through the session the operator just
       acknowledged.
    2. While still **pending** (fingerprint unchanged, session still
       active): keeps returning `(False, True)`.
    3. The session **ends** (busy_active goes false) while still pending,
       same fingerprint: sets `snooze_until = now + snooze_minutes` and
       returns `(True, False)` -- the timed snooze begins this exact poll.
    4. While **timed** and `now < snooze_until`, same fingerprint:
       `(True, False)` -- alert suppressed entirely (falls through to
       overlay/quiet-green/nothing in build_ci_payload), no LED question
       even arises since the alert branch never runs.
    5. **Expiry** (`now >= snooze_until`): state clears, `(False, False)`
       -- back to alerting normally if still failing.
    6. **Any fingerprint change** at any pending/timed point: immediately
       clears the fingerprint/snooze_until (but not the
       `session_was_active` tracking -- see below), falling through to
       step 1's logic fresh for the new fingerprint (or `(False, False)`
       if nothing is failing/stuck anymore, or if a session isn't already
       active for a fresh pending to start against).
    7. `snooze_minutes <= 0` disables the feature outright: any existing
       fingerprint/snooze_until is cleared and `(False, False)` always.

    **Edge, not level, and why the default matters.** Requirement 1 is a
    TRANSITION ("busy snapshot transitions from inactive... to active"),
    not a level condition ("an alert is showing and a session happens to
    be active") -- otherwise a session that was ALREADY running before an
    alert appeared (or before a fingerprint changed mid-session) would be
    wrongly treated as "you just pressed the button for this," silently
    snoozing something the operator never actually acknowledged. Detecting
    the edge needs to know what the PREVIOUS poll observed, tracked via
    `session_was_active` -- but polling is deliberately gated (see
    main.run_once) to skip `get_busy()` entirely when idle (no alert, no
    snooze state), which means there can be gaps where `session_was_active`
    wasn't being updated.

    **Critical correctness point (fixed after an initial version got this
    wrong -- see the regression tests): `session_was_active` is committed
    ONLY on a poll where `get_busy()` was ACTUALLY called this cycle**,
    signaled by `busy_active` being a real `bool` rather than `None`.
    `main.run_once` passes `None` for `busy_active` whenever polling was
    gated off (idle: no alert, no existing snooze state). An earlier
    version unconditionally wrote `busy_active` every call, including a
    dummy `False` for gated-off polls -- which meant a sequence of idle
    polls (no alert yet) would stamp `session_was_active = False`
    regardless of the device's ACTUAL state; if a session then started
    while STILL idle (unobserved, since nothing was polling), and only
    THEN did an alert appear (triggering the first real `get_busy()` call,
    correctly observing `busy_active=True`), the stale `False` from the
    dummy writes would read as "was NOT active a moment ago, now IS
    active" -- a spurious transition -- and silently start a pending
    snooze for a failure the operator never acknowledged. Committing only
    on an actual poll (leaving `session_was_active` untouched otherwise)
    closes this: an unobserved period leaves the value at whatever it was
    (or its default) rather than being corrupted by an unpolled guess.

    On the first EVER poll of a fresh process, or the first poll after any
    gap where `session_was_active` was never committed, it defaults to
    `True` -- not `False` -- so an as-yet-unobserved busy session is
    assumed to possibly PRE-DATE the alert rather than assumed absent:
    the conservative direction is to require an actually-OBSERVED
    inactive->active transition before granting pending, at the cost of
    occasionally missing a legitimate fresh session-start that happens to
    coincide with polling just resuming (a minor inconvenience -- the
    operator presses the button again -- versus the alternative of a
    silent, unintended auto-snooze).

    **Restart safety**: a process restart gets a fresh empty
    `snooze_state`, so `session_was_active` defaults to `True` on the very
    first poll regardless of the device's actual state -- the same
    conservative default above, which also happens to correctly prevent a
    restart-during-an-already-active-session from being misattributed as
    a fresh button press. In-memory only; documented as a known limitation
    (a snooze in effect at restart is lost, same as every other cache in
    this codebase).
    """
    was_active = snooze_state.get("session_was_active", True)
    if busy_active is not None:
        snooze_state["session_was_active"] = busy_active

    if snooze_minutes <= 0:
        snooze_state.pop("fingerprint", None)
        snooze_state.pop("snooze_until", None)
        return False, False

    pending_fp = snooze_state.get("fingerprint")
    snooze_until = snooze_state.get("snooze_until")

    if pending_fp is not None and alert_fingerprint != pending_fp:
        snooze_state.pop("fingerprint", None)
        snooze_state.pop("snooze_until", None)
        pending_fp = None
        snooze_until = None

    if pending_fp is None:
        if alert_fingerprint and busy_active and not was_active:
            snooze_state["fingerprint"] = alert_fingerprint
            snooze_state.pop("snooze_until", None)
            return False, True
        return False, False

    if snooze_until is None:
        # Defensive: main.run_once's polling gate guarantees busy_active
        # is a real bool (not None) whenever pending_fp is set (a pending
        # snooze always keeps polling -- see should_poll_busy), so this
        # should never actually see None here. If it somehow did anyway,
        # treat "unknown" the same as "still active" (stay pending rather
        # than prematurely starting the timed snooze on an unpolled
        # guess) -- the same conservative direction as everywhere else in
        # this function.
        if busy_active is None or busy_active:
            return False, True
        snooze_state["snooze_until"] = now + timedelta(minutes=snooze_minutes)
        return True, False

    if now < snooze_until:
        return True, False

    snooze_state.pop("fingerprint", None)
    snooze_state.pop("snooze_until", None)
    return False, False
