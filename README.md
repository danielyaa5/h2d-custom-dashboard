# H2D Custom Dashboard

A local web dashboard and MQTT status logger for the **Bambu Lab H2D**, with optional Smart Life exhaust-fan control. See printer progress, temperatures, filament types and fan status from a browser on your home network.

![H2D dashboard with simulated printer data](docs/screenshots/dashboard.png)

## Features

- Live chamber, bed and both nozzle temperatures, plus print progress, layers and remaining time.
- Daily CSV logs with automatic retention of the most recent 31 days.
- Optional Smart Life fan automation for ABS, ASA and PETG, including warm-up handling and a post-print purge.
- Manual fan speed controls, timed overrides and persistent exhaust settings.
- Responsive layout and a simple browser-tab icon.
- Optional macOS Dock launcher and a separate Windows status logger.

<img src="docs/screenshots/mobile.png" width="300" alt="H2D dashboard on a phone, using simulated data">

Both screenshots use the built-in demo, with fictional job names and temperatures.

## Try the demo

Requires Python 3.9 or later. The demo needs no printer, credentials or extra packages, and never connects to hardware:

```sh
python3 scripts/demo.py
```

Open [http://localhost:8792](http://localhost:8792). Demo controls change simulated state only; nothing is sent to a printer or fan.

## Connect your printer

The printer must be reachable over its local MQTT TLS port (8883), with a serial number and LAN access code available in its settings. This has been used with an H2D while cloud mode remained enabled; behavior may depend on firmware and network configuration.

```sh
cp config.example.json air/config.json
# Edit air/config.json with your printer hostname, serial and LAN access code.
python3 air/h2d_logger.py
```

Open [http://localhost:8791](http://localhost:8791) on the host, or the host's address and port 8791 from another device on your LAN. Keep the host awake while logging. Stop the foreground process with Control-C.

The logger subscribes to status reports and requests a full status snapshot after connecting. It does not send print jobs, move the printer or change its temperatures. It reconnects after a dropped connection. Logs are written to `air/logs/`; the server exposes dated CSV downloads, not the project folder or configuration files.

**Access:** the dashboard listens on all network interfaces without a login, and its optional fan controls can operate a real fan. Use it on a trusted network. Do not expose port 8791 to the public internet. MQTT uses TLS but accepts the printer's self-signed certificate without certificate verification.

### Optional Smart Life fan

The logger and dashboard need only the Python standard library. Smart Life support uses Tuya's SDK in a separate environment:

```sh
python3 -m venv air/fan-venv
air/fan-venv/bin/pip install tuya-device-sharing-sdk cryptography
air/fan-venv/bin/python air/fan.py login YOUR_SMART_LIFE_USER_CODE
air/fan-venv/bin/python air/fan.py devices
air/fan-venv/bin/python air/fan.py use YOUR_FAN_DEVICE_ID
```

Find the user code in Smart Life under **Me → Settings → Account and Security → User Code**. Follow the QR sign-in instructions. Credentials stay in ignored configuration files; never commit them. Restart the logger after setting up the fan.

By default, the fan runs at speed 5 for ABS, ASA and PETG once warm-up finishes. It runs at speed 10 during the post-print purge for 15 minutes. The dashboard can change these settings and apply manual or timed overrides. The logger preserves the tracked fan job across restarts and retries failed fan commands.

The printer-fan percentages shown in the settings panel are stored settings. This logger does **not** send fan-control commands to the printer or install end-of-print G-code; configure printer-side behavior separately if needed.

### Background service on macOS

Copy `air/h2d-status-logger.plist.example` to `~/Library/LaunchAgents/local.h2d-status-logger.plist`. Replace the Python, source and log paths with absolute paths on your machine, then load it:

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.h2d-status-logger.plist
```

The example starts at login and restarts on failure. It runs only while that user is logged in. To stop it:

```sh
launchctl bootout gui/$(id -u)/local.h2d-status-logger
```

### Optional macOS Dock launcher

Edit `dashboardUrl` in `dock-launcher/launcher.applescript` if the server runs on another machine, then run:

```sh
bash dock-launcher/build.sh
```

This compiles and installs `/Applications/H2D Dashboard.app`. The launcher activates an existing matching Google Chrome tab, or opens the dashboard. macOS may ask for permission to control Chrome and System Events. The launcher uses the artwork under `assets/`; its build folder is ignored by Git.

### Windows logger

The Windows logger is separate from the Python web dashboard. Copy `config.example.json` to `config.json` in the project root, fill in the printer details, then run in Windows PowerShell:

```powershell
.\h2d-log.ps1 -Minutes 120 -EverySeconds 10
```

It writes `status-log.csv` and `status-latest.json` locally. These runtime files are ignored by Git. Windows logging does not include the web dashboard or Smart Life control.

## Verification

```sh
python3 air/test_h2d_logger.py
# Optional, slower mutation checks; requires zsh and sed:
zsh air/mutation_check.sh
```

The 39 tests use temporary files and a fake TLS printer on localhost. They cover MQTT packet handling, reconnects, partial status updates, retention, HTTP routes, settings and fan automation. The real printer credentials and devices are not used. OpenSSL must be available for the fake printer certificate.

## Project layout

| Path | Purpose |
| --- | --- |
| `air/h2d_logger.py` | MQTT logger, web dashboard and fan coordination |
| `air/fan.py` | Optional Smart Life adapter |
| `scripts/demo.py` | Hardware-free demo for previews and screenshots |
| `dock-launcher/` | macOS launcher source and build script |
| `assets/` | Dock artwork and tab icons |
| `H2DStatus.cs`, `h2d-log.ps1` | Windows logger |

Configuration, access codes, Smart Life tokens, local logs and build products are excluded from the public source.
