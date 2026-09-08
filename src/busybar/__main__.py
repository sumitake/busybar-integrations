"""Command-line entry points for bounded, read-only BUSY Bar operations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from busybar.client import BusyBarClient
from busybar.config import device_kwargs, load_config
from busybar.diagnostics import diagnose, save_screen


def _client(config_path: str | None, host: str | None) -> BusyBarClient:
    config = load_config(Path(config_path) if config_path else None)
    kwargs = device_kwargs(config)
    if host:
        kwargs["host"] = host
    return BusyBarClient(**kwargs)


def _safe_device(value: Any) -> dict[str, Any]:
    allowed = ("name", "host", "hosts", "port", "device_id", "model", "service")
    if isinstance(value, dict):
        source = value
    else:
        source = {key: getattr(value, key) for key in allowed if hasattr(value, key)}
    return {key: source[key] for key in allowed if key in source}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m busybar")
    subparsers = parser.add_subparsers(dest="command", required=True)
    diagnostic = subparsers.add_parser("diagnose", help="read safe device diagnostics")
    diagnostic.add_argument("--host")
    diagnostic.add_argument("--config", help="path to a BUSY Bar config.toml")
    diagnostic.add_argument("--screen", help="save the local display-0 BMP when supported")
    discovery = subparsers.add_parser("discover", help="discover BUSY Bars on the local network")
    discovery.add_argument("--timeout", type=float, default=3.0)
    return parser


def _run_discover(timeout: float) -> int:
    try:
        from busybar.discovery import discover_devices
        devices = discover_devices(timeout)
    except Exception:
        # The discovery layer owns the bounded scan and should propagate a
        # scan/dependency failure.  Do not add a second network health probe.
        devices = None
    if devices is None:
        print(json.dumps({"devices": [], "available": False}, indent=2, sort_keys=True))
        return 2
    print(json.dumps({"devices": [_safe_device(item) for item in devices], "available": True},
                     indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "discover":
        return _run_discover(args.timeout)

    client = _client(args.config, args.host)
    report = diagnose(client)
    if args.screen:
        saved = save_screen(client, args.screen)
        report["features"]["screen"] = {"available": saved, "path": args.screen if saved else None}
        report["complete"] = report["complete"] and saved
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
