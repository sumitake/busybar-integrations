# CI Failure as a Calm Rotation Frame — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the CI failure/stuck screen show the repo + PR number and rotate calmly at the overlay tier (alternating with the calendar, running badge, and quota) instead of camping the panel at priority 60, keeping a gentle red LED while anything fails and retiring the now-unneeded snooze subsystem.

**Architecture:** Enrich `evaluate_runs` to keep a `FailingRun(workflow, ref)` per failing/stuck run; add fail/stuck/green frame builders and a descriptor-based sequence builder to `ci_status.logic`; rewrite `ci_status.main.run_once` so the existing overlay dwell/silence rotation is driven by a dynamic sequence that includes failures (even with no job running) and a centralized failure-driven LED lifecycle mirroring `calendar_countdown`'s; then delete the old priority-60 `build_ci_payload` path and the snooze state machine.

**Tech Stack:** Python 3.12, pytest, stdlib + existing `BusyBarClient`. No new dependencies.

## Global Constraints

- **Python floor:** 3.12+ (matches repo).
- **No new dependencies.** stdlib + existing `busybar` package only.
- **Priority tiers come from `busybar.display`** — reuse `PRIORITY_OVERLAY` (21); do **not** invent a new tier. `PRIORITY_ALERT` (60) becomes unused by `ci_status`.
- **Dwell/silence contract:** overlay-tier frames draw with element `timeout = OVERLAY_DWELL_SECONDS` (10) and commit `frame_index`/`last_dwell_end` **only after `client.draw` returns `DrawResult.DRAWN`** (the existing discipline — never commit on a failed draw).
- **LED off is explicit:** send `#00000000` on the failing→clear transition (never rely on omission to clear a lit LED), mirroring `calendar_countdown.logic`.
- **Run tests with:** `uv run pytest <path> -q` from the repo root. Full suite: `uv run pytest -q`.
- **Badge colors (unchanged):** failure bg `#A32D2DFF` / text `#FFFFFFFF`; stuck bg `#BA7517FF` / text `#0B0B0BFF`; green text `#00FF00FF`.
- **pyproject `version` stays `0.1.0`** — this repo's feature versions are doc labels (README prose), not the package version.

---

## File Structure

- `integrations/ci_status/logic.py` — **modify.** Add `FailingRun`; change `RepoState.failing`/`.stuck` to `list[FailingRun]`; enrich `evaluate_runs`; add `_fail_line`, fail/stuck/green frame builders, `build_overlay_sequence`, and the LED helper (`resolve_ci_led_value`, `CI_LED_COLOR`, `LED_OFF_COLOR`, `LED_OFF_ELEMENTS`). Later remove `build_ci_payload`, `overlay_frame_sequence`, `OVERLAY_FRAME_SHAPE`, `compute_alert_fingerprint`, `update_snooze`, and the `PRIORITY_ALERT` import.
- `integrations/ci_status/main.py` — **modify.** Rewrite `run_once`'s draw path to the sequence+LED model; run the rotation even with no active run; drop the snooze subsystem (`snooze_state` param, the `get_busy()` poll, `suppress_alert`/`suppress_led`).
- `tests/test_ci_logic.py` — **modify.** Update evaluate/state tests to `FailingRun`; add ref tests; add overlay fail/stuck/green frame + sequence tests + LED-helper tests; later delete `build_ci_payload`/snooze tests.
- `tests/test_ci_loop.py` — **modify.** Update failure/green/rotation tests to overlay-tier + LED behavior; add "failure rotates with no run" + LED-lifecycle tests; later delete snooze/alert-tier tests.
- `config.example.toml` — **modify.** Remove the `snooze_minutes` block.
- `README.md` (root) — **modify.** Ladder table + escalation/snooze prose.
- `integrations/ci_status/README.md` — **modify.** Failure-frame model; drop snooze section; priority-tier section.
- `src/busybar/display.py` — **modify.** Docstring rationale for `PRIORITY_ALERT` and `PRIORITY_AMBIENT_URGENT` (no behavior change).

---

## Task 1: Failure/stuck text carries the PR/branch ref

Delivers Request A while the failure is still a priority-60 alert (behavior otherwise unchanged). `evaluate_runs` stops discarding the run dict; the badge text gains the ref.

**Files:**
- Modify: `integrations/ci_status/logic.py` (`RepoState`, `evaluate_runs`, `build_ci_payload`; add `FailingRun`, `_fail_line`)
- Test: `tests/test_ci_logic.py`

**Interfaces:**
- Produces: `FailingRun` dataclass with fields `workflow: str`, `ref: str`. `RepoState.failing: list[FailingRun]`, `RepoState.stuck: list[FailingRun]`. `_fail_line(repo: str, fr: FailingRun) -> str` returning `"{repo} {ref} · {workflow}"` (ref omitted when empty).

- [ ] **Step 1: Write failing tests for `FailingRun` + enriched `evaluate_runs`**

In `tests/test_ci_logic.py`, first extend the `run()` fixture to optionally carry PR/branch, then rewrite the two evaluate tests. Replace the existing `run(...)` helper (lines 23-27) with:

