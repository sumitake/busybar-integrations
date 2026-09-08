import base64
import json

from busybar import diagnostics
from busybar.__main__ import main


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.paths = []

    def get_json(self, path, *, local_only=False):
        self.paths.append((path, local_only))
        value = self.responses.get(path)
        if isinstance(value, Exception):
            raise value
        return value


def test_diagnose_projects_allowlisted_fields_and_never_prints_token():
    token = "super-secret-token"
    client = FakeClient({
        "/api/version": {"api_semver": "27.5.0", "token": token},
        "/api/transport": {"type": "wifi", "headers": {"X-API-Token": token}},
        "/api/status/firmware": {"version": "1.2.3", "config": token},
        "/api/status/power": {"battery_charge": 88, "raw": {"token": token}},
        "/api/busy/snapshot": {"snapshot": {"type": "SIMPLE", "token": token}},
    })

    result = diagnostics.diagnose(client)

    assert result["reachable"] is True
    assert result["api"] == {"available": True, "api_semver": "27.5.0"}
    assert result["transport"] == {"available": True, "type": "wifi"}
    assert result["firmware"] == {"available": True, "version": "1.2.3"}
    assert result["power"] == {"available": True, "battery_charge": 88}
    assert token not in json.dumps(result)
    assert all(local_only for _, local_only in client.paths)


def test_diagnose_keeps_partial_evidence_and_marks_bad_json_unknown():
    client = FakeClient({
        "/api/version": {"api_semver": "27.5.0"},
        "/api/transport": ValueError("malformed response"),
        "/api/status/firmware": None,
        "/api/status/power": {"state": "usb"},
        "/api/busy/snapshot": None,
    })

    result = diagnostics.diagnose(client)

    assert result["reachable"] is True
    assert result["api"]["available"] is True
    assert result["transport"] == {"available": False}
    assert result["power"] == {"available": True, "state": "usb"}
    assert result["features"]["busy_snapshot"]["available"] is False
    assert result["unavailable"] == ["busy_snapshot", "firmware", "power", "transport"]


def test_diagnose_requires_complete_direct_sections_for_success():
    client = FakeClient({
        "/api/version": {"api_semver": "27.5.0"},
        "/api/transport": {"type": "wifi"},
        "/api/status/firmware": {
            "version": "1.2.3", "target": 22, "branch": "main",
            "build_date": "2026-01-01", "commit_hash": "abc",
            "intercom_version": "1",
        },
        "/api/status/power": {
            "state": "charging", "battery_charge": 88,
            "battery_voltage": 4100, "battery_current": 100, "usb_voltage": 5000,
        },
        "/api/busy/snapshot": {"snapshot": {"type": "SIMPLE"}},
    })

    result = diagnostics.diagnose(client)

    assert result["complete"] is True
    assert result["features"]["display_v2"]["available"] is True
    assert result["api"] == {"available": True, "api_semver": "27.5.0"}
    assert result["firmware"]["version"] == "1.2.3"
    assert result["power"]["battery_charge"] == 88
    assert result["features"]["busy_snapshot"]["available"] is True


def test_cli_unreachable_is_nonzero_without_mutating_calls(monkeypatch, capsys):
    client = FakeClient({path: None for path in diagnostics.ENDPOINTS.values()})
    monkeypatch.setattr("busybar.__main__._client", lambda config, host: client)

    assert main(["diagnose", "--host", "192.0.2.7"]) == 2

    output = json.loads(capsys.readouterr().out)
    assert output["reachable"] is False
    assert output["features"]["screen"]["available"] is None
    assert all(path.startswith("/api/") for path, _ in client.paths)
    assert "/api/log_dump" not in [path for path, _ in client.paths]


def test_cli_discover_uses_bounded_discovery_contract(monkeypatch, capsys):
    import sys
    import types

    seen = []
    module = types.ModuleType("busybar.discovery")
    module.discover_devices = lambda timeout: seen.append(timeout) or [
        {"name": "BUSY", "host": "192.0.2.10", "port": 80, "token": "omit"}
    ]
    monkeypatch.setitem(sys.modules, "busybar.discovery", module)

    assert main(["discover", "--timeout", "3"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert seen == [3.0]
    assert output["devices"] == [{"host": "192.0.2.10", "name": "BUSY", "port": 80}]
    assert "token" not in json.dumps(output)


def test_cli_discover_reports_propagated_scan_failure(monkeypatch, capsys):
    import sys
    import types

    module = types.ModuleType("busybar.discovery")
    module.discover_devices = lambda timeout: (_ for _ in ()).throw(RuntimeError("scan"))
    monkeypatch.setitem(sys.modules, "busybar.discovery", module)

    assert main(["discover"]) == 2
    assert json.loads(capsys.readouterr().out) == {"available": False, "devices": []}


def test_save_screen_requires_client_byte_helper_and_raw_bmp(tmp_path):
    class ScreenClient:
        def get_bytes(self, path, *, local_only=False):
            assert path == "/api/screen?display=0"
            assert local_only is True
            return b"BMdemo"

    destination = tmp_path / "front.bmp"
    assert diagnostics.save_screen(ScreenClient(), str(destination)) is True
    assert destination.read_bytes() == b"BMdemo"


def test_save_screen_decodes_firmware_base64_bgr_to_bmp(tmp_path):
    bgr = bytes((0, 0, 255)) * (72 * 16)

    class ScreenClient:
        def get_bytes(self, path, *, local_only=False):
            return base64.b64encode(bgr)

    destination = tmp_path / "front.bmp"
    assert diagnostics.save_screen(ScreenClient(), str(destination)) is True
    bitmap = destination.read_bytes()
    assert bitmap[:2] == b"BM"
    assert int.from_bytes(bitmap[18:22], "little") == 72
    assert int.from_bytes(bitmap[22:26], "little") == 16
    assert bitmap[54:57] == bytes((0, 0, 255))


def test_requested_screen_failure_makes_cli_incomplete(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("busybar.__main__._client", lambda config, host: object())
    monkeypatch.setattr("busybar.__main__.diagnose", lambda client: {"reachable": True, "complete": True, "features": {}})
    monkeypatch.setattr("busybar.__main__.save_screen", lambda client, path: False)
    assert main(["diagnose", "--screen", str(tmp_path / "screen.bmp")]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["complete"] is False
    assert report["features"]["screen"]["available"] is False
