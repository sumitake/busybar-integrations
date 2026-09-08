import sys
import pytest
from types import SimpleNamespace

from busybar.discovery import DiscoveredDevice, DiscoveryUnavailable, discover_devices


class _Info:
    def __init__(self, addresses, port=80):
        self._addresses = addresses
        self.port = port

    def parsed_addresses(self):
        return self._addresses


def _fake_zeroconf(monkeypatch, names, infos):
    state = SimpleNamespace(cancelled=False, closed=False)

    class Zeroconf:
        def get_service_info(self, _service_type, name, timeout):
            assert timeout >= 1
            return infos.get(name)

        def close(self):
            state.closed = True

    class ServiceBrowser:
        def __init__(self, _zc, service_type, listener):
            assert service_type == "_http._tcp.local."
            for name in names:
                listener.add_service(None, service_type, name)

        def cancel(self):
            state.cancelled = True

    monkeypatch.setitem(sys.modules, "zeroconf", SimpleNamespace(Zeroconf=Zeroconf, ServiceBrowser=ServiceBrowser))
    return state


def test_discovers_only_exact_device_identity_and_deduplicates_ipv4(monkeypatch):
    wanted = "busybar-aabbccddeeff._http._tcp.local."
    state = _fake_zeroconf(
        monkeypatch,
        ["other._http._tcp.local.", "aabbccddeeff._http._tcp.local.", wanted, "busybar-112233445566._http._tcp.local."],
        {wanted: _Info(["192.0.2.2", "2001:db8::1", "192.0.2.2", "192.0.2.3"], 8123)},
    )
    monkeypatch.setattr("busybar.discovery.time.sleep", lambda _seconds: None)
    records = discover_devices(device_id="AABBCCDDEEFF")
    assert records == [DiscoveredDevice("aabbccddeeff", wanted, ("192.0.2.2", "192.0.2.3"), 8123)]
    assert state.cancelled and state.closed


def test_discovery_uses_port_80_for_logical_zero_and_ignores_malformed(monkeypatch):
    name = "busybar-aabbccddeeff._http._tcp.local."
    _fake_zeroconf(monkeypatch, ["busybar-not-a-mac._http._tcp.local.", name], {name: _Info(["192.0.2.4"], 0)})
    waited = []
    monkeypatch.setattr("busybar.discovery.time.sleep", waited.append)
    records = discover_devices(timeout=99)
    assert len(records) == 1
    assert records[0].port == 80
    assert waited == [2.5]


def test_discovery_closes_resources_when_service_resolution_fails(monkeypatch):
    name = "busybar-aabbccddeeff._http._tcp.local."
    state = _fake_zeroconf(monkeypatch, [name], {})
    monkeypatch.setattr("busybar.discovery.time.sleep", lambda _seconds: None)
    with pytest.raises(DiscoveryUnavailable):
        discover_devices()
    assert state.cancelled and state.closed


def test_missing_optional_dependency_is_reported_as_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "zeroconf", None)
    with pytest.raises(DiscoveryUnavailable):
        discover_devices(device_id="aabbccddeeff")


def test_successful_empty_scan_is_distinct_from_unavailable(monkeypatch):
    state = _fake_zeroconf(monkeypatch, [], {})
    monkeypatch.setattr("busybar.discovery.time.sleep", lambda _: None)
    assert discover_devices() == []
    assert state.cancelled and state.closed
