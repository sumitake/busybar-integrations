# Finite firmware JavaScript example

`main.js` is a short health check for firmware 1.2.3. It polls the
credential-free `GET /api/version` endpoint for 30 seconds, allows only one
request at a time, and increments one `localStorage` run counter at startup.
It does not draw, create a manifest, install an app, or configure persistent
autostart. The example uses `app.busy.integrations_demo`, an application ID
accepted by the firmware validator.

The firmware's raw user script path is:

```text
/ext/user_assets/app.busy.integrations_demo/scripts/main.js
```

From the repository root, upload through the existing client:

```python
from pathlib import Path
from busybar.client import BusyBarClient

client = BusyBarClient(host="10.0.4.20", transport="local")
assert client.upload_asset(
    "app.busy.integrations_demo", "scripts/main.js",
    Path("examples/javascript/main.js").read_bytes(),
)
```

Connect to the stock CLI over **USB Ethernet TCP port 23**, not a serial
modem node. The firmware's normal USB function is a network interface:

```bash
nc 10.0.4.20 23
```

At the `>: ` prompt, run the script (the file argument is the raw device path):

```text
js -i app.busy.integrations_demo /ext/user_assets/app.busy.integrations_demo/scripts/main.js
```

There is no browser-style `AbortController` in the embedded runtime, and a
request that is already in progress cannot be hard-cancelled by this script.
The stopped flag prevents late responses from taking further action after the
30-second window. If an explicit operator stop is needed, the firmware CLI is:

```text
js -k
```

`js -k` aborts **all** running JavaScript scripts on the device. Use it only
as a manual, explicit stop; this example never invokes it automatically.

The CLI is a local development surface; this example uses the USB interface
and does not enable Wi-Fi CLI access. Closing the terminal is not a script
stop command. The script clears its own polling timer at 30 seconds.