```python
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
```

Add `FailingRun` to the import block (line 6-16) and update the two tests:

```python
def test_failure_detected_on_latest_run_only():
    runs = [run(1, "tests", "completed", "success"),
            run(1, "tests", "completed", "failure", 60),
            run(2, "lint", "completed", "failure", pr_number=42)]
    state = evaluate_runs("o/r", runs, NOW, 0)
    assert state.failing == [FailingRun("lint", "#42")] and state.stuck == []

def test_stuck_queued_detection_respects_threshold():
    runs = [run(1, "tests", "queued", None, created_min_ago=20, head_branch="main")]
    assert evaluate_runs("o/r", runs, NOW, 15).stuck == [FailingRun("tests", "main")]
    assert evaluate_runs("o/r", runs, NOW, 0).stuck == []
    assert evaluate_runs("o/r", runs, NOW, 30).stuck == []

def test_failing_run_ref_empty_when_no_pr_or_branch():
    state = evaluate_runs("o/r", [run(1, "tests", "completed", "failure")], NOW, 0)
    assert state.failing == [FailingRun("tests", "")]

def test_evaluate_sorts_failing_by_workflow_then_ref():
    runs = [run(2, "zeta", "completed", "failure", pr_number=9),
            run(1, "alpha", "completed", "failure", pr_number=3)]
    state = evaluate_runs("o/r", runs, NOW, 0)
    assert state.failing == [FailingRun("alpha", "#3"), FailingRun("zeta", "#9")]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ci_logic.py -q -k "evaluate or failing_run_ref"`
Expected: FAIL — `FailingRun` undefined / `evaluate_runs` returns strings.

- [ ] **Step 3: Add `FailingRun`, enrich `evaluate_runs`, add `_fail_line`**

In `integrations/ci_status/logic.py`, change the `RepoState` dataclass (lines 38-42) and add `FailingRun` above it:

```python
@dataclass(frozen=True)
class FailingRun:
    workflow: str   # r["name"]
    ref: str        # _pr_or_branch(r): "#42", the branch, or ""


@dataclass
class RepoState:
    repo: str
    failing: list[FailingRun]
    stuck: list[FailingRun]
```

Rewrite `evaluate_runs` (lines 132-145). Note `_pr_or_branch` is defined later in the file (line ~236) — module-level function, so calling it here is fine at runtime:

```python
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
```

Add `_fail_line` next to `_badge_elements` (near line 681):

```python
def _fail_line(repo: str, fr: FailingRun) -> str:
    """"owner/repo #42 · workflow" (the ref is dropped when empty)."""
    ref = f" {fr.ref}" if fr.ref else ""
    return f"{repo}{ref} · {fr.workflow}"
```

- [ ] **Step 4: Update `build_ci_payload` to render the ref (still priority 60)**

In `build_ci_payload` (lines 720-730), change the failure/stuck comprehensions to iterate `FailingRun` and use `_fail_line`:

```python
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
```

- [ ] **Step 5: Update the existing `build_ci_payload` tests to `FailingRun`**

In `tests/test_ci_logic.py`, update the state constructors and add a ref assertion:

```python
def test_payload_red_badge_on_failure():
    payload = build_ci_payload([RepoState("o/r", [FailingRun("tests", "#42")], [])], False, 180)
    assert payload["priority"] == PRIORITY_ALERT and payload["led"] == "#FF0000FF"
    bg = _bg_element(payload["elements"])
    assert bg["fill_colors"] == ["#A32D2DFF"] and bg["border_width"] == 0
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
```

`test_payload_none_when_green_and_quiet` and `test_payload_shows_green_glyph_when_enabled` use empty lists (`RepoState("o/r", [], [])`) and need no change.

- [ ] **Step 6: Run the ci_logic tests to verify they pass**

Run: `uv run pytest tests/test_ci_logic.py -q`
Expected: PASS.

- [ ] **Step 7: Run the loop tests to confirm the alert path still works with the ref**

The loop's `_run` fixture has no PR/branch, so failure text ref is empty — `test_draws_red_on_failure` still passes (priority 60, red LED).
Run: `uv run pytest tests/test_ci_loop.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add integrations/ci_status/logic.py tests/test_ci_logic.py
git commit -m "ci: carry PR/branch ref into failure and stuck badge text"
```

---

## Task 2: Overlay-tier fail/stuck/green frames + sequence builder

Additive logic only — nothing consumes these yet. They render the same badges at `PRIORITY_OVERLAY` (21) and assemble the dynamic rotation order.

**Files:**
- Modify: `integrations/ci_status/logic.py` (add frame-kind constants, extend `build_overlay_payload`, add `build_overlay_sequence`)
- Test: `tests/test_ci_logic.py`

