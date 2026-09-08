"""Bounded HTTP client for a BUSY Bar device.

The local route is preferred. Cloud relay support remains optional, and local
credentials are deliberately never sent to that relay. Discovery is a
trusted-LAN convenience only; the configured service identifier prevents us
from selecting an arbitrary discovered bar, but does not authenticate a LAN
advertisement.
"""

from __future__ import annotations

import logging
import time
from enum import Enum
from pathlib import PurePosixPath
from typing import Any

import requests

log = logging.getLogger(__name__)

NULL_CARD_ID = "00000000-0000-0000-0000-000000000000"
LOCAL_RETRY_SECONDS = 60
MAX_FALLBACK_HOSTS = 3


class DrawResult(Enum):
    DRAWN = "drawn"
    REJECTED = "rejected"
    UNREACHABLE = "unreachable"
    ERROR = "error"


def _version_at_least(value: object, minimum: tuple[int, int, int]) -> bool:
    """Accept ordinary API version strings without adding a version package."""
    if not isinstance(value, str):
        return False
    try:
        parts = tuple(int(part) for part in value.split(".")[:3])
    except ValueError:
        return False
    return len(parts) == 3 and parts >= minimum


class BusyBarClient:
    """Synchronously talk to one configured BUSY Bar.

    ``fallback_hosts`` is intentionally a small, explicit list. Optional mDNS
    discovery runs only when ``discover`` and an exact ``device_id`` are
    supplied. Both paths are finite and have no background listener or retry
    worker.
    """

    def __init__(
        self,
        host: str = "10.0.4.20",
        timeout: tuple = (3, 5),
        *,
        cloud_token: str = "",
        cloud_base_url: str = "https://api.busy.app/busybar",
        transport: str = "auto",
        cloud_timeout: tuple = (5, 15),
        local_token: str = "",
        fallback_hosts: list[str] | tuple[str, ...] = (),
        discover: bool = False,
        device_id: str = "",
    ):
        if transport not in ("auto", "local", "cloud"):
            raise ValueError(f"transport must be 'auto', 'local', or 'cloud', got {transport!r}")
        if len(fallback_hosts) > MAX_FALLBACK_HOSTS:
            raise ValueError(f"fallback_hosts supports at most {MAX_FALLBACK_HOSTS} hosts")
        if discover and not device_id:
            raise ValueError("discover requires an exact device_id")

        self.timeout = timeout
        self.cloud_token = cloud_token
        self.local_token = local_token
        self.cloud_base = cloud_base_url.rstrip("/")
        self.cloud_timeout = cloud_timeout
        self.transport = transport
        self.device_id = device_id.lower()
        self.discover = discover
        self._cloud_configured = bool(cloud_token)
        self.active_transport = "cloud" if transport == "cloud" else "local"
        self._degraded_since: float | None = None
        self._last_primary_probe = 0.0
        self._last_discovery: float | None = None
        self._capabilities_checked_at: float | None = None
        self.supports_display_v2 = False

        routes: list[str] = []
        for candidate in (host, *fallback_hosts):
            if candidate and candidate not in routes:
                routes.append(candidate)
        self._static_hosts = routes
        self._discovered_hosts: list[str] = []
        self._local_index = 0
        self.base = self._base_for(self._static_hosts[0])
        if self.discover:
            self._refresh_discovery()

    @staticmethod
    def _base_for(host: str) -> str:
        return host if host.startswith(("http://", "https://")) else f"http://{host}"

    def _all_local_hosts(self) -> list[str]:
        return (self._static_hosts + [h for h in self._discovered_hosts if h not in self._static_hosts])[:4]

    def _mark_degraded(self) -> None:
        if self.active_transport != "cloud":
            log.info("busybar transport: local -> cloud (local route unavailable)")
        self.active_transport = "cloud"
        self._degraded_since = time.monotonic()

    def _mark_recovered(self) -> None:
        if self.active_transport != "local":
            log.info("busybar transport: cloud -> local (local route recovered)")
        self.active_transport = "local"
        self._degraded_since = None

    def _should_probe_local(self) -> bool:
        return self._degraded_since is not None and time.monotonic() - self._degraded_since >= LOCAL_RETRY_SECONDS

    def _cloud_path(self, path: str) -> str:
        return path[len("/api"):] if path.startswith("/api") else path

    def _refresh_discovery(self) -> None:
        now = time.monotonic()
        if not self.discover or (self._last_discovery is not None and now - self._last_discovery < LOCAL_RETRY_SECONDS):
            return
        self._last_discovery = now
        try:
            from busybar.discovery import discover_devices

            discovered: list[str] = []
            for record in discover_devices(device_id=self.device_id):
                for host in record.hosts:
                    endpoint = f"{host}:{record.port}" if record.port != 80 else host
                    if endpoint not in discovered:
                        discovered.append(endpoint)
                    if len(discovered) == 4:
                        break
                if len(discovered) == 4:
                    break
            self._discovered_hosts = discovered
        except Exception:
            # Optional discovery must never prevent configured routes.
            log.warning("busybar discovery unavailable; using configured local routes")

    def _local_order(self) -> list[str]:
        hosts = self._all_local_hosts()
        if not hosts:
            return []
        now = time.monotonic()
        if self._local_index and now - self._last_primary_probe >= LOCAL_RETRY_SECONDS:
            self._last_primary_probe = now
            return [hosts[0], *[h for h in hosts if h != hosts[0]]]
        preferred = min(self._local_index, len(hosts) - 1)
        return [hosts[preferred], *[h for i, h in enumerate(hosts) if i != preferred]]

    def _try_local(
        self, method: str, path: str, *, replay_safe: bool, **kwargs: Any
    ) -> tuple[requests.Response | None, BaseException | None]:
        routes = self._local_order()
        if not routes:
            return None, None
        headers = dict(kwargs.pop("headers", None) or {})
        if self.local_token:
            headers["X-API-Token"] = self.local_token
        refreshed = False
        for attempt, route in enumerate(routes):
            try:
                response = requests.request(
                    method, f"{self._base_for(route)}{path}", timeout=self.timeout,
                    headers=headers or None, allow_redirects=False, **kwargs,
                )
            except requests.ConnectTimeout as exc:
                failure: BaseException = exc
            except requests.RequestException as exc:
                log.debug("local route unavailable (%s)", type(exc).__name__)
                if not replay_safe:
                    return None, exc
                failure = exc
            else:
                self.base = self._base_for(route)
                self._local_index = self._all_local_hosts().index(route)
                return response, None
            if not replay_safe and not isinstance(failure, requests.ConnectTimeout):
                return None, failure
            if attempt == len(routes) - 1 and not refreshed:
                # Refresh once after exhausting stale routes. Append only new
                # addresses that fit this operation's original four-attempt
                # budget. Uncertain writes return above, before discovery.
                self._refresh_discovery()
                refreshed = True
                for candidate in self._local_order():
                    if candidate not in routes and len(routes) < 4:
                        routes.append(candidate)
        return None, failure

    def _try_cloud(self, method: str, path: str, **kwargs: Any) -> requests.Response | None:
        headers = dict(kwargs.pop("headers", None) or {})
        headers["Authorization"] = f"Bearer {self.cloud_token}"
        try:
            return requests.request(
                method, f"{self.cloud_base}{self._cloud_path(path)}", timeout=self.cloud_timeout,
                headers=headers, allow_redirects=False, **kwargs,
            )
        except requests.RequestException as exc:
            log.debug("cloud route unavailable (%s)", type(exc).__name__)
            return None

    def _request(
        self, method: str, path: str, *, replay_safe: bool = True,
        local_only: bool = False, cloud_kwargs: dict[str, Any] | None = None, **kwargs: Any,
    ) -> requests.Response | None:
        if self.transport == "cloud":
            return None if local_only else self._try_cloud(method, path, **(cloud_kwargs or kwargs))
        if self.transport == "local" or local_only:
            return self._try_local(method, path, replay_safe=replay_safe, **kwargs)[0]

        if self.active_transport == "local" or self._should_probe_local():
            response, failure = self._try_local(method, path, replay_safe=replay_safe, **kwargs)
            if response is not None:
                self._mark_recovered()
                return response
            if not self._cloud_configured or (not replay_safe and not isinstance(failure, requests.ConnectTimeout)):
                return None
            self._mark_degraded()
        return self._try_cloud(method, path, **(cloud_kwargs or kwargs))

    def _display_payload(self, elements: list[dict], modern: bool) -> list[dict]:
        payload: list[dict] = []
        for index, raw in enumerate(elements):
            item = dict(raw)
            if not modern and item.get("type") == "xpmbitmap":
                continue
            if modern:
                item.setdefault("z_index", index)
            else:
                item.pop("z_index", None)
                item.pop("xpmbitmap", None)
            payload.append(item)
        return payload

    def draw(self, application_name: str, elements: list[dict], priority: int = 50,
             led_notification_color: str | None = None) -> DrawResult:
        local_modern = self.transport != "cloud" and self.active_transport == "local" and self.supports_display_v2
        safe_elements = self._display_payload(elements, modern=False)
        modern_elements = self._display_payload(elements, modern=True)
        bitmap_only = bool(elements) and not safe_elements
        if bitmap_only and not local_modern:
            return DrawResult.ERROR

        def body_for(items: list[dict]) -> dict[str, Any]:
            body: dict[str, Any] = {"application_name": application_name, "priority": priority, "elements": items}
            if led_notification_color is not None:
                body["led_notification_color"] = led_notification_color
            return body

        response = self._request(
            "POST", "/api/display/draw", json=body_for(modern_elements if local_modern else safe_elements),
            cloud_kwargs={"json": body_for(safe_elements)}, local_only=bitmap_only,
        )
        if response is None:
            return DrawResult.UNREACHABLE
        if response.status_code == 409:
            return DrawResult.REJECTED
        if response.status_code == 200:
            return DrawResult.DRAWN
        log.warning("draw failed: HTTP %s", response.status_code)
        return DrawResult.ERROR

    def play_audio(self, application_name: str, stock_path: str | None = None, path: str | None = None) -> bool:
        """Queue one sound without changing the device's volume setting.

        A 200 acknowledges the queued request, not audible playback. In
        particular, firmware stock paths are runtime ``.snd`` assets rather
        than their source-tree ``.wav`` names. A read or connection failure is
        treated as uncertain and is never replayed to another route.
        """
        if (stock_path is None) == (path is None):
            raise ValueError("play_audio requires exactly one of stock_path or path")
        body: dict[str, str] = {"application_name": application_name}
        body["stock_path" if stock_path is not None else "path"] = stock_path if stock_path is not None else path  # type: ignore[assignment]
        response = self._request("POST", "/api/audio/play", json=body, replay_safe=False)
        return response is not None and response.status_code == 200

    def clear(self, application_name: str) -> bool:
        if not application_name:
            raise ValueError("clear requires an application_name")
        response = self._request("DELETE", "/api/display/draw", params={"application_name": application_name})
        return response is not None and response.status_code == 200

    def remove_elements(self, application_name: str, ids: list[str]) -> bool:
        """Delete named elements without sending the firmware-buggy body owner."""
        if not application_name:
            raise ValueError("remove_elements requires an application_name")
        if not ids:
            return True
        if self.transport == "cloud" or self.active_transport == "cloud" or not self.supports_display_v2:
            return False
        response = self._request(
            "DELETE", "/api/display/draw", local_only=True,
            params={"application_name": application_name}, json={"element_ids": ids},
        )
        return response is not None and response.status_code == 200

    def upload_asset(self, application_name: str, filename: str, data: bytes) -> bool:
        path = PurePosixPath(filename)
        if path.is_absolute() or ".." in path.parts or filename in ("", "."):
            return False
        response = self._request(
            "POST", "/api/assets/upload", local_only=True,
            params={"application_name": application_name, "file": filename}, data=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        return response is not None and response.status_code == 200

    @staticmethod
    def _json_dict(response: requests.Response | None) -> dict | None:
        if response is None or response.status_code != 200:
            return None
        try:
            value = response.json()
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def get_json(self, path: str, *, local_only: bool = False) -> dict | None:
        if path not in {
            "/api/status", "/api/version", "/api/busy/snapshot",
            "/api/transport", "/api/status/firmware", "/api/status/power",
        }:
            raise ValueError("unsupported diagnostic endpoint")
        return self._json_dict(self._request("GET", path, local_only=local_only))

    def get_bytes(self, path: str, *, local_only: bool = True) -> bytes | None:
        """Read a fixed diagnostic byte stream, currently the front/back screen."""
        if path not in {"/api/screen?display=0", "/api/screen?display=1"}:
            raise ValueError("unsupported diagnostic endpoint")
        if not local_only:
            raise ValueError("screen diagnostics are local only")
        response = self._request("GET", path, local_only=local_only)
        return response.content if response is not None and response.status_code == 200 else None

    def refresh_capabilities(self) -> bool:
        now = time.monotonic()
        if self._capabilities_checked_at is not None and now - self._capabilities_checked_at < LOCAL_RETRY_SECONDS:
            return self.supports_display_v2
        self._capabilities_checked_at = now
        self.supports_display_v2 = False
        if self.transport == "cloud" or self.active_transport == "cloud":
            return False
        version = self.get_json("/api/version", local_only=True)
        if version is not None:
            self.supports_display_v2 = _version_at_least(version.get("api_semver"), (27, 5, 0))
        return self.supports_display_v2

    def status(self) -> dict | None:
        return self.get_json("/api/status")

    def get_busy(self) -> dict | None:
        return self.get_json("/api/busy/snapshot")

    def set_busy_simple(self, time_left_ms: int) -> bool:
        """Start a SIMPLE BUSY session using the firmware's nested snapshot.

        The timestamp must be current: the device can return 200 for a stale
        timestamp while applying no state change. Because this writes device
        state, uncertain send/read failures are not replayed.
        """
        body = {
            "snapshot": {"type": "SIMPLE", "card_id": NULL_CARD_ID, "time_left_ms": time_left_ms, "is_paused": False},
            "snapshot_timestamp_ms": int(time.time() * 1000),
        }
        response = self._request("PUT", "/api/busy/snapshot", json=body, replay_safe=False)
        return response is not None and response.status_code == 200
