import copy
import inspect
import logging
import os
import tomllib
from pathlib import Path

from busybar.client import BusyBarClient

log = logging.getLogger(__name__)

DEFAULTS: dict = {
    "device": {
        "host": "10.0.4.20",
        # Cloud transport fallback (v1.6) -- see README's "Cloud
        # transport" section before setting these. An empty cloud_token
        # (the shipped default) disables cloud fallback entirely in
        # "auto" mode; keys here MUST match BusyBarClient's constructor
        # kwarg names exactly since call sites pass **cfg["device"].
        "cloud_token": "",
        "cloud_base_url": "https://api.busy.app/busybar",
        "transport": "auto",  # "auto" | "local" | "cloud" (forced)
        "local_token": "",
        "fallback_hosts": [],
        "discover": False,
        "device_id": "",
    },
    "calendar_countdown": {
        # 10s matches busybar.display.AMBIENT_REDRAW_SECONDS -- the ambient
        # tier's redraw contract, tuned (after on-device re-measurement
        # showed a 15s poll only recovering the screen in 2 of 6 dwell
        # cycles) to match the overlay's 10s dwell gap exactly, so this
        # app's own redraws land inside those gaps far more often for
        # near-true alternation (v1.5; see the spec doc's v1.5 section for
        # both measurement rounds). Existing user configs that set
        # poll_seconds explicitly are unaffected -- this only changes the
        # out-of-the-box default.
        "poll_seconds": 10,
        "lookahead_hours": 12,
        "warn_minutes": 5,
        "notice_minutes": 15,
        "progress_window_minutes": 60,
        "include_all_day": False,
        "auto_busy": False,
        "calendars": [],
        # v1.5.2 escalation ladder -- see busybar/display.py's
        # PRIORITY_AMBIENT_RAISED/PRIORITY_AMBIENT_URGENT docstrings and
        # calendar_countdown.logic.select_priority for the full ladder.
        "approach_minutes": 30,   # <= this and > notice_minutes: PRIORITY_AMBIENT_RAISED
                                  # (can no longer be silently interrupted by the overlay
                                  # tier's CI badge/quota rotation)
        "imminent_minutes": 1,    # <= this (and not in_progress): LED blinks on every draw
        "chirp": True,            # one-time audio chirp exactly at event start (T-0);
                                  # set false to disable audio entirely
        # Stock-animation accents (2026-08-06). Icons/animation are device
        # stock, referenced by stock_path -- no assets bundled.
        "escalation_icons": True,       # animated calendar icons at warn (5m) / imminent (1m)
        "start_animation": "meeting_72x16",  # full-panel takeover for the first minute after
                                             # start (aligned with the T-0 chirp); "" disables
        "start_window_seconds": 60,     # how long the start takeover holds (also its urgent-priority window)
    },
    "ci_status": {
        "poll_seconds": 120,
        "repos": [],
        "show_green": False,
        "stale_queued_minutes": 0,  # 0 = disabled
        "show_running": True,
        "running_poll_seconds": 20,
        "show_quota": True,  # GraphQL/REST quota frames join the overlay
                            # rotation while a run is active (no effect if
                            # show_running is false -- see ci_status/main.py)
        # Account-wide watching (v1.5.1) -- off by default; the operator
        # enables it locally per-machine, not as a shipped default, since
        # it changes what gets polled/displayed without an explicit repo
        # list. See ci_status/README.md's "Account-wide watching" section
        # for quota math and the active-window caveat.
        "watch_account_repos": False,
        "repos_exclude": [],       # silence specific repos without leaving account mode
        "active_within_days": 30,  # only repos pushed within this window are polled
        "repo_refresh_minutes": 60,  # how often the repo list itself is re-enumerated
        "running_spinner": True,        # animated 8x8 spinner on the running badge
        "bitmap_icons": True,           # v1.2.3 cosmetic XPM2 accents when locally supported
    },
    "nyan_filler": {
        "enabled": True,
        "poll_seconds": 1,            # reclaims a dark gap within ~1s; the draw is
                                      # tiny and mostly-sleeping (see nyan_filler/README)
        "quiet_hours": "00:00-07:00", # local time; "" disables quiet hours entirely
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def device_kwargs(cfg: dict) -> dict:
    """Filter `cfg["device"]` down to BusyBarClient's known constructor
    kwargs, for use as `BusyBarClient(**device_kwargs(cfg))`.

    Before v1.6 both integration call sites only ever passed a single
    explicit keyword (`host=cfg["device"]["host"]`), so an unknown/typo'd
    [device] key in config.toml (e.g. `coud_token` for `cloud_token`) was
    silently ignored -- it just never got read. v1.6 switched both call
    sites to splat the whole [device] table so the three new cloud-
    transport keys wouldn't need updating twice; splatting an *unfiltered*
    dict, though, means that same typo now raises TypeError("unexpected
    keyword argument") at startup instead -- a cryptic crash, and the
    worst possible failure mode for exactly the moment an operator is
    most likely to typo a key: first-time cloud_token setup. This
    restores "unknown key doesn't crash startup" while adding
    observability the pre-v1.6 code never had: each ignored key is
    logged at WARNING (not silently dropped) so a genuine typo is still
    visible, just not fatal.

    The known-kwargs set is derived from BusyBarClient's own signature
    (rather than hardcoded here) so it can't drift out of sync with the
    constructor as transport options evolve.
    """
    known = set(inspect.signature(BusyBarClient.__init__).parameters) - {"self"}
    device = cfg.get("device", {})
    for key in sorted(set(device) - known):
        log.warning("config: ignoring unknown [device] key %r (not a BusyBarClient parameter)", key)
    return {k: v for k, v in device.items() if k in known}


def find_config() -> Path | None:
    candidate = Path(__file__).resolve().parents[2] / "config.toml"
    return candidate if candidate.exists() else None


def load_config(path: Path | None = None) -> dict:
    if path is None:
        path = find_config()
    cfg = copy.deepcopy(DEFAULTS)
    if path is not None and Path(path).exists():
        with open(path, "rb") as fh:
            cfg = _merge(cfg, tomllib.load(fh))
    env_host = os.environ.get("BUSYBAR_HOST")
    if env_host:
        cfg = _merge(cfg, {"device": {"host": env_host}})
    env_local_token = os.environ.get("BUSYBAR_LOCAL_TOKEN")
    if env_local_token:
        cfg = _merge(cfg, {"device": {"local_token": env_local_token}})
    return cfg
