import logging
import pytest
from unittest.mock import Mock, patch
import requests
from busybar.client import BusyBarClient, DrawResult

ELEMENTS = [{"id": "0", "type": "text", "text": "hi", "font": "normal"}]


def _response(status_code: int, payload: dict | None = None) -> Mock:
    resp = Mock()
    resp.status_code = status_code
    resp.json.return_value = payload or {}
    resp.text = "error text"
    return resp


@patch("busybar.client.requests.request")
def test_draw_success(mock_request):
    mock_request.return_value = _response(200)
    client = BusyBarClient(host="192.0.2.1")
    assert client.draw("app", ELEMENTS, priority=20) == DrawResult.DRAWN
    method, url = mock_request.call_args.args
    assert method == "POST" and url == "http://192.0.2.1/api/display/draw"
    body = mock_request.call_args.kwargs["json"]
    assert body["application_name"] == "app" and body["priority"] == 20
    assert "led_notification_color" not in body


@patch("busybar.client.requests.request")
def test_draw_409_is_rejected_not_error(mock_request):
    mock_request.return_value = _response(409)
    assert BusyBarClient().draw("app", ELEMENTS) == DrawResult.REJECTED


@patch("busybar.client.requests.request")
def test_draw_unreachable(mock_request):
    mock_request.side_effect = requests.ConnectionError()
    assert BusyBarClient().draw("app", ELEMENTS) == DrawResult.UNREACHABLE


@patch("busybar.client.requests.request")
def test_clear_scopes_to_app(mock_request):
    mock_request.return_value = _response(200)
    assert BusyBarClient().clear("app") is True
    assert mock_request.call_args.kwargs["params"] == {"application_name": "app"}


@patch("busybar.client.requests.request")
def test_status_none_when_unreachable(mock_request):
    mock_request.side_effect = requests.Timeout()
    assert BusyBarClient().status() is None


@patch("busybar.client.time.time")
@patch("busybar.client.requests.request")
def test_set_busy_simple_payload(mock_request, mock_time):
    # Regression test for the confirmed-on-device bug: the device rejects a
    # flat BusySnapshot body (HTTP 400 "Failed to parse snapshot") even
    # though that's the shape /openapi.yaml's schema literally describes.
    # The firmware actually requires the snapshot nested under a
    # "snapshot" key alongside "snapshot_timestamp_ms" -- mirroring what
    # get_busy() (GET) returns -- and does NOT want busy_bar_settings on
    # this write path.
    mock_request.return_value = _response(200)
    mock_time.return_value = 1_700_000_000.5
    assert BusyBarClient().set_busy_simple(90_000) is True
    method, url = mock_request.call_args.args
    assert method == "PUT" and url == "http://10.0.4.20/api/busy/snapshot"
    body = mock_request.call_args.kwargs["json"]
    assert body == {
        "snapshot": {
            "type": "SIMPLE",
            "card_id": "00000000-0000-0000-0000-000000000000",
            "time_left_ms": 90_000,
            "is_paused": False,
        },
        "snapshot_timestamp_ms": 1_700_000_000_500,
    }
    assert "busy_bar_settings" not in body
    assert "busy_bar_settings" not in body["snapshot"]


@patch("busybar.client.requests.request")
def test_set_busy_simple_false_on_400(mock_request):
    # The pre-fix flat body reproduced a live 400 "Failed to parse
    # snapshot" on every call -- guard against regressing to that shape by
    # asserting the method's own failure handling is intact.
    mock_request.return_value = _response(400)
    assert BusyBarClient().set_busy_simple(90_000) is False


@patch("busybar.client.requests.request")
def test_draw_500_is_error_not_unreachable(mock_request):
    mock_request.return_value = _response(500)
    assert BusyBarClient().draw("app", ELEMENTS) == DrawResult.ERROR


@patch("busybar.client.requests.request")
def test_get_busy_success(mock_request):
    payload = {"type": "SIMPLE", "time_left_ms": 12_000}
    mock_request.return_value = _response(200, payload)
    assert BusyBarClient().get_busy() == payload


@patch("busybar.client.requests.request")
def test_status_success(mock_request):
    payload = {"version": "1.0.0", "uptime_ms": 300_000}
    mock_request.return_value = _response(200, payload)
    assert BusyBarClient().status() == payload


