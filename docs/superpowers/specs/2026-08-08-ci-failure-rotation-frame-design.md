# CI Failure as a Calm Rotation Frame — Design Spec

**Date:** 2026-08-08
**Status:** approved design; not yet on-device verified (see §9)
**Depends on:** `busybar.display` priority ladder, `src/busybar/client.py`, the existing `ci_status` overlay-rotation loop and `calendar_countdown`'s LED lifecycle (as a pattern to mirror)

---

## 1. Goal

Make the CI **failure** (and stuck-queue) screen do two things it doesn't today:

1. **Say what actually failed.** Show the **repo and PR number** (falling back to
   the branch when a run has no PR), not just the workflow name — so a glance
   tells you where to look.
2. **Stop hijacking the panel.** Instead of a persistent priority-60 takeover
   that evicts the calendar, a failure becomes one **calm frame in the existing
   overlay rotation**, alternating with the upcoming calendar event, the running
   badge, and the quota gauges.

A **gentle red LED stays lit** while anything is failing, so the loss of the
screen takeover doesn't mean the failure goes unnoticed.

## 2. Scope & non-goals

**In scope**
- `integrations/ci_status/logic.py`: enrich failure/stuck rendering with the
  PR/branch ref; demote failure/stuck/green from `PRIORITY_ALERT` (60) to the
  overlay tier (21); generalize the overlay-frame sequence to include failure,
  stuck, and green frames; add the LED-lifecycle helper.
- `integrations/ci_status/main.py`: restructure `run_once` so the overlay
  rotation runs whenever there is *any* content (a failure, stuck, running run,
  quota, or green), not only while a job is running; retire the snooze
  subsystem and its per-poll `get_busy()` call.
- Config: drop `snooze_minutes`.
- Docs: root `README.md` ladder table, `integrations/ci_status/README.md`, and
  the now-stale rationale comment on `PRIORITY_AMBIENT_URGENT` in
  `src/busybar/display.py`.
- Tests: rework the ci_status logic/main tests to the new frame model; delete
  the snooze tests; add ref-rendering, no-active-run rotation, dwell/silence,
  and LED on→off coverage.

**Non-goals**
- **No calendar behavior change.** `calendar_countdown`'s tiers, LED, and chirp
  are untouched. Its `PRIORITY_AMBIENT_URGENT` (65) still outranks everything
  ci_status draws; only the *reason* that tier was originally introduced (a CI
  failure camping the calendar) goes away — a doc-comment update, not a code
  change.
- **No new priority tier.** Failure/stuck reuse `PRIORITY_OVERLAY` (21).
  `PRIORITY_ALERT` (60) simply becomes unused by ci_status; it stays defined as
  ladder documentation.
- **No polling-cadence change.** `next_poll_seconds` still switches on *running*
  runs only; a failure does not shorten the poll interval.
- **No new dependencies.**

## 3. Background: how a failure behaves today

`evaluate_runs` ([`logic.py:132`](../../../integrations/ci_status/logic.py)) reduces
each failing run to its workflow **name** (`r["name"]`) and throws the rest of
the run dict away — including the `pull_requests` array and `head_branch` that
`_pr_or_branch` ([`logic.py:236`](../../../integrations/ci_status/logic.py)) already
knows how to turn into `#42`-or-branch for the running badge.

`build_ci_payload` ([`logic.py:691`](../../../integrations/ci_status/logic.py)) then
renders a full-panel badge at **`PRIORITY_ALERT` (60)** with a red LED:

```
CI FAIL owner/repo:workflow   (red bg #A32D2DFF, white text, LED #FF0000FF)
CI stuck owner/repo:workflow  (amber bg #BA7517FF, dark text, LED None)
CI ok                         (green text, priority 60, LED None)
```

Priority 60 is a no-dwell takeover: it redraws every poll and **evicts** the
ambient calendar (20). An entire extra tier — `PRIORITY_AMBIENT_URGENT` (65) —
exists only so an *imminent* calendar event can claw the screen back from it
(see the docstring in [`display.py:127`](../../../src/busybar/display.py)).

The **overlay rotation** — running badge → quota(gql) → quota(rest) at tier 21,
each drawn for a 10 s dwell then **silent for one dwell** so the calendar can
reclaim the gap — lives in `run_once`
([`main.py:236`](../../../integrations/ci_status/main.py)). It runs only while a job
is actively running, and an alert explicitly resets/preempts it.

