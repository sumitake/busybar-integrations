import sys
from pathlib import Path

try:
    import busybar  # noqa: F401
except ImportError:  # bare clone / broken editable install: use the repo's src/
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import argparse
import logging
import time
from datetime import datetime, timedelta, timezone

from busybar.client import BusyBarClient, DrawResult
from busybar.config import device_kwargs, load_config
from busybar.display import OVERLAY_DWELL_SECONDS, PRIORITY_OVERLAY, overlay_gap_elapsed

from .logic import (
    RepoState, RunningInfo, QuotaInfo,
    build_overlay_payload, build_overlay_sequence, evaluate_runs,
    LED_OFF_COLOR, LED_OFF_ELEMENTS, OVERLAY_FRAME_QUOTA_GQL, OVERLAY_FRAME_QUOTA_REST,
    parse_rate_limit, resolve_ci_led_value, resolve_repo_list, select_running_run,
)

APP = "ci_status"
log = logging.getLogger(APP)

QUOTA_LABELS = {"graphql": "GITHUB GRAPHQL", "core": "GITHUB REST"}
QUOTA_STALE_SECONDS = 300  # never show rate_limit data older than 5 minutes


def _refresh_account_repos(poller, repo_cache: dict | None, now: datetime,
                           repo_refresh_minutes: int) -> list[dict] | None:
    """Re-enumerate the account's owned repos (v1.5.1) when `repo_cache`
    is stale or has never been populated, keeping the previous list on an
    enumeration failure -- never crash, never silently fall back to an
    empty watch list (an empty result from `fetch_account_repos` is
    indistinguishable from "genuinely zero repos," so `None` is the only
    signal treated as "keep what we had"; see that method's docstring).
    Mirrors `_refresh_quota`'s cache-freshness pattern.
    """
    if repo_cache is None:
        return None
    fetched_at = repo_cache.get("fetched_at")
    stale = fetched_at is None or (now - fetched_at).total_seconds() > repo_refresh_minutes * 60
    if stale:
        fetched = poller.fetch_account_repos()
        if fetched is not None:
            repo_cache["repos"] = fetched
            repo_cache["fetched_at"] = now
        elif repo_cache.get("repos") is None:
            log.warning("account repo enumeration failed and no previous list is "
                       "cached yet -- watch_account_repos contributes nothing this poll")
    return repo_cache.get("repos")


def _refresh_quota(poller, quota_cache: dict | None, now: datetime) -> dict[str, QuotaInfo] | None:
    """Fetch /rate_limit fresh (it's exempt from GitHub's own rate
    limiting, so there's no cost to calling it every poll) and update
    `quota_cache` on success. Returns the current bucket->QuotaInfo mapping
    if `quota_cache` holds data no older than QUOTA_STALE_SECONDS (whether
    from this fetch or an earlier one that succeeded when this one
    didn't), else None -- callers must treat None as "no quota frames this
    cycle," never fall back to stale numbers.
    """
    if quota_cache is None:
        return None
    raw = poller.fetch_rate_limit()
    if raw is not None:
        parsed = parse_rate_limit(raw)
        if parsed is not None:
            quota_cache["buckets"] = parsed
            quota_cache["fetched_at"] = now
    fetched_at = quota_cache.get("fetched_at")
    if fetched_at is None or (now - fetched_at).total_seconds() > QUOTA_STALE_SECONDS:
        return None
    buckets = quota_cache.get("buckets") or {}
    return {
        key: QuotaInfo(label=QUOTA_LABELS[key], limit=buckets[key]["limit"],
                      remaining=buckets[key]["remaining"], used=buckets[key]["used"],
                      reset_epoch=buckets[key]["reset"], now=now)
        for key in buckets if key in QUOTA_LABELS
    }