**Interfaces:**
- Consumes: `FailingRun`, `_fail_line`, `_badge_elements`, `_text_element` (Task 1 / existing).
- Produces:
  - Constants `OVERLAY_FRAME_FAIL = "fail"`, `OVERLAY_FRAME_STUCK = "stuck"`, `OVERLAY_FRAME_GREEN = "green"` (alongside existing `OVERLAY_FRAME_CI_BADGE`/`_QUOTA_GQL`/`_QUOTA_REST`).
  - `build_overlay_payload(descriptor: dict, timeout_s: int, *, running=None, quota_by_bucket=None, show_spinner=False) -> dict | None` — now dispatches on `descriptor["kind"]`. A descriptor is `{"kind": ...}` plus, for fail/stuck, `"repo": str` and `"run": FailingRun`.
  - `build_overlay_sequence(states: list[RepoState], *, running_present: bool, quota_frames: list[str], show_green: bool) -> list[dict]` — ordered descriptors: all failures, then all stuck, then the CI badge (if `running_present`), then each quota frame in `quota_frames`, then a single green frame **only if the sequence is otherwise empty** and `show_green`.

- [ ] **Step 1: Write failing tests for the frame builders + sequence**

Add to `tests/test_ci_logic.py` (extend the import block with `OVERLAY_FRAME_FAIL, OVERLAY_FRAME_STUCK, OVERLAY_FRAME_GREEN, build_overlay_sequence`):

```python
def test_fail_frame_is_red_badge_at_overlay_tier():
    d = {"kind": OVERLAY_FRAME_FAIL, "repo": "o/r", "run": FailingRun("tests", "#42")}
    payload = build_overlay_payload(d, OVERLAY_DWELL_SECONDS)
    assert payload["priority"] == PRIORITY_OVERLAY
    assert _bg_element(payload["elements"])["fill_colors"] == ["#A32D2DFF"]
    t = _text_element(payload["elements"])
    assert t["text"] == "CI FAIL o/r #42 · tests" and t["color"] == "#FFFFFFFF"

def test_fail_frame_drops_ref_when_empty():
    d = {"kind": OVERLAY_FRAME_FAIL, "repo": "o/r", "run": FailingRun("tests", "")}
    assert _text_element(build_overlay_payload(d, 10)["elements"])["text"] == "CI FAIL o/r · tests"

def test_stuck_frame_is_amber_badge_at_overlay_tier():
    d = {"kind": OVERLAY_FRAME_STUCK, "repo": "o/r", "run": FailingRun("deploy", "#7")}
    payload = build_overlay_payload(d, 10)
    assert payload["priority"] == PRIORITY_OVERLAY
    assert _bg_element(payload["elements"])["fill_colors"] == ["#BA7517FF"]
    assert _text_element(payload["elements"])["text"] == "CI stuck o/r #7 · deploy"

def test_green_frame_is_quiet_text_at_overlay_tier():
    payload = build_overlay_payload({"kind": OVERLAY_FRAME_GREEN}, 10)
    assert payload["priority"] == PRIORITY_OVERLAY
    assert _text_element(payload["elements"])["color"] == "#00FF00FF"
    assert not any(e["type"] == "rectangle" for e in payload["elements"])

def test_sequence_orders_fail_then_stuck_then_badge_then_quota():
    states = [RepoState("o/r", [FailingRun("a", "#1")], [FailingRun("b", "#2")])]
    seq = build_overlay_sequence(states, running_present=True,
                                 quota_frames=[OVERLAY_FRAME_QUOTA_GQL], show_green=False)
    assert [d["kind"] for d in seq] == [
        OVERLAY_FRAME_FAIL, OVERLAY_FRAME_STUCK, OVERLAY_FRAME_CI_BADGE, OVERLAY_FRAME_QUOTA_GQL]
    assert seq[0]["run"] == FailingRun("a", "#1") and seq[0]["repo"] == "o/r"

def test_sequence_one_frame_per_failing_run():
    states = [RepoState("o/r", [FailingRun("a", ""), FailingRun("b", "")], [])]
    seq = build_overlay_sequence(states, running_present=False, quota_frames=[], show_green=False)
    assert [d["kind"] for d in seq] == [OVERLAY_FRAME_FAIL, OVERLAY_FRAME_FAIL]

def test_sequence_green_only_when_otherwise_empty():
    empty = [RepoState("o/r", [], [])]
    assert [d["kind"] for d in build_overlay_sequence(empty, running_present=False, quota_frames=[], show_green=True)] == [OVERLAY_FRAME_GREEN]
    # green suppressed when other content exists
    busy = [RepoState("o/r", [FailingRun("a", "")], [])]
    assert OVERLAY_FRAME_GREEN not in [d["kind"] for d in build_overlay_sequence(busy, running_present=False, quota_frames=[], show_green=True)]

def test_sequence_empty_when_nothing_and_green_off():
    assert build_overlay_sequence([RepoState("o/r", [], [])], running_present=False, quota_frames=[], show_green=False) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ci_logic.py -q -k "frame or sequence"`
Expected: FAIL — new constants / `build_overlay_sequence` undefined, `build_overlay_payload` doesn't accept a dict.

- [ ] **Step 3: Add constants and extend `build_overlay_payload`**

In `integrations/ci_status/logic.py`, add near the existing frame constants (line 606-608):

```python
OVERLAY_FRAME_FAIL = "fail"
OVERLAY_FRAME_STUCK = "stuck"
OVERLAY_FRAME_GREEN = "green"
```

