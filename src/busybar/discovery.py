"""Short-lived, opt-in BUSY Bar mDNS discovery.

mDNS advertising is useful for a trusted LAN, but it is not authentication.
Callers that choose a device must pass its configured USB-MAC-derived ID; this
module never silently chooses the first service it sees.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any

log = logging.getLogger(__name__)

SERVICE_TYPE = "_http._tcp.local."
MAX_TIMEOUT_SECONDS = 3.0
MAX_HOSTS = 4
MAX_RECORDS = 16
_DEVICE_ID = re.compile(r"^[0-9a-f]{12}$")
_INSTANCE_PREFIX = "busybar-"


class DiscoveryUnavailable(RuntimeError):
    """The optional discovery scan could not produce trustworthy scan results."""


@dataclass(frozen=True)
class DiscoveredDevice:
    """A parsed BUSY Bar service advertisement, without TXT trust claims."""

    device_id: str
    name: str
    hosts: tuple[str, ...]
    port: int


def _service_device_id(name: str) -> str | None:
    suffix = SERVICE_TYPE.lower()
    normalized = name.lower()
    if not normalized.endswith(suffix):
        return None
    instance = normalized[: -len(suffix)].rstrip(".")
    if not instance.startswith(_INSTANCE_PREFIX):
        return None
    device_id = instance.removeprefix(_INSTANCE_PREFIX)
    return device_id if _DEVICE_ID.fullmatch(device_id) else None


class _NamesOnlyListener:
    def __init__(self) -> None:
        self.names: set[str] = set()
        self._lock = Lock()

    def add_service(self, _zc: Any, _service_type: str, name: str) -> None:
        with self._lock:
            self.names.add(name)

    def update_service(self, _zc: Any, _service_type: str, name: str) -> None:
        with self._lock:
            self.names.add(name)

    def remove_service(self, _zc: Any, _service_type: str, _name: str) -> None:
        return

    def snapshot(self) -> list[str]:
        with self._lock:
            return sorted(self.names)


def _ipv4_hosts(info: Any) -> tuple[str, ...]:
    candidates: list[str] = []
    try:
        addresses = info.parsed_addresses()
    except (AttributeError, OSError, ValueError):
        addresses = []
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if parsed.version == 4 and address not in candidates:
            candidates.append(address)
        if len(candidates) == MAX_HOSTS:
            break
    return tuple(candidates)


def discover_devices(timeout: float = MAX_TIMEOUT_SECONDS, *, device_id: str = "") -> list[DiscoveredDevice]:
    """Scan for at most three seconds and return parsed matching advertisements.

    An empty ``device_id`` lists valid BUSY Bar records for diagnostics. A
    nonempty value must be the exact lower/uppercase USB-MAC-derived 12-hex
    ID. Firmware advertises it as ``busybar-<id>._http._tcp.local.``. This
    function creates no listener that outlives the call, and resolves services
    only after browsing has finished.
    """
    expected = device_id.lower()
    if expected and not _DEVICE_ID.fullmatch(expected):
        log.warning("busybar discovery needs a 12-hex device_id")
        return []
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError as exc:
        raise DiscoveryUnavailable("Install the optional 'discovery' dependency") from exc

    wait_seconds = min(max(float(timeout), 0.0), MAX_TIMEOUT_SECONDS)
    deadline = time.monotonic() + wait_seconds
    # Leave a small part of the bounded scan for synchronous resolution after
    # the browser has been stopped. A full three-second browse would leave no
    # time to turn collected names into usable IPv4 candidates.
    browse_seconds = max(0.0, wait_seconds - min(0.5, wait_seconds / 2))
    listener = _NamesOnlyListener()
    zc = None
    browser = None
    try:
        zc = Zeroconf()
        browser = ServiceBrowser(zc, SERVICE_TYPE, listener)
        time.sleep(browse_seconds)
        browser.cancel()
        browser = None
        records: list[DiscoveredDevice] = []
        unresolved = False
        for name in listener.snapshot():
            found_id = _service_device_id(name)
            if found_id is None or (expected and found_id != expected):
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                unresolved = True
                break
            info = zc.get_service_info(SERVICE_TYPE, name, timeout=max(1, int(remaining * 1000)))
            if info is None:
                unresolved = True
                continue
            hosts = _ipv4_hosts(info)
            if not hosts:
                continue
            advertised_port = int(getattr(info, "port", 0) or 0)
            port = advertised_port if advertised_port > 0 else 80
            records.append(DiscoveredDevice(found_id, name, hosts, port))
            if len(records) == MAX_RECORDS:
                break
        if unresolved and not records:
            raise DiscoveryUnavailable("Matching service could not be resolved within scan deadline")
        return records
    except DiscoveryUnavailable:
        raise
    except Exception as exc:
        raise DiscoveryUnavailable("Discovery scan failed") from exc
    finally:
        try:
            if browser is not None:
                browser.cancel()
        finally:
            if zc is not None:
                zc.close()