## 4. Design

### 4a. Failure/stuck content — the ref (Request A)

Introduce a small record and carry it out of `evaluate_runs`:

```python
@dataclass
class FailingRun:
    workflow: str   # r["name"]
    ref: str        # _pr_or_branch(r): "#42", else the branch, else ""
```

`RepoState.failing` and `RepoState.stuck` change from `list[str]` to
`list[FailingRun]`, sorted deterministically by `(workflow, ref)`. `evaluate_runs`
builds a `FailingRun` per latest-per-workflow run in the `FAILING` set (and per
stale-queued run for stuck), calling the existing `_pr_or_branch` on the run dict
it already has in hand.

Frame text (badge colors unchanged — red for fail, amber for stuck):

```
CI FAIL owner/repo #42 · ci.yml         # PR present
CI FAIL owner/repo main · nightly.yml   # no PR → branch
CI stuck owner/repo #7 · deploy.yml
```

The `·` separator and `CI FAIL`/`CI stuck` prefixes are the proposed wording;
trivially tunable. The badge still scrolls, so length is not a hard constraint.

### 4b. Demotion into the rotation (Request B)

**Priority.** Failure, stuck, and green all draw at `PRIORITY_OVERLAY` (21) with a
`OVERLAY_DWELL_SECONDS` timeout — the same dwell/silence contract the running
badge and quota frames already follow. Nothing ci_status draws sits above the
ambient calendar's own raised/urgent tiers anymore, so the calendar reclaims the
gap between every ci_status frame, and an imminent event still wins outright.