Rewrite `build_overlay_payload` (lines 642-671) to dispatch on a descriptor dict. Keep the existing CI-badge / quota bodies; add fail/stuck/green. All frames set `"led": None` — the LED is decided centrally in `main.run_once` (Task 3/4):

```python
def build_overlay_payload(descriptor: dict, timeout_s: int, *,
                          running: "RunningInfo | None" = None,
                          quota_by_bucket: dict[str, "QuotaInfo"] | None = None,
                          show_spinner: bool = False) -> dict | None:
    """Build the {"elements", "priority", "led"} payload for one overlay-tier
    dwell slot from a frame descriptor ({"kind": ...} plus kind-specific
    fields). Returns None only if a frame's data isn't available this cycle;
    build_overlay_sequence never emits a descriptor whose data is missing, so
    in practice callers get a payload. `led` is always None here -- the
    failure-driven LED is resolved by the caller (see resolve_ci_led_value)."""
    kind = descriptor["kind"]
    if kind == OVERLAY_FRAME_CI_BADGE:
        if running is None:
            return None
        return {"elements": _build_running_elements(running, timeout_s, show_spinner=show_spinner),
                "priority": PRIORITY_OVERLAY, "led": None}
    if kind in (OVERLAY_FRAME_QUOTA_GQL, OVERLAY_FRAME_QUOTA_REST):
        bucket_key = "graphql" if kind == OVERLAY_FRAME_QUOTA_GQL else "core"
        info = (quota_by_bucket or {}).get(bucket_key)
        if info is None:
            return None
        return {"elements": _build_quota_elements(info, timeout_s),
                "priority": PRIORITY_OVERLAY, "led": None}
    if kind == OVERLAY_FRAME_FAIL:
        text = "CI FAIL " + _fail_line(descriptor["repo"], descriptor["run"])
        return {"elements": _badge_elements(text, "#A32D2DFF", "#FFFFFFFF", timeout_s),
                "priority": PRIORITY_OVERLAY, "led": None}
    if kind == OVERLAY_FRAME_STUCK:
        text = "CI stuck " + _fail_line(descriptor["repo"], descriptor["run"])
        return {"elements": _badge_elements(text, "#BA7517FF", "#0B0B0BFF", timeout_s),
                "priority": PRIORITY_OVERLAY, "led": None}
    if kind == OVERLAY_FRAME_GREEN:
        return {"elements": [_text_element("CI ok", "#00FF00FF", timeout_s)],
                "priority": PRIORITY_OVERLAY, "led": None}
    return None
```

- [ ] **Step 4: Add `build_overlay_sequence`**

Add below `build_overlay_payload`:

```python
def build_overlay_sequence(states: list[RepoState], *, running_present: bool,
                           quota_frames: list[str], show_green: bool) -> list[dict]:
    """The ordered overlay-tier rotation for this poll: one frame per failing
    run, then one per stuck run, then the running CI badge (if a run is
    active), then each available quota frame, then a single quiet-green frame
    ONLY when nothing else is present and show_green is on. Each fail/stuck
    descriptor carries its repo and FailingRun so the renderer needs no extra
    lookup."""
    seq: list[dict] = []
    for s in states:
        for fr in s.failing:
            seq.append({"kind": OVERLAY_FRAME_FAIL, "repo": s.repo, "run": fr})
    for s in states:
        for fr in s.stuck:
            seq.append({"kind": OVERLAY_FRAME_STUCK, "repo": s.repo, "run": fr})
    if running_present:
        seq.append({"kind": OVERLAY_FRAME_CI_BADGE})
    for frame in quota_frames:
        seq.append({"kind": frame})
    if not seq and show_green:
        seq.append({"kind": OVERLAY_FRAME_GREEN})
    return seq
```

- [ ] **Step 5: Migrate existing direct `build_overlay_payload` call sites to the descriptor form**

`tests/test_ci_logic.py` already has ~16 direct calls that pass a **string frame name** as the first positional arg (the running-badge and quota tests). The new signature takes a **descriptor dict**, so wrap each frame name in `{"kind": ...}`. Apply this transform to every direct call site (running-badge tests around lines 278, 317, 325, 330, 462, 468, 599, 609, 840, 849; quota tests around lines 338, 358, 365, 367, 371, 475):

```
build_overlay_payload(OVERLAY_FRAME_CI_BADGE,  →  build_overlay_payload({"kind": OVERLAY_FRAME_CI_BADGE},
build_overlay_payload(OVERLAY_FRAME_QUOTA_GQL, →  build_overlay_payload({"kind": OVERLAY_FRAME_QUOTA_GQL},
build_overlay_payload(OVERLAY_FRAME_QUOTA_REST,→  build_overlay_payload({"kind": OVERLAY_FRAME_QUOTA_REST},
```

Semantics are unchanged (e.g. the `running=None → None` case at line 330 still returns `None`). Do **not** touch `overlay_frame_sequence`/`OVERLAY_FRAME_SHAPE` here — those are removed in Task 5.

- [ ] **Step 6: Run to verify pass**

Run: `uv run pytest tests/test_ci_logic.py -q`
Expected: PASS (new frame/sequence tests plus every migrated running-badge/quota call site).