# --- play_audio (v1.5.2, calendar_countdown chirp) -------------------------------

@patch("busybar.client.requests.request")
def test_play_audio_stock_path_success(mock_request):
    mock_request.return_value = _response(200)
    assert BusyBarClient().play_audio("calendar_countdown", stock_path="shared/calendar_event_starts.wav") is True
    method, url = mock_request.call_args.args
    assert method == "POST" and url == "http://10.0.4.20/api/audio/play"
    body = mock_request.call_args.kwargs["json"]
    assert body == {"application_name": "calendar_countdown",
                    "stock_path": "shared/calendar_event_starts.wav"}

@patch("busybar.client.requests.request")
def test_play_audio_path_variant(mock_request):
    mock_request.return_value = _response(200)
    assert BusyBarClient().play_audio("app", path="data.snd") is True
    body = mock_request.call_args.kwargs["json"]
    assert body == {"application_name": "app", "path": "data.snd"}

def test_play_audio_requires_exactly_one_source():
    try:
        BusyBarClient().play_audio("app")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass

@patch("busybar.client.requests.request")
def test_play_audio_false_on_404(mock_request):
    mock_request.return_value = _response(404)
    assert BusyBarClient().play_audio("app", stock_path="shared/nope.wav") is False

@patch("busybar.client.requests.request")
def test_play_audio_false_on_unreachable(mock_request):
    mock_request.side_effect = requests.ConnectionError()
    assert BusyBarClient().play_audio("app", stock_path="shared/x.wav") is False

@patch("busybar.client.requests.request")
def test_play_audio_never_touches_volume_endpoint(mock_request):
    mock_request.return_value = _response(200)
    BusyBarClient().play_audio("app", stock_path="shared/x.wav")
    for call in mock_request.call_args_list:
        assert "/api/audio/volume" not in call.args[1]


# --- cloud transport fallback (v1.6) ----------------------------------------
# Placeholder tokens only -- never anything resembling a real credential.
FAKE_TOKEN = "test-placeholder-token-do-not-use"


def _cloud_client(host="192.0.2.1", **kwargs):
    return BusyBarClient(host=host, cloud_token=FAKE_TOKEN,
                         cloud_base_url="https://cloud.example.test/busybar", **kwargs)


@patch("busybar.client.requests.request")
def test_auto_falls_back_to_cloud_on_local_failure(mock_request):
    mock_request.side_effect = [requests.ConnectionError(), _response(200)]
    client = _cloud_client()
    assert client.draw("app", ELEMENTS) == DrawResult.DRAWN
    assert mock_request.call_count == 2
    local_call, cloud_call = mock_request.call_args_list
    assert local_call.args == ("POST", "http://192.0.2.1/api/display/draw")
    assert cloud_call.args == ("POST", "https://cloud.example.test/busybar/display/draw")
    assert client.active_transport == "cloud"


@patch("busybar.client.requests.request")
def test_no_fallback_when_cloud_unconfigured(mock_request):
    mock_request.side_effect = requests.ConnectionError()
    client = BusyBarClient(host="192.0.2.1")  # cloud_token defaults to ""
    assert client.draw("app", ELEMENTS) == DrawResult.UNREACHABLE
    assert mock_request.call_count == 1  # only local was ever attempted
    assert client.active_transport == "local"  # never transitions without cloud configured


@patch("busybar.client.requests.request")
def test_unreachable_when_both_transports_fail(mock_request):
    mock_request.side_effect = requests.ConnectionError()
    client = _cloud_client()
    assert client.draw("app", ELEMENTS) == DrawResult.UNREACHABLE
    assert mock_request.call_count == 2  # local AND cloud were both attempted
    assert client.active_transport == "cloud"


@patch("busybar.client.requests.request")
def test_forced_local_never_attempts_cloud_even_on_failure(mock_request):
    mock_request.side_effect = requests.ConnectionError()
    client = _cloud_client(transport="local")
    assert client.draw("app", ELEMENTS) == DrawResult.UNREACHABLE
    assert mock_request.call_count == 1
    assert mock_request.call_args.args[1].startswith("http://192.0.2.1")