**One frame per failing run.** Each `(repo, FailingRun)` is its own rotation slot
(operator's choice), so a burst of N failures produces N slots cycling one per
dwell. Same for stuck.

**Dynamic sequence.** Generalize the frame sequence from a list of frame-name
strings to a list of **frame descriptors**, built fresh each poll from whatever
exists this cycle, in this order:

```
[ fail(repo, run) for each failing run ]      # most important → leads
+ [ stuck(repo, run) for each stuck run ]
+ [ ci_badge ]           if show_running and a run is active
+ [ quota(graphql), quota(core) ]  if show_quota and fresh data
+ [ green ]              if show_green and the sequence is otherwise empty
```

`build_overlay_payload` dispatches on the descriptor kind (it already builds
`ci_badge` and both `quota` kinds; add `fail`, `stuck`, `green`, reusing
`_badge_elements`/`_text_element`). Green only appears when nothing else does, so
it never competes for a slot — it just becomes a dwelling frame instead of a
priority-60 camp, letting the calendar show through even in the all-clear state.

**Rotation mechanics are unchanged.** The existing `frame_index` (mod sequence
length), `overlay_gap_elapsed` dwell gate, DRAWN-gated commit of
`frame_index`/`last_dwell_end`, and the **unified `last_shape` clear-gate** all
carry over verbatim — the clear-gate already spans "every tier that can draw to
APP," so the new fail/stuck/green shapes slot in without special-casing. Because
the set of failing runs can change between polls, the index→frame mapping is not
a stable identity across polls; that is acceptable for a rotation (it already is
for quota frames appearing/disappearing) and is called out in §9.

**`build_ci_payload` dissolves.** Its failure>stuck>overlay>green precedence is
replaced by "draw the descriptor the rotation picked this poll." The function is
removed (or reduced to the LED-attaching wrapper in §4c).

### 4c. The gentle LED (operator's choice)

The LED must be red whenever *anything is failing*, independent of which frame is
currently on screen (a running-badge frame while a failure also exists must still
show red). Mirror `calendar_countdown`'s proven lifecycle
([`logic.py:319`](../../../integrations/calendar_countdown/logic.py)):

- Compute `led_should_be_on = any(state.failing for state in states)` once per
  poll (failure only — stuck keeps today's LED-`None` behavior).
- `resolve_led_value(led_should_be_on, led_was_on)` returns the red color while
  on, an **explicit `#00000000`** on the on→off transition poll (the
  hypothesis-agnostic off, since "omit = off" is unverifiable on this device),
  and `None` (omit) once already off.
- Attach the resolved value to whatever payload is drawn this poll.
- Track `led_was_on` in the caller-owned state dict, committing **only after a
  confirmed successful draw** — same DRAWN-gated discipline as everywhere else.
- On a poll where failures just cleared but there is **nothing else to draw**
  (rotation empty, green off), emit the calendar's `LED_OFF_ELEMENTS` 1×1
  transparent placeholder to carry the explicit off, since the draw endpoint
  requires ≥1 element.
- During a dwell **silence gap**, ci_status draws nothing; the LED stays sticky
  from its last assertion, which is the desired "still red while failing."

This also closes a latent gap: today nothing reliably turns the CI red LED *off*
when a failure resolves — the calendar solved this and ci_status never did.

### 4d. Retiring snooze (operator's choice)

Delete `compute_alert_fingerprint`, `update_snooze`, the `snooze_state` plumbing
through `run_once`/`main`, the `suppress_alert`/`suppress_led` parameters, and the
per-poll `client.get_busy()` call ci_status made *only* to drive snooze. A calm
rotating frame needs no acknowledge-to-quiet mechanism. This removes a large,
subtle state machine and its tests.

## 5. Config changes

- Remove `snooze_minutes` from `config.example.toml` and its README entry.
  Loading is already `.get`-based, so an old `config.toml` that still sets it is
  harmless — the key simply goes unread.
- No new keys. `show_green`, `show_running`, `show_quota`, `running_spinner`,
  `poll_seconds`, `running_poll_seconds`, and the account-wide keys are unchanged.

## 6. Ripple / docs

- **Root `README.md`** priority-ladder table: the `PRIORITY_ALERT` (60) row no
  longer belongs to ci_status; note it as unused / reserved, and note that
  ci_status failure/stuck/green now share `PRIORITY_OVERLAY` (21).
- **`display.py`**: update the `PRIORITY_AMBIENT_URGENT` docstring rationale (it
  cites a persistent CI failure evicting the calendar — which no longer happens)
  and the `PRIORITY_ALERT` docstring (no current in-repo drawer). Behavior of
  both constants is unchanged.
- **`integrations/ci_status/README.md`**: replace the "failure/stuck alert
  badge" description with the calm-rotation-frame model; drop the snooze section.

## 7. Testing

- **Ref rendering:** PR present → `#N`; no PR → `head_branch`; neither → empty.
- **Rotation with no active run:** a failure alone produces a drawing rotation
  (today this path drew nothing but a priority-60 badge).
- **One frame per failing run:** N failing runs → N descriptors/slots.
- **Dwell/silence:** a failure frame draws for a dwell, then stays silent one
  dwell (calendar-reclaim gap), same as the running badge.
- **Green folded in:** `show_green` with nothing else failing/running draws
  `CI ok` at tier 21 and dwells (no longer a priority-60 camp).
- **LED lifecycle:** red while failing (including during a non-failure frame when
  a failure coexists); explicit `#00000000` on the failing→clear transition, via
  both the piggybacked path and the standalone `LED_OFF_ELEMENTS` path; omitted
  once already off.
- **Shape clear-gate:** transitions across the new seams (fail→ci_badge,
  fail→green, fail→quota, and back) still clear the prior shape before drawing.
- **Snooze removed:** delete the snooze tests; assert `run_once` never calls
  `get_busy()`.

## 8. Rollout

- Version bump per repo convention (recent tag is v1.6; this is a user-visible
  ci_status behavior change — suggest **v1.7**, operator to confirm).
- Single branch/PR; no migration. An operator on an old config needs no action
  (`snooze_minutes` just stops mattering).

## 9. Open questions / risks

- **Rotation length under many failures.** Per-run frames mean a failure burst
  lengthens the cycle (each failure waits longer to reappear). Chosen knowingly;
  revisit only if it bites in practice (a possible future cap: collapse to one
  combined frame past K failures).
- **Dynamic sequence identity.** The failing set changing between polls reshuffles
  which `frame_index` maps to which frame. Acceptable for a rotation and already
  true for quota frames; no stable-identity guarantee is intended.
- **LED device semantics unverified.** Whether omitting `led_notification_color`
  turns a lit LED off is not observable through this API; the explicit-`#00000000`
  approach is correct either way (same caveat `calendar_countdown` documents).
- **On-device verification pending.** The dwell/silence timing, the LED staying
  red across non-failure frames and turning off on clear, and the calendar
  reclaiming gaps around a failure frame should be confirmed on the physical
  device (fw 1.1.1), matching how prior specs recorded live spike results.