- [ ] **Step 7: Commit**

```bash
git add integrations/ci_status/logic.py tests/test_ci_logic.py
git commit -m "ci: add overlay-tier fail/stuck/green frames and dynamic sequence builder"
```

---

## Task 3: Failure-driven LED lifecycle helper

Mirror `calendar_countdown`'s LED discipline so the red LED stays lit while any failure exists and turns off (explicitly) when it clears — decoupled from which frame is on screen.

**Files:**
- Modify: `integrations/ci_status/logic.py` (add LED constants + `resolve_ci_led_value`)
- Test: `tests/test_ci_logic.py`

**Interfaces:**
- Produces: `CI_LED_COLOR = "#FF0000FF"`, `LED_OFF_COLOR = "#00000000"`, `LED_OFF_ELEMENTS: list[dict]` (a single 1×1 transparent, 5 s self-expiring element), and `resolve_ci_led_value(led_should_be_on: bool, led_was_on: bool) -> str | None`.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_ci_logic.py` (import `resolve_ci_led_value, CI_LED_COLOR, LED_OFF_COLOR, LED_OFF_ELEMENTS`):

```python
def test_led_value_red_while_failing():
    assert resolve_ci_led_value(True, False) == CI_LED_COLOR
    assert resolve_ci_led_value(True, True) == CI_LED_COLOR

def test_led_value_explicit_off_on_transition():
    assert resolve_ci_led_value(False, True) == LED_OFF_COLOR

def test_led_value_omitted_once_already_off():
    assert resolve_ci_led_value(False, False) is None

def test_led_off_elements_is_single_expiring_placeholder():
    assert len(LED_OFF_ELEMENTS) == 1
    el = LED_OFF_ELEMENTS[0]
    assert el["type"] == "rectangle" and el["width"] == 1 and el["height"] == 1
    assert el["fill_colors"] == ["#00000000"] and el["timeout"] == 5
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ci_logic.py -q -k led`
Expected: FAIL — names undefined.

- [ ] **Step 3: Implement the LED helper**

Add to `integrations/ci_status/logic.py` (near the top of the overlay section, after the frame constants):

```python
CI_LED_COLOR = "#FF0000FF"
LED_OFF_COLOR = "#00000000"
# ^ Explicit LED-off (zero alpha). Whether omitting led_notification_color
# turns a lit LED off is not observable through this device's API, so the
# on->off transition sends this value explicitly -- same hypothesis-agnostic
# choice calendar_countdown makes (see its resolve_led_value / LED_OFF_COLOR).
LED_OFF_ELEMENTS = [{
    "id": "ci_led_off_flush", "type": "rectangle", "x": 0, "y": 0,
    "width": 1, "height": 1, "fill": "solid", "fill_colors": ["#00000000"],
    "border_width": 0, "timeout": 5,
}]
# ^ Minimal 1x1 transparent self-expiring element -- the draw endpoint requires
# >=1 element, so a bare led_notification_color with no element is impossible.
# Used on the "nothing else to draw but the LED must go off" path (main.run_once).


def resolve_ci_led_value(led_should_be_on: bool, led_was_on: bool) -> str | None:
    """The led_notification_color to send THIS poll: CI_LED_COLOR while any
    failure exists; LED_OFF_COLOR (explicit) on the exact failing->clear poll;
    None (omit) once already off. main.run_once tracks `led_was_on` in its
    caller-owned overlay_state, committed only after a confirmed DRAWN send."""
    if led_should_be_on:
        return CI_LED_COLOR
    if led_was_on:
        return LED_OFF_COLOR
    return None
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_ci_logic.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add integrations/ci_status/logic.py tests/test_ci_logic.py
git commit -m "ci: add failure-driven LED lifecycle helper (mirrors calendar)"
```

---

## Task 4: Rewire `run_once` — calm rotation + gentle LED, retire snooze

The core change (Request B). `run_once` now drives the dwell/silence rotation from `build_overlay_sequence` (so failures rotate even with no running job), centralizes the LED via `resolve_ci_led_value`, and drops the snooze subsystem.

**Files:**
- Modify: `integrations/ci_status/main.py` (`run_once`, `main`; imports)
- Test: `tests/test_ci_loop.py`

**Interfaces:**
- Consumes: `evaluate_runs`, `select_running_run`, `build_overlay_sequence`, `build_overlay_payload`, `resolve_ci_led_value`, `LED_OFF_COLOR`, `LED_OFF_ELEMENTS`, `OVERLAY_FRAME_QUOTA_GQL`, `OVERLAY_FRAME_QUOTA_REST` (logic); `PRIORITY_OVERLAY`, `OVERLAY_DWELL_SECONDS`, `overlay_gap_elapsed` (display).
- Produces: `run_once(client, poller, cfg, now, state_cache, dry_run, running_cache=None, overlay_state=None, quota_cache=None, repo_cache=None) -> str` — **`snooze_state` parameter removed.** `overlay_state` gains a `"led_was_on": bool` key.

- [ ] **Step 1: Write failing loop tests for the new behavior**

In `tests/test_ci_loop.py`, update `_run` is unchanged. Change `test_draws_red_on_failure` to expect the overlay tier, and add new tests. (Import `FailingRun` and `RepoState` are already imported for `RepoState`; add `FailingRun`.)

```python
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
```

Add a tiny helper near the top of the test module (after `_run`):

```python
def _drawn_text(client) -> str:
    els = client.draw.call_args.kwargs.get("elements") or client.draw.call_args.args[1]
    return next(e["text"] for e in els if e.get("type") == "text")