@patch("busybar.client.requests.request")
def test_forced_cloud_always_goes_straight_to_cloud(mock_request):
    mock_request.return_value = _response(200)
    client = _cloud_client(transport="cloud")
    assert client.draw("app", ELEMENTS) == DrawResult.DRAWN
    assert mock_request.call_count == 1
    method, url = mock_request.call_args.args
    assert method == "POST" and url == "https://cloud.example.test/busybar/display/draw"
    assert client.active_transport == "cloud"


@patch("busybar.client.requests.request")
def test_cloud_request_shape_bearer_header_and_base_url(mock_request):
    mock_request.side_effect = [requests.ConnectionError(), _response(200)]
    client = _cloud_client()
    client.draw("app", ELEMENTS, priority=30)
    cloud_call = mock_request.call_args_list[1]
    assert cloud_call.args == ("POST", "https://cloud.example.test/busybar/display/draw")
    assert cloud_call.kwargs["headers"] == {"Authorization": f"Bearer {FAKE_TOKEN}"}
    assert cloud_call.kwargs["timeout"] == (5, 15)
    assert cloud_call.kwargs["json"]["application_name"] == "app"


@patch("busybar.client.requests.request")
def test_cloud_path_mapping_strips_api_prefix(mock_request):
    mock_request.side_effect = [requests.ConnectionError(), _response(200, {})]
    client = _cloud_client()
    client.status()
    cloud_call = mock_request.call_args_list[1]
    assert cloud_call.args == ("GET", "https://cloud.example.test/busybar/status")


@patch("busybar.client.time.monotonic")
@patch("busybar.client.requests.request")
def test_degraded_client_skips_local_within_retry_window(mock_request, mock_time):
    # First call degrades to cloud at t=0. A second call at t=30 (well
    # inside LOCAL_RETRY_SECONDS=60) must skip the local attempt entirely
    # and go straight to cloud -- only one requests.request call for the
    # second draw, and it must be the cloud URL.
    mock_time.return_value = 0.0
    mock_request.side_effect = [requests.ConnectionError(), _response(200)]
    client = _cloud_client()
    assert client.draw("app", ELEMENTS) == DrawResult.DRAWN
    assert mock_request.call_count == 2  # local (failed) + cloud (succeeded)

    mock_time.return_value = 30.0
    mock_request.reset_mock()
    mock_request.side_effect = None
    mock_request.return_value = _response(200)
    assert client.draw("app", ELEMENTS) == DrawResult.DRAWN
    assert mock_request.call_count == 1  # local skipped -- straight to cloud
    method, url = mock_request.call_args.args
    assert url == "https://cloud.example.test/busybar/display/draw"


@patch("busybar.client.time.monotonic")
@patch("busybar.client.requests.request")
def test_degraded_client_probes_local_after_retry_window_elapses(mock_request, mock_time):
    # Degrade at t=0, then let LOCAL_RETRY_SECONDS (60) elapse: the next
    # call must try local FIRST again (the recovery probe), and recover
    # to active_transport == "local" when it succeeds.
    mock_time.return_value = 0.0
    mock_request.side_effect = [requests.ConnectionError(), _response(200)]
    client = _cloud_client()
    client.draw("app", ELEMENTS)
    assert client.active_transport == "cloud"

    mock_time.return_value = 61.0  # LOCAL_RETRY_SECONDS elapsed
    mock_request.reset_mock()
    mock_request.side_effect = None
    mock_request.return_value = _response(200)  # local now recovered
    assert client.draw("app", ELEMENTS) == DrawResult.DRAWN
    assert mock_request.call_count == 1  # local probe succeeded, no cloud needed
    method, url = mock_request.call_args.args
    assert url == "http://192.0.2.1/api/display/draw"
    assert client.active_transport == "local"