def run_once(client, poller, cfg: dict, now: datetime,
             state_cache: dict[str, RepoState], dry_run: bool,
             running_cache: dict[str, list[dict]] | None = None,
             overlay_state: dict | None = None,
             quota_cache: dict | None = None,
             repo_cache: dict | None = None) -> str:
    """`running_cache`, `overlay_state`, `quota_cache`, and `repo_cache`,
    when passed, are caller-owned dicts this function mutates in place
    (mirroring `state_cache`'s existing pattern) so `main()` can hold one
    instance of each across loop iterations while `run_once` itself stays
    a pure function of its arguments plus those dicts. Omitting
    `running_cache` (the default) skips running-job/overlay detection
    entirely; omitting `repo_cache` (the default) skips account-wide
    discovery entirely and falls back to the pre-v1.5.1 behavior of
    polling exactly `cfg["ci_status"]["repos"]` every cycle.

    Account-wide watching (v1.5.1): when `repo_cache` is given, the
    effective repo list for this poll is resolved fresh each call via
    `resolve_repo_list` (cheap -- it's a set operation over already-cached
    data, not a network call) from `repos`/`repos_exclude`/
    `watch_account_repos`/`active_within_days`, re-enumerating the
    account's repos via `_refresh_account_repos` only when that cache is
    older than `repo_refresh_minutes` (or empty). Any repo present in
    `state_cache`/`running_cache` but absent from the freshly-resolved
    list -- excluded, aged out of the active window, or deleted upstream
    -- has its cached state dropped (and the poller's own per-repo ETag
    slots forgotten via `forget_repo`) so a stale failure/stuck alert or
    running badge can't linger for a repo that's no longer being watched.

    Overlay rotation: the overlay tier draws one frame per dwell slot,
    cycling through `build_overlay_sequence(...)`'s ordered frame list (see
    the "Failure/stuck/green rotation" paragraph below for what populates
    it). A dwell slot only fires once `overlay_gap_elapsed(last_dwell_end, now) >=
    OVERLAY_DWELL_SECONDS` (busybar.display's contract: stay silent at
    least one full dwell so the ambient calendar has a real chance to
    reclaim the screen in between -- see busybar/display.py and the spec
    doc's v1.5 section for why). `overlay_state`'s `frame_index` and
    `last_dwell_end` only commit once `client.draw` actually returns
    DRAWN, same discipline as calendar_countdown's transition-state fix: a
    failed draw must not be mistaken for a completed dwell, or the
    rotation would silently skip frames / wait a dwell for nothing.

    `overlay_state["last_shape"]` is a *unified* shape tracker, not
    overlay-specific despite living in this dict: it records the element-id
    set of whatever payload was last actually drawn to `APP`, across every
    tier that can draw here -- an alert badge, the quiet-green text, or
    either overlay frame kind -- and every draw path below checks it before
    drawing and commits to it after DRAWN. The firmware upserts by element
    id within an `application_name`, and each of these payload shapes has a
    different id set (`{bg, ci}` for an alert, `{ci}` alone for quiet
    green, `{bg, title, track, track_fill, eta}` for the running badge,
    `{bg, title, track, track_fill, pct, reset}` for a quota frame) --
    switching shapes without a clear() first leaves the previous shape's
    now-orphaned ids rendered until their own timeout elapses (up to 1.5x
    `poll_seconds` for an alert/green draw), the same upsert-by-id bug
    class the v1.3.1 calendar transition-clear fix addressed, recurring at
    every seam a different payload shape can follow another -- not just
    between the two overlay-frame shapes. Critically, resetting the
    rotation bookkeeping (`frame_index`/`last_dwell_end`, e.g. when an
    alert preempts the overlay or a run ends) must NOT also reset
    `last_shape`: that field describes what is physically on the device
    right now, which a bookkeeping reset does not change, and clearing it
    prematurely was the root cause of a real bug where the clear-gate saw
    "no shape on record" and wrongly concluded no clear was needed on the
    next transition.

    Failure/stuck/green rotation (v1.6, Request B): the overlay tier's
    rotation is no longer gated on a run being active -- `build_overlay_sequence`
    folds in one frame per failing run, one per stuck run, the running CI
    badge (only while a run is active), each available quota frame, and a
    single quiet-green frame (only when nothing else is present and
    `show_green` is on), and the dwell/silence gate above applies uniformly
    across all of them. This means a failure rotates into view and keeps
    rotating even with no CI run currently in progress, instead of only
    showing while `running_cache`/`show_running` happened to have something
    active. `overlay_state["led_was_on"]` tracks the failure-driven LED
    across polls (see `resolve_ci_led_value`): it commits to
    `led_should_be_on` only once a draw actually lands (DRAWN), same DRAWN
    discipline as `frame_index`/`last_dwell_end`.
    """
    c = cfg["ci_status"]
    timeout_s = int(c["poll_seconds"] * 1.5)

    if repo_cache is not None:
        account_repos = (_refresh_account_repos(poller, repo_cache, now,
                                                 c.get("repo_refresh_minutes", 60))
                         if c.get("watch_account_repos") else None)
        effective_repos = resolve_repo_list(
            c["repos"], c.get("repos_exclude", []), bool(c.get("watch_account_repos")),
            account_repos, c.get("active_within_days", 30), now)
    else:
        effective_repos = c["repos"]

    # Drop cached state for any repo that left the effective list this
    # poll (excluded, aged out, deleted upstream) so a stale alert or
    # running badge can't linger for a repo no longer being watched.
    for repo in set(state_cache) - set(effective_repos):
        state_cache.pop(repo, None)
        poller.forget_repo(repo)
    if running_cache is not None:
        for repo in set(running_cache) - set(effective_repos):
            running_cache.pop(repo, None)
            poller.forget_repo(repo)

    for repo in effective_repos:
        runs = poller.fetch_runs(repo)
        if runs is not None:  # None = 304/no-change/error -> keep cached state
            state_cache[repo] = evaluate_runs(repo, runs, now,
                                              c["stale_queued_minutes"])

    states = list(state_cache.values())

    # Running detection (unchanged gating): a live run enables the CI badge and,
    # with show_quota, the quota frames. Independent of failures, which rotate
    # regardless of whether anything is running.
    running_info = None
    running_present = False
    quota_by_bucket = None
    quota_frames: list[str] = []
    if running_cache is not None and c["show_running"]:
        for repo in effective_repos:
            running_runs = poller.fetch_running_runs(repo)
            if running_runs is not None:
                running_cache[repo] = running_runs
        selected = select_running_run(running_cache)
        if selected is not None:
            run, repo, other_count = selected
            median = poller.fetch_median_eta(repo, run["workflow_id"])
            running_info = RunningInfo(run=run, repo=repo, other_count=other_count,
                                       median_minutes=median, now=now)
            running_present = True
            if c["show_quota"]:
                quota_by_bucket = _refresh_quota(poller, quota_cache, now)
                if quota_by_bucket:
                    if "graphql" in quota_by_bucket:
                        quota_frames.append(OVERLAY_FRAME_QUOTA_GQL)
                    if "core" in quota_by_bucket:
                        quota_frames.append(OVERLAY_FRAME_QUOTA_REST)

    sequence = build_overlay_sequence(states, running_present=running_present,
                                      quota_frames=quota_frames, show_green=c["show_green"])

    # Failure-driven LED (Request B keeps a gentle cue). led_should_be_on is
    # driven by FAILURES only -- stuck keeps its historical LED-None behavior.
    led_should_be_on = any(s.failing for s in states)
    led_was_on = bool(overlay_state.get("led_was_on")) if overlay_state is not None else False
    led_value = resolve_ci_led_value(led_should_be_on, led_was_on)

    # Empty sequence -> nothing to show. Honor an explicit LED-off transition
    # (there is no failure now, so led_value is either LED_OFF_COLOR or None).
    if not sequence:
        if overlay_state is not None:
            overlay_state.pop("frame_index", None)
            overlay_state.pop("last_dwell_end", None)
        if dry_run:
            return "DRY-RUN: nothing to show"
        if led_value == LED_OFF_COLOR:
            result = client.draw(APP, LED_OFF_ELEMENTS, priority=PRIORITY_OVERLAY,
                                 led_notification_color=LED_OFF_COLOR)
            if result == DrawResult.DRAWN and overlay_state is not None:
                overlay_state["led_was_on"] = False
                overlay_state["last_shape"] = frozenset(e["id"] for e in LED_OFF_ELEMENTS)
            return f"led off; {result.value}"
        client.clear(APP)
        if overlay_state is not None:
            overlay_state["last_shape"] = None
        return "nothing to show; cleared"

    # Dwell gate: one frame per dwell, then silent one dwell so the ambient
    # calendar can reclaim the gap. frame_index/last_dwell_end commit only on DRAWN.
    seq_len = len(sequence)
    frame_index = (overlay_state.get("frame_index", 0) if overlay_state is not None else 0) % seq_len
    last_dwell_end = overlay_state.get("last_dwell_end") if overlay_state is not None else None
    if overlay_gap_elapsed(last_dwell_end, now) < OVERLAY_DWELL_SECONDS:
        return "overlay dwell gap; staying silent (letting the ambient app reclaim the screen)"

    payload = build_overlay_payload(sequence[frame_index], OVERLAY_DWELL_SECONDS,
                                    running=running_info, quota_by_bucket=quota_by_bucket,
                                    show_spinner=c.get("running_spinner", False))

    if dry_run:
        return f"DRY-RUN payload: {payload!r} led={led_value}"

    # Unified shape-clear gate (see the original docstring): the firmware upserts
    # by element id within an application_name, so a shape change needs a clear
    # first. Spans every frame kind that can draw here.
    shape = frozenset(e["id"] for e in payload["elements"])
    if overlay_state is not None:
        last_shape = overlay_state.get("last_shape")
        if last_shape is not None and last_shape != shape:
            client.clear(APP)

    result = client.draw(APP, payload["elements"], priority=payload["priority"],
                         led_notification_color=led_value)
    if result == DrawResult.DRAWN and overlay_state is not None:
        overlay_state["last_shape"] = shape
        overlay_state["led_was_on"] = led_should_be_on
        overlay_state["frame_index"] = frame_index + 1
        overlay_state["last_dwell_end"] = now + timedelta(seconds=OVERLAY_DWELL_SECONDS)

    text = next(e["text"] for e in payload["elements"] if e["type"] == "text")
    return f"{text[:40]!r} -> {result.value}"