```

Note `client.draw` is called positionally as `client.draw(APP, payload["elements"], priority=..., led_notification_color=...)`, so `elements` is `call_args.args[1]`; `_drawn_text` handles both.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ci_loop.py -q -k "failure or led or green_folds"`
Expected: FAIL — failure still draws at 60 / `snooze_state`-free path not yet implemented / `led_was_on` absent.

- [ ] **Step 3: Rewrite `run_once`'s draw path**

In `integrations/ci_status/main.py`, update imports (lines 16-23): drop `compute_alert_fingerprint`, `update_snooze`, `build_ci_payload`, `overlay_frame_sequence` from the logic import; add `build_overlay_sequence`, `resolve_ci_led_value`, `LED_OFF_COLOR`, `LED_OFF_ELEMENTS`, `OVERLAY_FRAME_QUOTA_GQL`, `OVERLAY_FRAME_QUOTA_REST`. Keep `RepoState, RunningInfo, QuotaInfo, build_overlay_payload, evaluate_runs, parse_rate_limit, resolve_repo_list, select_running_run`.

Change the `run_once` signature (line 86-92) to drop `snooze_state`:

```python
def run_once(client, poller, cfg: dict, now: datetime,
             state_cache: dict[str, RepoState], dry_run: bool,
             running_cache: dict[str, list[dict]] | None = None,
             overlay_state: dict | None = None,
             quota_cache: dict | None = None,
             repo_cache: dict | None = None) -> str:
```

Keep the repo-resolution + pruning + `evaluate_runs` block (lines 162-191) unchanged. Then **replace everything from line 192 (`has_alert = ...`) through the end of the function** with:

```python
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
```

Update `run_once`'s docstring: remove the "Alert snooze" paragraph; note that the rotation now includes failure/stuck/green frames and runs even without an active run, and that `overlay_state["led_was_on"]` tracks the failure LED.

- [ ] **Step 4: Drop snooze from `main()`**

In `main()` (lines 373-384) remove the `snooze_state: dict = {}` line and the `snooze_state=snooze_state` kwarg from the `run_once` call.

- [ ] **Step 5: Update the loop tests that assumed the alert tier / snooze**

