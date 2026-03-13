# RC Car Web Server

A Flask-based web server for RC car configuration and control, designed for embedded Linux (Yocto) targets. Provides WiFi management, over-the-air software updates, and an interactive terminal to the onboard CLI application.

## Features

- **WiFi Management** — Scan, connect, and persist WiFi credentials across software updates using NetworkManager (`nmcli`)
- **Software Updates** — Upload and apply `.swu` firmware images with real-time progress tracking via Server-Sent Events
- **Terminal** — Browser-based terminal (xterm.js) bridged over WebSocket to the onboard CLI application via TCP
- **Remote Debugging** — Optional `debugpy` support for VS Code remote attach

## Project Structure

```
src/
├── rc-config-server.py      # Flask application (routes, WebSocket, SSE)
├── connection_manager.py     # TCP client and update daemon protocol
└── templates/
    └── index.html            # Single-page frontend (xterm.js, WiFi UI, update UI)
scripts/
└── upload.sh                 # Deploy to target device via SCP
```

## Prerequisites

- Python >= 3.10
- `flask` and `flask-sock` (see `requirements.txt`)
- NetworkManager (`nmcli`) on the target system
- Ethernet interface `enP8p1s0` (the server binds to this interface only)

## Running

```bash
# Install dependencies
pip install -r requirements.txt

# Start the server
python3 src/rc-config-server.py
```

The server binds to the IPv4 address of `enP8p1s0` on the configured web port.

## Deployment to Target

```bash
# Upload source files and restart on the device
./scripts/upload.sh
```

This copies `src/*` to `root@192.168.1.10:/opt/rc-car/web-server` and starts the server.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `RC_CAR_WEB_PORT` | `5000` | Web server listen port |
| `RC_CAR_CLI_PORT` | `8001` | Onboard CLI application TCP port |
| `RC_CAR_UPDATER_PORT` | `5000` | Software updater daemon port |
| `RC_CAR_WIFI_CREDENTIALS_DIR` | `/data/wifi-credentials` | Persistent WiFi credential storage |
| `RC_CAR_WIFI_STATE_PATH` | `/data/wifi-credentials/wifi.json` | WiFi state file |
| `RC_CAR_WIFI_RESTORE_ON_BOOT` | `1` | Auto-restore WiFi on boot (`0` to disable) |
| `RC_CAR_WIFI_RESTORE_ATTEMPTS` | `15` | Max WiFi restore retry attempts |
| `RC_CAR_WIFI_RESTORE_DELAY_S` | `1.0` | Initial delay between restore attempts (seconds) |
| `RC_CAR_WIFI_RESTORE_MAX_DELAY_S` | `20.0` | Max backoff delay between attempts (seconds) |

## Architecture

```
Browser (xterm.js) ──WebSocket──► Flask ──TCP──► rc-car-nav CLI (port 8001)
Browser (UI)       ──HTTP/SSE───► Flask ──TCP──► Updater daemon (port 5000)
                                  Flask ──nmcli──► NetworkManager
```

- The web server only binds to the Ethernet interface for security
- WiFi credentials are persisted to `/data/` to survive SWUpdate image writes
- Software updates trigger an automatic reboot on completion

## Remote Debugging

The server optionally starts a `debugpy` listener on port 5678. To attach from VS Code:

1. Install `debugpy` on the target: `pip3 install debugpy`
2. Restart the web server
3. In VS Code, run the **RC Car Remote Debug** launch configuration (see `.vscode/launch.json`)

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).
