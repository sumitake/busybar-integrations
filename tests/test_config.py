import logging
import tomllib
from pathlib import Path
from busybar.config import DEFAULTS, device_kwargs, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]

def test_defaults_when_no_file(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg["device"]["host"] == "10.0.4.20"
    # v1.6 cloud transport fallback -- disabled out of the box.
    assert cfg["device"]["cloud_token"] == ""
    assert cfg["device"]["cloud_base_url"] == "https://api.busy.app/busybar"
    assert cfg["device"]["transport"] == "auto"
    assert cfg["calendar_countdown"]["poll_seconds"] == 10  # v1.5 ambient-tier default
    assert cfg["ci_status"]["repos"] == []
    assert cfg["ci_status"]["show_running"] is True
    assert cfg["ci_status"]["running_poll_seconds"] == 20
    assert cfg["ci_status"]["show_quota"] is True
    # v1.5.1 account-wide watching defaults
    assert cfg["ci_status"]["watch_account_repos"] is False
    assert cfg["ci_status"]["repos_exclude"] == []
    assert cfg["ci_status"]["active_within_days"] == 30
    assert cfg["ci_status"]["repo_refresh_minutes"] == 60

def test_file_overrides_defaults(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[device]\nhost = "192.0.2.7"\n[ci_status]\nrepos = ["a/b"]\n')
    cfg = load_config(p)
    assert cfg["device"]["host"] == "192.0.2.7"
    assert cfg["ci_status"]["repos"] == ["a/b"]
    assert cfg["calendar_countdown"]["warn_minutes"] == 5  # untouched default

def test_env_overrides_file(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text('[device]\nhost = "192.0.2.7"\n')
    monkeypatch.setenv("BUSYBAR_HOST", "192.0.2.99")
    assert load_config(p)["device"]["host"] == "192.0.2.99"

def test_local_token_env_overrides_file(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text('[device]\nlocal_token = "file-token"\n')
    monkeypatch.setenv("BUSYBAR_LOCAL_TOKEN", "env-token")
    assert load_config(p)["device"]["local_token"] == "env-token"

def test_returned_config_mutation_does_not_corrupt_defaults(tmp_path):
    # Mutate the returned config in place
    cfg1 = load_config(tmp_path / "missing.toml")
    cfg1["ci_status"]["repos"].append("mutated/repo")
    cfg1["calendar_countdown"]["poll_seconds"] = 999

    # Load a fresh config and verify it is unaffected
    cfg2 = load_config(tmp_path / "missing.toml")
    assert cfg2["ci_status"]["repos"] == []
    assert cfg2["calendar_countdown"]["poll_seconds"] == 10


# --- config.example.toml parity (v1.6) --------------------------------------
# config.example.toml's own header claims "All keys optional; defaults
# shown" -- these tests hold that claim to account for the [device] table
# specifically, since it's the one this round touches and the one where a
# drifted example (e.g. a non-empty placeholder token) would be a real
# hygiene problem, not just documentation staleness.

def test_example_toml_device_section_matches_defaults():
    with open(REPO_ROOT / "config.example.toml", "rb") as fh:
        example = tomllib.load(fh)
    assert example["device"] == DEFAULTS["device"]

def test_example_toml_cloud_token_is_empty_placeholder_not_a_real_looking_token():
    with open(REPO_ROOT / "config.example.toml", "rb") as fh:
        example = tomllib.load(fh)
    # Guards against ever accidentally shipping a real-looking credential
    # in the committed example file -- the real token belongs only in the
    # git-ignored config.toml.
    assert example["device"]["cloud_token"] == ""


# --- device_kwargs() unknown-key guard (final-gate review, v1.6) -----------
# Both integration call sites splat cfg["device"] into BusyBarClient's
# constructor. Splatting the raw dict would TypeError on any unknown/typo'd
# [device] key (a silent-ignore -> crash regression from pre-v1.6, where
# only host= was ever passed explicitly) -- device_kwargs() must filter to
# known kwargs and warn, not crash.

def test_device_kwargs_passes_through_all_known_keys():
    cfg = {"device": {"host": "192.0.2.1", "cloud_token": "x",
                      "cloud_base_url": "https://cloud.example.test",
                      "transport": "local"}}
    assert device_kwargs(cfg) == cfg["device"]

def test_device_kwargs_drops_unknown_key_without_crashing(caplog):
    cfg = {"device": {"host": "192.0.2.1", "coud_token": "typo'd-key"}}
    caplog.set_level(logging.WARNING, logger="busybar.config")
    result = device_kwargs(cfg)
    assert result == {"host": "192.0.2.1"}  # unknown key silently dropped, not crashed
    assert "coud_token" in caplog.text
    assert any(record.levelname == "WARNING" for record in caplog.records)

def test_device_kwargs_result_never_crashes_busybarclient_construction():
    from busybar.client import BusyBarClient
    cfg = {"device": {"host": "192.0.2.1", "cloud_token": "x", "bogus_extra_key": 123}}
    # This is the actual regression this guard exists for: a typo'd or
    # unrecognized [device] key must not raise TypeError when splatted.
    client = BusyBarClient(**device_kwargs(cfg))
    assert client.base == "http://192.0.2.1"

def test_device_kwargs_logs_one_warning_per_unknown_key(caplog):
    cfg = {"device": {"host": "192.0.2.1", "bogus_one": 1, "bogus_two": 2}}
    caplog.set_level(logging.WARNING, logger="busybar.config")
    device_kwargs(cfg)
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2
    assert any("bogus_one" in r.getMessage() for r in warnings)
    assert any("bogus_two" in r.getMessage() for r in warnings)

def test_device_kwargs_no_warnings_when_all_keys_known(caplog):
    caplog.set_level(logging.WARNING, logger="busybar.config")
    device_kwargs({"device": dict(DEFAULTS["device"])})
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_nyan_filler_defaults(tmp_path):
    # Hermetic: load from an isolated tmp_path with no [nyan_filler] section
    # (mirrors test_defaults_when_no_file's isolation) rather than path=None,
    # which would read any real repo-root config.toml and break once the
    # operator adds a [nyan_filler] section there.
    cfg = load_config(tmp_path / "missing.toml")["nyan_filler"]
    assert cfg == {"enabled": True, "poll_seconds": 1, "quiet_hours": "00:00-07:00"}


def test_animation_accent_defaults(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    cal = cfg["calendar_countdown"]
    assert cal["escalation_icons"] is True
    assert cal["start_animation"] == "meeting_72x16"
    assert cal["start_window_seconds"] == 60
    assert cfg["ci_status"]["running_spinner"] is True
    assert cfg["ci_status"]["bitmap_icons"] is True