Update these existing tests in `tests/test_ci_loop.py` (do **not** delete the snooze block yet — that's Task 5):
- `test_304_keeps_previous_state`, `test_dry_run_touches_nothing`: unchanged assertions still hold (draw once / DRY-RUN, no priority assumption). Leave as-is.
- `test_overlay_then_alert_clears_stale_overlay_shape` and `test_alert_then_overlay_clears_stale_alert_shape`: these drive a failure via `state_cache` seeded with a failing run and assert a `clear()` precedes the shape change. They still exercise a real fail↔running seam; keep them but pass `overlay_state` and expect the failure frame at `PRIORITY_OVERLAY`. If they assert priority 60 anywhere, change to `PRIORITY_OVERLAY`.
- `test_quiet_green_then_overlay_clears_stale_green_shape`: green now draws at the overlay tier; the shape-clear assertion is unchanged. Verify it still passes; adjust any priority assertion to `PRIORITY_OVERLAY`.

Run the seam tests: `uv run pytest tests/test_ci_loop.py -q -k "clears_stale or shape"` and fix any priority/led assertions to the overlay-tier values.

- [ ] **Step 6: Run to verify pass (new + adapted tests; snooze tests still present)**

Run: `uv run pytest tests/test_ci_loop.py -q -k "not snooze"`
Expected: PASS. (Snooze tests still import a removed `snooze_state` path and will error — they are deleted in Task 5. Run with `-k "not snooze"` here.)

- [ ] **Step 7: Commit**

```bash
git add integrations/ci_status/main.py tests/test_ci_loop.py
git commit -m "ci: rotate failure/stuck/green at overlay tier with gentle LED; drop snooze from run_once"
```

---

## Task 5: Delete dead code + obsolete tests

Remove the now-unreachable alert-tier and snooze code and their tests, so the module has one model.

**Files:**
- Modify: `integrations/ci_status/logic.py` (remove `build_ci_payload`, `overlay_frame_sequence`, `OVERLAY_FRAME_SHAPE`, `compute_alert_fingerprint`, `update_snooze`, the `PRIORITY_ALERT` import, and the snooze narrative comment block)
- Modify: `tests/test_ci_logic.py`, `tests/test_ci_loop.py` (delete obsolete tests + imports)

**Interfaces:**
- Consumes: nothing new.
- Produces: `ci_status.logic` no longer exports `build_ci_payload`, `overlay_frame_sequence`, `OVERLAY_FRAME_SHAPE`, `compute_alert_fingerprint`, `update_snooze`.

- [ ] **Step 1: Delete obsolete tests first (so the suite defines the target surface)**

In `tests/test_ci_logic.py`:
- Remove `build_ci_payload`, `compute_alert_fingerprint`, `update_snooze`, `overlay_frame_sequence`, `OVERLAY_FRAME_SHAPE`, `PRIORITY_ALERT` from the imports (lines 6-17).
- Delete the `build_ci_payload` tests: `test_payload_none_when_green_and_quiet`, `test_payload_shows_green_glyph_when_enabled`, `test_payload_red_badge_on_failure`, `test_payload_amber_badge_on_stuck_only`, `test_failure_badge_takes_priority_over_stuck`, and the entire `# --- build_ci_payload: overlay precedence ---` section (starting ~line 459). Their coverage now lives in Task 2's `build_overlay_payload`/`build_overlay_sequence` tests.
- Delete the `overlay_frame_sequence`/`OVERLAY_FRAME_SHAPE` tests (the `# --- overlay_frame_sequence ---` section ~lines 441-456): `test_overlay_frame_sequence_badge_only_when_quota_disabled`, `test_overlay_frame_sequence_includes_quota_frames_when_enabled`, `test_overlay_frame_shape_distinguishes_badge_from_quota`. Their replacement is Task 2's `build_overlay_sequence` tests.
- Delete the entire snooze section (every `update_snooze`/`compute_alert_fingerprint` test).

In `tests/test_ci_loop.py`:
- Delete the snooze section (from the `--- alert snooze ...` comment / `CFG_SNOOZE` through the last `test_snooze_*`), including the `_busy` helper and `CFG_SNOOZE`.
- Delete `test_alert_rejected_during_calendar_elevation_does_not_commit_then_recovers` (alert-tier-specific; the overlay-tier equivalent `test_overlay_dwell_rejected_during_calendar_elevation_resumes_after` remains and now covers failures too).

- [ ] **Step 2: Run to verify the deletions fail against still-present code**

Run: `uv run pytest tests/test_ci_logic.py tests/test_ci_loop.py -q`
Expected: PASS (nothing references the doomed symbols anymore). If any collection error, it's a missed import — fix it.

- [ ] **Step 3: Remove the dead logic**

In `integrations/ci_status/logic.py`:
- Change the import (line 33) from `from busybar.display import PRIORITY_OVERLAY, PRIORITY_ALERT` to `from busybar.display import PRIORITY_OVERLAY`.
- Delete `build_ci_payload` (the whole function, ~lines 691-736) and its precedence docstring.
- Delete `overlay_frame_sequence` (lines 632-639) and the `OVERLAY_FRAME_SHAPE` dict + its reference comment (lines 610-629).
- Delete `compute_alert_fingerprint` and `update_snooze` and the `--- alert snooze ...` narrative comment block (from ~line 739 to the end of the snooze section).

- [ ] **Step 4: Run the full ci suite + a grep guard**

Run: `uv run pytest tests/test_ci_logic.py tests/test_ci_loop.py -q`
Expected: PASS.
Run: `grep -rnE "build_ci_payload|update_snooze|compute_alert_fingerprint|overlay_frame_sequence|OVERLAY_FRAME_SHAPE|PRIORITY_ALERT" integrations/ci_status tests`
Expected: no matches (empty output).

- [ ] **Step 5: Commit**

```bash
git add integrations/ci_status/logic.py tests/test_ci_logic.py tests/test_ci_loop.py
git commit -m "ci: remove dead alert-tier payload and snooze subsystem"
```

---

## Task 6: Config + docs + version label

Remove the snooze config and refresh the docs to the calm-rotation model.

**Files:**
- Modify: `config.example.toml`, `README.md`, `integrations/ci_status/README.md`, `src/busybar/display.py`

**Interfaces:** none (docs/config only).

- [ ] **Step 1: Remove `snooze_minutes` from `config.example.toml`**

Delete the snooze comment + key (lines 74-79: the `# Alert snooze ...` block and `snooze_minutes = 30`).

- [ ] **Step 2: Update the root `README.md`**

- Ladder table (line 56): change the `60 | PRIORITY_ALERT` row to note it is **reserved/unused** now, and update the `21 | PRIORITY_OVERLAY` row to read: `ci_status`'s rotation — running badge, GraphQL/REST quota gauges, **and failure/stuck/quiet-green frames**.
- Replace the **"Escalation beats alerts."** paragraph (line 64): `ci_status` no longer draws at `PRIORITY_ALERT`; failure/stuck now rotate at `PRIORITY_OVERLAY` (21) under the calendar's ambient tiers, so an imminent event naturally outranks them and the failure alternates with the calendar rather than camping the panel.
- Delete the **"Snooze by acknowledgment."** paragraph (line 66).

- [ ] **Step 3: Update `integrations/ci_status/README.md`**

- Rewrite the "What It Does" failure/stuck sentence (line 5): a failure/stuck now shows as a **calm rotation frame at the overlay tier** reading `CI FAIL owner/repo #42 · workflow` (PR number, or the branch when there's no PR), alternating with the calendar, running badge, and quota — not a priority-60 takeover. A gentle red LED stays lit while anything is failing.
- Remove the `snooze_minutes` row from the Config Reference table (line 91).
- Delete the entire **"## Snoozing alerts"** section (lines 103-113).
- Update the **"## Display Priority Tiers"** section (lines 115-143): failure/stuck/green draw at `PRIORITY_OVERLAY` (21) in the rotation; there is no longer an alert-tier preemption or a `build_ci_payload` precedence chain. Update the account-wide privacy note (line 97) `repo:workflow` phrasing to `repo #PR · workflow`.

- [ ] **Step 4: Update `src/busybar/display.py` docstrings (no behavior change)**

- `PRIORITY_ALERT` docstring (line 119): note no in-repo integration currently draws here — it remains defined as the ladder's alert slot for reference (ci_status moved its failure/stuck frames down to `PRIORITY_OVERLAY`).
- `PRIORITY_AMBIENT_URGENT` docstring (line 127) and `PRIORITY_AMBIENT_RAISED` (line 102): soften the "a persistent CI failure alert was permanently evicting the calendar" rationale to past tense / historical, since ci_status no longer camps at 60. Keep the tiers and numbers exactly as they are.

- [ ] **Step 5: Version label**

The spec labels this **v1.7**. Update the ci_status README's status/feature references that carry a version label to v1.7 where such labels appear (do **not** change `pyproject.toml` `version`). Update the spec doc's Status line if desired.

- [ ] **Step 6: Commit**

```bash
git add config.example.toml README.md integrations/ci_status/README.md src/busybar/display.py docs/superpowers/specs/2026-08-08-ci-failure-rotation-frame-design.md
git commit -m "docs+config: calm-rotation CI failure model; drop snooze; ladder/tier updates"
```

---

## Task 7: Full-suite verification

**Files:** none (verification).

- [ ] **Step 1: Run the entire test suite**

Run: `uv run pytest -q`
Expected: PASS (all modules — calendar, nyan, client, config, display, ci).

- [ ] **Step 2: Lint / import sanity**

Run: `uv run python -c "import sys; sys.path.insert(0,'integrations'); import ci_status.logic, ci_status.main; print('import ok')"`
Expected: `import ok` (no ImportError from removed symbols).

- [ ] **Step 3: Dry-run the integration end-to-end**

Run: `cd integrations && uv run python -m ci_status.main --once --dry-run`
Expected: a `DRY-RUN ...` summary line, no traceback. (Real device/GitHub not required for `--dry-run`.)

- [ ] **Step 4: Record the on-device verification checklist (not automatable here)**

The following require the physical device (fw 1.1.1) and are called out in the spec §9 — note them in the PR description for the operator to confirm: (a) a failure frame alternates with the calendar and running badge with visible dwell gaps; (b) the red LED stays lit across a non-failure frame while a failure coexists, and turns off within one dwell after the failure clears; (c) an imminent calendar event still outranks the failure frame.

- [ ] **Step 5: Commit any final fixes (if Steps 1-3 surfaced issues)**

```bash
git add -A
git commit -m "ci: fixups from full-suite verification"
```

---

## Self-Review

**Spec coverage:**
- Request A (repo + PR): Task 1 (`FailingRun` + `evaluate_runs` + `_fail_line`), rendered by Task 2 frames. ✓
- Request B (calm rotation, no takeover): Task 2 (overlay frames + sequence) + Task 4 (`run_once` rewrite, rotation runs with no active run). ✓
- Gentle LED: Task 3 (helper) + Task 4 (central LED, off-transition + `LED_OFF_ELEMENTS`). ✓
- Move both failure + stuck: sequence includes both (Task 2); LED is failure-only per spec §4c. ✓
- One frame per failing run: `build_overlay_sequence` emits one descriptor per `FailingRun` (Task 2, `test_sequence_one_frame_per_failing_run`). ✓
- Green folded into rotation: Task 2 green frame + Task 4 (`test_green_folds_into_rotation_at_overlay_tier`). ✓
- Retire snooze: Task 4 (main/run_once) + Task 5 (logic + tests). ✓
- Config/docs/ripple: Task 6. ✓
- Testing (spec §7): ref rendering (T1), no-active-run rotation (T4), dwell/silence (T4), green fold (T4), LED lifecycle (T3+T4), shape-clear seams (T4 adapted), snooze removed (T5). ✓

**Placeholder scan:** No TBD/TODO; every code step carries real code. The on-device checks (Task 7 Step 4) are explicitly non-automatable and documented, not a placeholder. ✓

**Type consistency:** `FailingRun(workflow, ref)` used identically in T1/T2/T4. `build_overlay_payload(descriptor: dict, ...)` (T2) matches its `run_once` call site (T4). `build_overlay_sequence(states, *, running_present, quota_frames, show_green)` signature matches T4's call. `resolve_ci_led_value(led_should_be_on, led_was_on)` and `overlay_state["led_was_on"]` consistent across T3/T4. `run_once` loses `snooze_state` in T4 and no later task references it. ✓