def next_poll_seconds(cfg_ci: dict, running_cache: dict[str, list[dict]]) -> int:
    """Cadence switch: `running_poll_seconds` while any *currently
    watched* repo has a running run, `poll_seconds` otherwise. Checks
    every key `running_cache` actually holds (not `cfg_ci["repos"]`) --
    account-wide watching (v1.5.1) means the set of repos with entries in
    `running_cache` can include auto-discovered repos that were never in
    the explicit `repos` list at all; iterating `cfg_ci["repos"]` would
    silently miss an active run on any of those and never shorten the
    poll interval for it. A pure function of `running_cache`'s
    post-`run_once` state so it's testable without mocking `time.sleep`.
    """
    any_running = any(running_cache.values())
    return cfg_ci["running_poll_seconds"] if any_running else cfg_ci["poll_seconds"]


def config_requires_repos(cfg: dict) -> str | None:
    """Validates that this config gives ci_status *something* to watch --
    either an explicit `repos` list, or `watch_account_repos = true`
    (which discovers repos at runtime, so an empty `repos` list is valid
    in that mode -- see v1.5.1's account-wide watching). Returns the
    error message to log if neither is satisfied, else None. Pulled out
    of main() as a pure function of `cfg` so this validation is testable
    without exercising the rest of main()'s side effects (argparse,
    logging setup, gh auth, device connection).
    """
    if not cfg["ci_status"]["repos"] and not cfg["ci_status"].get("watch_account_repos"):
        return ("No repos configured. Copy config.example.toml to config.toml "
               "and set [ci_status] repos, or set watch_account_repos = true.")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="BUSY Bar CI status")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = load_config()
    error = config_requires_repos(cfg)
    if error is not None:
        log.error(error)
        return 1
    from .github import RestPoller, get_token
    try:
        poller = RestPoller(get_token())
    except RuntimeError as exc:
        log.error(str(exc))
        return 1
    client = BusyBarClient(**device_kwargs(cfg))
    client.clear(APP)  # drop any stale elements from a previous process (type collisions 400)

    state_cache: dict[str, RepoState] = {}
    running_cache: dict[str, list[dict]] = {}
    overlay_state: dict = {}
    quota_cache: dict = {}
    repo_cache: dict = {}
    backoff = 5
    while True:
        summary = run_once(client, poller, cfg, datetime.now(timezone.utc),
                           state_cache, args.dry_run, running_cache=running_cache,
                           overlay_state=overlay_state, quota_cache=quota_cache,
                           repo_cache=repo_cache)
        log.info(summary)
        if args.once:
            return 0
        if summary.endswith("unreachable"):  # device offline: back off, not full poll
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
        else:
            backoff = 5
            time.sleep(next_poll_seconds(cfg["ci_status"], running_cache))


if __name__ == "__main__":
    raise SystemExit(main())
