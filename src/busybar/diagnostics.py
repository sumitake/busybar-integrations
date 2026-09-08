"""Small, read-only diagnostics for a BUSY Bar.

The diagnostic path deliberately uses the client's existing request surface.
It projects responses onto a small allowlist so a status endpoint cannot turn
the CLI into a configuration, token, or arbitrary firmware-dump printer.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
import struct
from typing import Any


ENDPOINTS = {
    "api": "/api/version",
    "transport": "/api/transport",
    "firmware": "/api/status/firmware",
    "power": "/api/status/power",
    "busy_snapshot": "/api/busy/snapshot",
}

_FIELDS = {
    "api": ("api_semver",),
    "transport": ("type",),
    "firmware": (
        "version",
        "target",
        "branch",
        "build_date",
        "commit_hash",
        "intercom_version",
        "nwp_version",
        "matter_version",
    ),
    "power": (
        "state",
        "battery_charge",
        "battery_voltage",
        "battery_current",
        "usb_voltage",
    ),
}

_REQUIRED = {
    "api": ("api_semver",),
    "transport": ("type",),
    "firmware": ("version", "target", "branch", "build_date", "commit_hash", "intercom_version"),
    "power": ("state", "battery_charge", "battery_voltage", "battery_current", "usb_voltage"),
}


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _safe_fields(payload: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    """Return only scalar allowlisted fields from a response mapping.

    The direct endpoint objects are deliberately not recursively unwrapped.
    """
    root = _mapping(payload)
    if root is None:
        return {}
    values: dict[str, Any] = {}
    for key in fields:
        value = root.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            if value is not None:
                values[key] = value
    return values


def _read_json(client: Any, path: str) -> tuple[Any | None, str | None]:
    """Read one allowlisted GET through the shared client."""
    try:
        value = client.get_json(path, local_only=True)
        return (value, None) if value is not None else (None, "unavailable")
    except Exception:
        # Diagnostics must remain useful when an optional endpoint is absent
        # or a transport cannot reach the device.  Do not print exception text:
        # it can contain URLs, headers, or other caller-controlled material.
        return None, "unavailable"


def diagnose(client: Any) -> dict[str, Any]:
    """Collect bounded, allowlisted, read-only evidence from *client*."""
    result: dict[str, Any] = {
        "reachable": False,
        "complete": False,
        "api": {"available": False},
        "transport": {"available": False},
        "firmware": {"available": False},
        "power": {"available": False},
        "features": {
            "busy_snapshot": {"available": False},
            "screen": {"available": None},
        },
    }
    errors: dict[str, str] = {}

    for section in ("api", "transport", "firmware", "power"):
        payload, error = _read_json(client, ENDPOINTS[section])
        fields = _safe_fields(payload, _FIELDS[section])
        result[section] = {"available": payload is not None, **fields}
        if payload is not None:
            result["reachable"] = True
        if not _valid(section, payload, fields):
            errors[section] = error or "invalid"

    payload, error = _read_json(client, ENDPOINTS["busy_snapshot"])
    snapshot = _mapping(payload)
    snapshot_body = snapshot.get("snapshot") if snapshot else None
    result["features"]["busy_snapshot"]["available"] = (
        isinstance(snapshot_body, Mapping) and isinstance(snapshot_body.get("type"), str)
    )
    if payload is not None:
        result["reachable"] = True
    if not result["features"]["busy_snapshot"]["available"]:
        errors["busy_snapshot"] = error or "invalid"

    api_version = result["api"].get("api_semver")
    result["features"]["display_v2"] = {"available": _version_at_least(api_version, (27, 5, 0))}
    result["complete"] = not errors

    if errors:
        result["unavailable"] = sorted(errors)
    return result


def _valid(section: str, payload: Any, fields: dict[str, Any]) -> bool:
    if not isinstance(payload, Mapping):
        return False
    if not all(key in fields for key in _REQUIRED[section]):
        return False
    if section == "api":
        return _version_tuple(payload.get("api_semver")) is not None
    if section == "transport":
        return payload.get("type") in {"usb", "wifi"}
    return True


def _version_at_least(value: Any, minimum: tuple[int, int, int]) -> bool:
    numbers = _version_tuple(value)
    return numbers is not None and numbers >= minimum


def _version_tuple(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    try:
        numbers = tuple(int(part) for part in value.split(".")[:3])
    except ValueError:
        return None
    if len(numbers) != 3:
        return None
    return numbers[0], numbers[1], numbers[2]


def save_screen(client: Any, destination: str) -> bool:
    """Save display-0 as a standard 72x16 24-bit BMP.

    Firmware labels the response ``image/bmp`` but returns a base64-encoded
    BGR24 framebuffer in the 1.2.3 path (the tag's ``api_streaming.c`` uses
    ``MG_REPLY_IMAGE`` over the display buffer). Accept an already-formed BMP
    too so a corrected server response remains readable without another HTTP
    stack.
    """
    try:
        data = client.get_bytes("/api/screen?display=0", local_only=True)
        if not isinstance(data, (bytes, bytearray)):
            return False
        data = bytes(data).strip()
        if data.startswith(b"BM"):
            bitmap = data
        else:
            try:
                bgr = base64.b64decode(data, validate=True)
            except (ValueError, binascii.Error):
                return False
            bitmap = _bgr_to_bmp(bgr)
            if bitmap is None:
                return False
        with open(destination, "wb") as handle:
            handle.write(bitmap)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _bgr_to_bmp(bgr: bytes, width: int = 72, height: int = 16) -> bytes | None:
    """Wrap the firmware's row-major BGR24 framebuffer in a BMP header."""
    if len(bgr) != width * height * 3:
        return None
    row_bytes = width * 3
    stride = (row_bytes + 3) & ~3
    pixels = bytearray()
    for row in range(height - 1, -1, -1):
        source = bgr[row * row_bytes:(row + 1) * row_bytes]
        pixels.extend(source)
        pixels.extend(b"\x00" * (stride - row_bytes))
    header = struct.pack(
        "<2sIHHIIIIHHIIIIII", b"BM", 54 + len(pixels), 0, 0, 54, 40,
        width, height, 1, 24, 0, len(pixels), 2835, 2835, 0, 0,
    )
    return header + pixels