@patch("busybar.client.time.monotonic")
@patch("busybar.client.requests.request")
def test_degraded_client_reprobes_local_and_stays_cloud_if_still_down(mock_request, mock_time):
    # Recovery probe fires after the window elapses but local is STILL
    # down: client must fall back to cloud again for that same request
    # (not return UNREACHABLE just because the probe failed) and reset
    # the degraded timer so the next probe is another window out.
    mock_time.return_value = 0.0
    mock_request.side_effect = [requests.ConnectionError(), _response(200)]
    client = _cloud_client()
    client.draw("app", ELEMENTS)

    mock_time.return_value = 61.0
    mock_request.side_effect = [requests.ConnectionError(), _response(200)]  # local still down, cloud up
    assert client.draw("app", ELEMENTS) == DrawResult.DRAWN
    assert client.active_transport == "cloud"
    assert client._degraded_since == 61.0  # timer reset to the failed probe's time


@patch("busybar.client.requests.request")
def test_no_recovery_probe_before_window_when_never_degraded(mock_request):
    # A freshly-constructed "auto" client (active_transport == "local")
    # always tries local first, regardless of LOCAL_RETRY_SECONDS -- the
    # probe-skip logic only applies once actually degraded.
    mock_request.return_value = _response(200)
    client = _cloud_client()
    assert client.active_transport == "local"
    assert client.draw("app", ELEMENTS) == DrawResult.DRAWN
    assert mock_request.call_count == 1
    assert mock_request.call_args.args[1] == "http://192.0.2.1/api/display/draw"


# --- upload_asset (nyan_filler, local-only) ---------------------------------

@patch("busybar.client.requests.request")
def test_upload_asset_success(mock_request):
    mock_request.return_value = _response(200)
    data = b"\x00\x01\x02binarydata"
    assert BusyBarClient().upload_asset("nyan_filler", "nyan_72x16.anim", data) is True
    method, url = mock_request.call_args.args
    assert method == "POST"
    assert url == "http://10.0.4.20/api/assets/upload"
    assert mock_request.call_args.kwargs["params"] == {
        "application_name": "nyan_filler", "file": "nyan_72x16.anim"
    }
    assert mock_request.call_args.kwargs["headers"] == {"Content-Type": "application/octet-stream"}
    assert mock_request.call_args.kwargs["data"] == data


@patch("busybar.client.requests.request")
def test_upload_asset_false_on_non_200(mock_request):
    mock_request.return_value = _response(500)
    assert BusyBarClient().upload_asset("nyan_filler", "nyan_72x16.anim", b"x") is False


@patch("busybar.client.requests.request")
def test_upload_asset_false_when_unreachable(mock_request):
    mock_request.side_effect = requests.ConnectionError()
    assert BusyBarClient().upload_asset("nyan_filler", "nyan_72x16.anim", b"x") is False


# --- token never logged (v1.6 security requirement) -------------------------

@patch("busybar.client.time.monotonic")
@patch("busybar.client.requests.request")
def test_cloud_token_never_appears_in_log_output(mock_request, mock_time, caplog):
    caplog.set_level(logging.DEBUG, logger="busybar.client")
    mock_time.return_value = 0.0
    mock_request.side_effect = [requests.ConnectionError(), _response(200)]
    client = _cloud_client()
    client.draw("app", ELEMENTS)  # local fails, cloud succeeds -- degrades to cloud

    # Still well within LOCAL_RETRY_SECONDS (60): per the recovery-probe
    # design (see test_degraded_client_skips_local_within_retry_window),
    # this second call skips the local attempt entirely and goes straight
    # to cloud -- only ONE requests.request call happens here, not two.
    mock_time.return_value = 30.0
    mock_request.side_effect = [requests.ConnectionError()]  # the sole (cloud) attempt fails
    assert client.draw("app", ELEMENTS) == DrawResult.UNREACHABLE

    for record in caplog.records:
        assert FAKE_TOKEN not in record.getMessage()
        assert FAKE_TOKEN not in str(record.args)


# --- fallback-only-on-RequestException contract (final-gate review) --------

@patch("busybar.client.requests.request")
def test_local_http_500_is_error_and_does_not_trigger_cloud_fallback(mock_request):
    # Highest-risk semantic in the whole feature: a local HTTP error
    # response (no exception -- the device is reachable, it just returned
    # a bad status) must NOT be treated as "local is down." Locks in that
    # fallback triggers ONLY on requests.RequestException, never on a
    # non-2xx/409 response the device actually returned.
    local_500 = _response(500)
    mock_request.return_value = local_500
    client = _cloud_client()
    assert client.draw("app", ELEMENTS) == DrawResult.ERROR
    assert mock_request.call_count == 1  # cloud was never attempted
    assert mock_request.call_args.args == ("POST", "http://192.0.2.1/api/display/draw")
    assert client.active_transport == "local"  # never degraded


