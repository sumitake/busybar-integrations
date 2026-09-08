# Home Assistant example

`busybar.yaml` uses Home Assistant's built-in REST sensor and `rest_command`
facilities. Replace `BUSY_BAR_IP` with your device address. Add the local device token to `secrets.yaml` as
`busybar_api_token`; the YAML sends it in the firmware's `X-API-Token` header.
The cloud relay uses a different `Authorization: Bearer` contract and is not
used here.

The sensor preserves the firmware 1.2.3 BUSY snapshot shape:

```json
{
  "snapshot": {
    "type": "SIMPLE",
    "card_id": "...",
    "time_left_ms": 90000,
    "is_paused": false,
    "busy_bar_settings": {}
  },
  "snapshot_timestamp_ms": 1700000000000
}
```

This is the nested snapshot contract verified on firmware 1.2.3; nesting is
not claimed as a new 1.2.3 feature. Unknown responses should not be converted
to an idle timer by guessing alternative field paths.

Malformed snapshots and snapshots without the expected fields become
`unknown` or unavailable. They are never rendered as `NOT_STARTED` or an
idle state. The commented automation is a light cue on a state change; it
does not start a timer. Notice TTLs are clamped to 1-60 seconds so a bad
automation value cannot create a permanent canvas element. The example does not claim that an official Home
Assistant core BUSY Bar integration exists.

If local authentication is disabled, remove the `X-API-Token` header lines
instead of creating an unnecessary token. Merge the YAML into your existing
REST/REST-command configuration, or include it as an HA package; do not add
duplicate top-level keys. Validate the configuration in Home Assistant before
reloading it. References: [REST sensors](https://www.home-assistant.io/integrations/sensor.rest/)
and [REST commands](https://www.home-assistant.io/integrations/rest_command/).