# --- cloud 409 -> REJECTED (final-gate review) -------------------------------

@patch("busybar.client.requests.request")
def test_cloud_409_is_rejected_not_error_forced_cloud(mock_request):
    mock_request.return_value = _response(409)
    client = _cloud_client(transport="cloud")
    assert client.draw("app", ELEMENTS) == DrawResult.REJECTED

@patch("busybar.client.requests.request")
def test_cloud_409_is_rejected_not_error_while_degraded(mock_request):
    mock_request.side_effect = [requests.ConnectionError(), _response(200)]
    client = _cloud_client()
    client.draw("app", ELEMENTS)  # degrades to cloud
    assert client.active_transport == "cloud"

    mock_request.side_effect = [_response(409)]  # degraded -- skips local, straight to cloud
    assert client.draw("app", ELEMENTS) == DrawResult.REJECTED


# --- transport ValueError guard (final-gate review) --------------------------

def test_invalid_transport_value_raises_value_error():
    try:
        BusyBarClient(transport="carrier-pigeon")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "carrier-pigeon" in str(exc)


# --- firmware 1.2.3 local transport boundaries --------------------------------

@patch("busybar.client.requests.request")
def test_local_token_never_reaches_cloud_and_redirects_are_disabled(mock_request):
    mock_request.side_effect = [requests.ConnectionError(), _response(200)]
    client = BusyBarClient(host="192.0.2.1", local_token="local-only-token",
                            cloud_token=FAKE_TOKEN, cloud_base_url="https://cloud.example.test/busybar")
    assert client.draw("app", ELEMENTS) == DrawResult.DRAWN
    local_call, cloud_call = mock_request.call_args_list
    assert local_call.kwargs["headers"] == {"X-API-Token": "local-only-token"}
    assert cloud_call.kwargs["headers"] == {"Authorization": f"Bearer {FAKE_TOKEN}"}
    assert local_call.kwargs["allow_redirects"] is False
    assert cloud_call.kwargs["allow_redirects"] is False


@patch("busybar.client.requests.request")
def test_unsafe_audio_is_not_replayed_after_read_or_connection_uncertainty(mock_request):
    client = _cloud_client()
    mock_request.side_effect = requests.ReadTimeout()
    assert client.play_audio("app", stock_path="shared/x.snd") is False
    assert mock_request.call_count == 1
    mock_request.reset_mock()
    mock_request.side_effect = requests.ConnectionError()
    assert client.set_busy_simple(1_000) is False
    assert mock_request.call_count == 1


@patch("busybar.client.requests.request")
def test_unsafe_audio_may_fail_over_after_definite_connect_timeout(mock_request):
    mock_request.side_effect = [requests.ConnectTimeout(), _response(200)]
    assert _cloud_client().play_audio("app", stock_path="shared/x.snd") is True
    assert mock_request.call_count == 2
    assert mock_request.call_args_list[1].args[1].startswith("https://cloud.example.test/")


@patch("busybar.client.requests.request")
def test_auth_and_conflict_responses_never_trigger_failover(mock_request):
    for status in (401, 403, 409):
        mock_request.reset_mock()
        mock_request.return_value = _response(status)
        assert _cloud_client().draw("app", ELEMENTS) in (DrawResult.ERROR, DrawResult.REJECTED)
        assert mock_request.call_count == 1


@patch("busybar.client.requests.request")
def test_configured_fallback_host_is_tried_before_cloud(mock_request):
    mock_request.side_effect = [requests.ConnectionError(), _response(200)]
    client = _cloud_client(fallback_hosts=["192.0.2.2"])
    assert client.status() == {}
    assert [call.args[1] for call in mock_request.call_args_list] == [
        "http://192.0.2.1/api/status", "http://192.0.2.2/api/status"
    ]


@patch("busybar.client.time.monotonic")
@patch("busybar.client.requests.request")
def test_capabilities_are_local_only_and_cached(mock_request, mock_time):
    mock_time.return_value = 0.0
    mock_request.return_value = _response(200, {"api_semver": "27.5.0"})
    client = BusyBarClient()
    assert client.refresh_capabilities() is True
    assert client.supports_display_v2 is True
    mock_time.return_value = 30.0
    assert client.refresh_capabilities() is True
    assert mock_request.call_count == 1
    cloud = _cloud_client(transport="cloud")
    assert cloud.refresh_capabilities() is False
    assert cloud.supports_display_v2 is False


@patch("busybar.client.requests.request")
def test_read_only_diagnostics_support_documented_json_and_screen_paths(mock_request):
    response = _response(200, {"version": "ok"})
    response.content = b"screen-bytes"
    mock_request.return_value = response
    client = BusyBarClient(local_token="local-token")
    assert client.get_json("/api/status/firmware", local_only=True) == {"version": "ok"}
    assert client.get_bytes("/api/screen?display=0") == b"screen-bytes"
    assert all(call.kwargs["headers"] == {"X-API-Token": "local-token"} for call in mock_request.call_args_list)


@patch("busybar.client.requests.request")
def test_remove_elements_uses_query_owner_and_never_duplicates_it_in_json(mock_request):
    mock_request.return_value = _response(200)
    client = BusyBarClient()
    client.supports_display_v2 = True
    assert client.remove_elements("app", ["obsolete", "other"])
    assert mock_request.call_args.args == ("DELETE", "http://10.0.4.20/api/display/draw")
    assert mock_request.call_args.kwargs["params"] == {"application_name": "app"}
    assert mock_request.call_args.kwargs["json"] == {"element_ids": ["obsolete", "other"]}
    mock_request.reset_mock()
    assert BusyBarClient().remove_elements("app", []) is True
    assert mock_request.call_count == 0


@patch("busybar.client.requests.request")
def test_remove_elements_requires_verified_current_local_capability(mock_request):
    client = BusyBarClient()
    assert client.remove_elements("app", ["obsolete"]) is False
    assert mock_request.call_count == 0


@patch("busybar.client.requests.request")
def test_subdirectory_asset_upload_and_traversal_rejection(mock_request):
    mock_request.return_value = _response(200)
    client = BusyBarClient()
    assert client.upload_asset("app", "icons/ok.xpm", b"x") is True
    assert mock_request.call_args.kwargs["params"]["file"] == "icons/ok.xpm"
    mock_request.reset_mock()
    assert client.upload_asset("app", "../nope.xpm", b"x") is False
    assert client.upload_asset("app", "/nope.xpm", b"x") is False
    assert mock_request.call_count == 0


@patch("busybar.client.requests.request")
def test_bitmaps_and_z_index_degrade_for_legacy_and_cloud(mock_request):
    mixed = [
        {"id": "icon", "type": "xpmbitmap", "xpmbitmap": "xpm"},
        {"id": "text", "type": "text", "text": "ok", "z_index": 99},
    ]
    mock_request.return_value = _response(200)
    assert BusyBarClient().draw("app", mixed) == DrawResult.DRAWN
    legacy = mock_request.call_args.kwargs["json"]["elements"]
    assert legacy == [{"id": "text", "type": "text", "text": "ok"}]
    mock_request.reset_mock()
    mock_request.side_effect = [requests.ConnectionError(), _response(200)]
    client = _cloud_client()
    client.supports_display_v2 = True
    assert client.draw("app", mixed) == DrawResult.DRAWN
    local, cloud = mock_request.call_args_list
    assert local.kwargs["json"]["elements"][0]["z_index"] == 0
    assert cloud.kwargs["json"]["elements"] == [{"id": "text", "type": "text", "text": "ok"}]


@patch("busybar.client.requests.request")
def test_all_bitmap_payload_is_rejected_before_an_empty_legacy_or_cloud_draw(mock_request):
    assert BusyBarClient().draw("app", [{"id": "icon", "type": "xpmbitmap", "xpmbitmap": "xpm"}]) == DrawResult.ERROR
    assert mock_request.call_count == 0


@patch("busybar.client.requests.request")
def test_malformed_snapshot_json_is_unknown(mock_request):
    response = _response(200)
    response.json.side_effect = ValueError("bad json")
    mock_request.return_value = response
    assert BusyBarClient().get_busy() is None


@patch("busybar.client.requests.request")
def test_bitmap_only_is_supported_locally_without_empty_cloud_fallback(mock_request):
    client = _cloud_client()
    client.supports_display_v2 = True
    bitmap = [{"id": "icon", "type": "xpmbitmap", "data": "! XPM2\n1 1 1 1\nX c #FFFFFF\nX\n", "timeout": 5}]
    mock_request.return_value = _response(200)
    assert client.draw("app", bitmap) == DrawResult.DRAWN
    assert mock_request.call_args.kwargs["json"]["elements"][0]["type"] == "xpmbitmap"
    mock_request.reset_mock()
    mock_request.side_effect = requests.ReadTimeout()
    assert client.draw("app", bitmap) == DrawResult.UNREACHABLE
    assert mock_request.call_count == 1
    assert mock_request.call_args.args[1].startswith("http://192.0.2.1/")


def test_discovered_routes_cannot_expand_total_route_budget():
    client = BusyBarClient(fallback_hosts=["192.0.2.1", "192.0.2.2", "192.0.2.3"])
    client._discovered_hosts = ["192.0.2.4", "192.0.2.5"]
    assert len(client._all_local_hosts()) == 4


@patch("busybar.client.requests.request")
def test_discovery_failure_preserves_explicit_host(mock_request):
    from busybar.discovery import DiscoveryUnavailable
    mock_request.return_value = _response(200, {"api_semver": "27.5.0"})
    with patch("busybar.discovery.discover_devices", side_effect=DiscoveryUnavailable("scan unavailable")):
        client = BusyBarClient(host="192.0.2.8", discover=True, device_id="aabbccddeeff")
        assert client.get_json("/api/version") == {"api_semver": "27.5.0"}
    assert mock_request.call_args.args[1] == "http://192.0.2.8/api/version"


@patch("busybar.client.requests.request")
def test_empty_owner_cannot_turn_cleanup_into_global_delete(mock_request):
    client = BusyBarClient()
    client.supports_display_v2 = True
    with pytest.raises(ValueError):
        client.clear("")
    with pytest.raises(ValueError):
        client.remove_elements("", ["old"])
    mock_request.assert_not_called()


@patch("busybar.client.requests.request")
@pytest.mark.parametrize("audio", [False, True])
def test_newly_discovered_address_is_tried_in_current_operation(mock_request, audio):
    from busybar.discovery import DiscoveredDevice
    client = BusyBarClient()
    client.discover = True
    client.device_id = "aabbccddeeff"
    record = DiscoveredDevice(client.device_id, "busybar-aabbccddeeff._http._tcp.local.", ("192.0.2.9",), 80)
    # Only a definite connection timeout permits retrying the audio variant.
    mock_request.side_effect = [requests.ConnectTimeout(), _response(200, {"api_semver": "27.5.0"})]
    with patch("busybar.discovery.discover_devices", return_value=[record]) as scan:
        result = client.play_audio("app", stock_path="sound.snd") if audio else client.get_json("/api/version")
    assert result
    assert mock_request.call_count == 2
    assert mock_request.call_args.args[1].startswith("http://192.0.2.9/")
    scan.assert_called_once()


@patch("busybar.client.requests.request")
def test_discovery_refresh_cannot_exceed_four_attempts_or_replay_uncertain_audio(mock_request):
    client = BusyBarClient(fallback_hosts=["192.0.2.1", "192.0.2.2", "192.0.2.3"])
    mock_request.side_effect = requests.ConnectTimeout()
    with patch.object(client, "_refresh_discovery") as scan:
        assert client.get_json("/api/version") is None
    assert mock_request.call_count == 4
    scan.assert_called_once()
    mock_request.reset_mock()
    mock_request.side_effect = requests.ReadTimeout()
    with patch.object(client, "_refresh_discovery") as scan:
        assert client.play_audio("app", stock_path="sound.snd") is False
    assert mock_request.call_count == 1
    scan.assert_not_called()
