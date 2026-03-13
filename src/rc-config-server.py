from flask import Flask, render_template, request, jsonify
from flask_sock import Sock
import subprocess
import sys
import socket
import fcntl
import struct
import os
import time
import uuid
import threading
import logging
import json

try:
    import debugpy
    debugpy.listen(("0.0.0.0", 5678))
    logging.getLogger().info("debugpy listening on port 5678")
except ImportError:
    pass

from connection_manager import UpdatePipe, TcpClient
import time


WEB_UI_VERSION = "1.00.0005"

WEB_PORT = int(os.environ.get("RC_CAR_WEB_PORT", "5000"))
CLI_PORT = int(os.environ.get("RC_CAR_CLI_PORT", "8001"))

# Persistent Wi-Fi credentials/state storage (survives swupdate via /data)
WIFI_CREDENTIALS_DIR = os.environ.get("RC_CAR_WIFI_CREDENTIALS_DIR", "/data/wifi-credentials")
WIFI_CREDENTIALS_PATH = os.path.join(WIFI_CREDENTIALS_DIR, "credentials.json")

# Persisted Wi-Fi state (last configured SSID, etc.)
WIFI_STATE_PATH = os.environ.get(
    "RC_CAR_WIFI_STATE_PATH",
    os.path.join(WIFI_CREDENTIALS_DIR, "wifi.json"),
)
LEGACY_WIFI_STATE_PATH = "/var/lib/rc-car-webserver/wifi.json"


# Defines
UPLOAD_DIR = "/home/images"
updater = UpdatePipe(web_port=WEB_PORT)
tcp_client = TcpClient(port=CLI_PORT, host="127.0.0.1", timeout=5)

status_lock  = threading.Lock()
thread_can_run : bool = False
progress : float = 0.0
thread = None
save_path : str = ""

# Per-job state storage: map job_id -> state dict
job_states: dict = {}
# Per-job stop events and threads
job_events: dict = {}
job_threads: dict = {}

app = Flask(__name__)
sock = Sock(app)

UPDATE_FINISHED = 3


def _load_wifi_state() -> dict:
    try:
        with open(WIFI_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        # Backward-compat: older images stored Wi-Fi state outside /data
        try:
            with open(LEGACY_WIFI_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            state = data if isinstance(data, dict) else {}
            if state:
                _save_wifi_state(state)
            return state
        except FileNotFoundError:
            return {}
        except Exception:
            logging.exception("Failed to load legacy Wi-Fi state from %s", LEGACY_WIFI_STATE_PATH)
            return {}
    except Exception:
        logging.exception("Failed to load Wi-Fi state from %s", WIFI_STATE_PATH)
        return {}


def _save_wifi_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(WIFI_STATE_PATH), exist_ok=True)
        tmp = WIFI_STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, WIFI_STATE_PATH)
    except Exception:
        logging.exception("Failed to save Wi-Fi state to %s", WIFI_STATE_PATH)

def _ensure_wifi_credentials_dir() -> None:
    try:
        os.makedirs(WIFI_CREDENTIALS_DIR, mode=0o700, exist_ok=True)
        try:
            os.chmod(WIFI_CREDENTIALS_DIR, 0o700)
        except Exception:
            pass
    except Exception:
        logging.exception("Failed to ensure Wi-Fi credentials dir exists: %s", WIFI_CREDENTIALS_DIR)


def _atomic_write_json(path: str, data: dict, file_mode: int | None = None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    if file_mode is not None:
        try:
            os.chmod(tmp, file_mode)
        except Exception:
            pass
    os.replace(tmp, path)
    if file_mode is not None:
        try:
            os.chmod(path, file_mode)
        except Exception:
            pass


def _snapshot_wifi_credentials() -> dict:
    """
    Best-effort snapshot of Wi-Fi info to survive swupdate.
    Stores SSID/password when available (plain text on disk).
    """
    status = _get_wifi_status()
    device, connection = status.get("device"), status.get("connection")

    ssid = status.get("ssid") or status.get("saved_ssid")
    password = None

    if connection:
        try:
            out = subprocess.check_output(
                [
                    "nmcli",
                    "--show-secrets",
                    "-g",
                    "802-11-wireless.ssid,802-11-wireless-security.psk",
                    "con",
                    "show",
                    connection,
                ],
                text=True,
            )
            lines = [ln.strip() for ln in out.splitlines() if ln.strip() != ""]
            if lines:
                ssid = lines[0] or ssid
            if len(lines) >= 2:
                password = lines[1] or None
        except Exception:
            pass

    return {
        "format_version": 1,
        "updated": time.time(),
        "ssid": ssid,
        "password": password,
        "device": device,
        "connection": connection,
    }


def _persist_wifi_credentials(ssid: str | None, password: str | None, source: str) -> None:
    _ensure_wifi_credentials_dir()
    payload = _snapshot_wifi_credentials()
    if ssid:
        payload["ssid"] = ssid
    if password:
        payload["password"] = password
    payload["source"] = source

    try:
        _atomic_write_json(WIFI_CREDENTIALS_PATH, payload, file_mode=0o600)
    except Exception:
        logging.exception("Failed to persist Wi-Fi credentials to %s", WIFI_CREDENTIALS_PATH)


def _persist_wifi_credentials_snapshot(source: str) -> None:
    _ensure_wifi_credentials_dir()
    payload = _snapshot_wifi_credentials()
    payload["source"] = source
    try:
        _atomic_write_json(WIFI_CREDENTIALS_PATH, payload, file_mode=0o600)
    except Exception:
        logging.exception("Failed to persist Wi-Fi credentials snapshot to %s", WIFI_CREDENTIALS_PATH)


def _load_wifi_credentials() -> dict:
    try:
        with open(WIFI_CREDENTIALS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logging.exception("Failed to load Wi-Fi credentials from %s", WIFI_CREDENTIALS_PATH)
        return {}


def _get_wifi_device() -> str | None:
    try:
        out = subprocess.check_output(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "dev", "status"],
            text=True,
        )
        for line in out.splitlines():
            if not line:
                continue
            parts = _split_nmcli_t_line(line)
            if len(parts) < 2:
                continue
            device, dev_type = parts[0], parts[1]
            if dev_type == "wifi":
                return device or None
    except Exception:
        return None
    return None


def _restore_wifi_if_needed() -> bool:
    """
    If not currently connected, try to restore Wi-Fi using persisted /data credentials.
    Returns True if connected (either already or after restore attempt).
    """
    status = _get_wifi_status()
    if status.get("connected"):
        return True

    creds = _load_wifi_credentials()
    saved = _load_wifi_state()

    ssid = creds.get("ssid") or saved.get("ssid")
    password = creds.get("password")
    connection = creds.get("connection")
    device = creds.get("device") or _get_wifi_device()

    if not ssid:
        return False

    subprocess.run(["nmcli", "radio", "wifi", "on"], check=False)

    # First try bringing up an existing connection profile (fast path).
    if connection:
        cmd = ["nmcli", "con", "up", "id", str(connection)]
        if device:
            cmd += ["ifname", str(device)]
        res = subprocess.run(cmd, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode == 0:
            time.sleep(1.0)
            return bool(_get_wifi_status().get("connected"))
        logging.warning(
            "Wi-Fi restore: failed to bring up connection '%s' (rc=%s): %s",
            connection,
            res.returncode,
            (res.stderr or res.stdout or "").strip(),
        )

    # Otherwise connect by SSID (will create/refresh a connection profile).
    cmd = ["nmcli", "dev", "wifi", "connect", str(ssid)]
    if password:
        cmd += ["password", str(password)]
    if device:
        cmd += ["ifname", str(device)]
    res = subprocess.run(cmd, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0:
        logging.warning(
            "Wi-Fi restore: nmcli connect failed for ssid '%s' (rc=%s): %s",
            ssid,
            res.returncode,
            (res.stderr or res.stdout or "").strip(),
        )
        return False

    # Make sure active connection autoconnects
    try:
        active_cons = subprocess.check_output(
            ["nmcli", "-t", "--separator", "\t", "-f", "NAME,TYPE", "con", "show", "--active"],
            text=True,
        )
        for line in active_cons.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[1] == "802-11-wireless":
                subprocess.run(["nmcli", "con", "modify", parts[0], "connection.autoconnect", "yes"], check=False)
                break
    except Exception:
        pass

    _save_wifi_state({"ssid": ssid, "updated": time.time()})
    _persist_wifi_credentials_snapshot(source="restore_wifi_if_needed")
    time.sleep(1.0)
    return bool(_get_wifi_status().get("connected"))


def _wifi_restore_worker() -> None:
    enabled = os.environ.get("RC_CAR_WIFI_RESTORE_ON_BOOT", "1").strip().lower()
    if enabled in ("0", "false", "no", "off"):
        return

    _ensure_wifi_credentials_dir()

    max_attempts = int(os.environ.get("RC_CAR_WIFI_RESTORE_ATTEMPTS", "15"))
    delay_s = float(os.environ.get("RC_CAR_WIFI_RESTORE_DELAY_S", "1.0"))
    max_delay_s = float(os.environ.get("RC_CAR_WIFI_RESTORE_MAX_DELAY_S", "20.0"))

    for attempt in range(1, max_attempts + 1):
        try:
            if _restore_wifi_if_needed():
                logging.info("Wi-Fi restore complete")
                return
        except Exception:
            logging.exception("Wi-Fi restore attempt %s failed", attempt)

        time.sleep(delay_s)
        delay_s = min(max_delay_s, delay_s * 1.5)


def _get_ipv4_for_device(device: str) -> str | None:
    if not device:
        return None
    try:
        out = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show", "dev", device],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        # Example: "3: wlan0    inet 192.168.1.20/24 brd ..."
        for line in out.splitlines():
            parts = line.split()
            if "inet" in parts:
                idx = parts.index("inet")
                if idx + 1 < len(parts):
                    return parts[idx + 1].split("/", 1)[0]
    except Exception:
        return None
    return None


def _split_nmcli_t_line(line: str) -> list[str]:
    """Split an nmcli -t line that may use ':' (default) or a custom separator."""
    if "\t" in line:
        return line.split("\t")
    return line.split(":")


def _get_wifi_status() -> dict:
    saved = _load_wifi_state()
    status = {
        "connected": False,
        "ssid": None,
        "device": None,
        "connection": None,
        "ip": None,
        "saved_ssid": saved.get("ssid"),
        "saved_updated": saved.get("updated"),
    }

    try:
        # First: determine whether any Wi-Fi device is connected.
        dev_status = subprocess.check_output(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "dev", "status"],
            text=True,
        )
        wifi_device = None
        wifi_connection = None
        for line in dev_status.splitlines():
            if not line:
                continue
            parts = _split_nmcli_t_line(line)
            if len(parts) < 4:
                continue
            device, dev_type, state, connection = parts[0], parts[1], parts[2], parts[3]
            if dev_type == "wifi" and state == "connected":
                wifi_device = device or None
                wifi_connection = connection or None
                break

        if wifi_device:
            status["connected"] = True
            status["device"] = wifi_device
            status["connection"] = wifi_connection
            status["ip"] = _get_ipv4_for_device(wifi_device)

            # Second: best-effort SSID lookup.
            try:
                wifi_list = subprocess.check_output(
                    ["nmcli", "-t", "-f", "ACTIVE,SSID,DEVICE", "dev", "wifi", "list"],
                    text=True,
                )
                for wline in wifi_list.splitlines():
                    if not wline:
                        continue
                    wparts = _split_nmcli_t_line(wline)
                    if len(wparts) < 3:
                        continue
                    active, ssid, device = wparts[0], wparts[1], wparts[2]
                    if active.strip().lower() == "yes" and (not device or device == wifi_device):
                        status["ssid"] = ssid or None
                        break
            except Exception:
                pass

            # Third: if SSID is still unknown, try reading from the active connection.
            if not status.get("ssid") and wifi_connection:
                try:
                    ssid_val = subprocess.check_output(
                        ["nmcli", "-g", "802-11-wireless.ssid", "con", "show", wifi_connection],
                        text=True,
                    ).strip()
                    status["ssid"] = ssid_val or None
                except Exception:
                    pass
    except FileNotFoundError:
        status["error"] = "nmcli not found"
    except Exception as e:
        status["error"] = str(e)

    return status


def poll(job_id: str, stop_event: threading.Event, interval: float = 0.5) -> None:
    """
    Monitor the updater for a specific job_id. Writes the latest message and progress
    into `job_states[job_id]` so HTTP endpoints or SSE streams can read it.

    This function returns when the update finishes or when `stop_event` is set.
    """
    logging.info("Starting status request thread for job %s", job_id)

    try:
        while not stop_event.is_set():
            # read_state is expected to return (state, msg) where state may be None or a code
            update_state, msg = updater.read_state()

            with status_lock:
                st = job_states.get(job_id, {})
                # update message text
                st['msg'] = msg
                # if updater provides a numeric progress in msg or separately, try to set it
                # keep existing progress value if none available
                # If update_state indicates finished, mark done
                st['state'] = update_state
                st['done'] = (update_state == UPDATE_FINISHED)
                st['updated'] = time.time()
                job_states[job_id] = st

            if update_state is not None and update_state == UPDATE_FINISHED:
                break

            # wait with ability to wake early
            stop_event.wait(interval)

    except Exception:
        logging.exception("Error while polling updater for job %s", job_id)
    finally:
        # final state: if not present, ensure it's there
        with status_lock:
            st = job_states.get(job_id, {})
            st.setdefault('done', True)
            st.setdefault('msg', st.get('msg', 'finished'))
            st['updated'] = time.time()
            job_states[job_id] = st

        logging.info("Update finished for job %s", job_id)
        # Optional: reboot if desired
        try:
            logging.info("Rebooting in 5 seconds... ")

            while True:
                subprocess.run(["shutdown", "-r", "now"], check=True)
        except Exception:
            logging.exception("Failed to reboot after update")


@app.route("/")
def index():
    version : str = "0.00.0000"

    # Open version file
    try:
        with open('/etc/versions/oe-version.txt') as file:
            for line in file:
                    version = line.strip()
                    break
    except FileNotFoundError:
        pass

    # will look for templates/index.html
    return render_template("index.html", version=version, webui_version=WEB_UI_VERSION)


# keep your existing frontend endpoints; implement later:
@app.get("/api/wifi/scan")
def wifi_scan():
    try:
        # Ask NetworkManager to scan + list
        subprocess.run(["nmcli", "dev", "wifi", "rescan"], check=False)

        # Parse list as lines of SSIDs
        result = subprocess.check_output(
            ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
            text=True
        )
        networks = []
        for line in result.strip().splitlines():
            if not line:
                continue
            ssid, signal, security = (line.split(":", 2) + ["", "", ""])[:3]
            networks.append({
                "ssid": ssid,
                "signal": int(signal) if signal.isdigit() else 0,
                "security": security or "OPEN"
            })
        return jsonify(networks), 200
    except subprocess.CalledProcessError as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/wifi/connect")
def wifi_connect():
    data = request.get_json(silent=True) or {}
    ssid = data.get("ssid")
    password = data.get("password")

    if not ssid:
        return jsonify({"ok": False, "error": "Missing SSID"}), 400

    try:
        # Ensure Wi-Fi radio is enabled
        subprocess.run(["nmcli", "radio", "wifi", "on"], check=False)

        if password:
            cmd = ["nmcli", "dev", "wifi", "connect", ssid, "password", password]
        else:
            cmd = ["nmcli", "dev", "wifi", "connect", ssid]
        subprocess.check_call(cmd)

        # Make sure the created/used connection is set to autoconnect
        try:
            active_cons = subprocess.check_output(
                ["nmcli", "-t", "--separator", "\t", "-f", "NAME,TYPE", "con", "show", "--active"],
                text=True,
            )
            wifi_con_name = None
            for line in active_cons.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2 and parts[1] == "802-11-wireless":
                    wifi_con_name = parts[0]
                    break
            if wifi_con_name:
                subprocess.run(["nmcli", "con", "modify", wifi_con_name, "connection.autoconnect", "yes"], check=False)
        except Exception:
            pass

        # Persist Wi-Fi info to /data so it survives software updates.
        _save_wifi_state({"ssid": ssid, "updated": time.time()})
        _persist_wifi_credentials(ssid=ssid, password=password, source="wifi_connect")

        status = _get_wifi_status()
        return jsonify({"ok": True, **status}), 200
    except subprocess.CalledProcessError as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.get("/api/wifi/status")
def wifi_status():
    return jsonify({"ok": True, **_get_wifi_status()}), 200


def _is_safe_dir(path, base):
    # make sure 'path' stays within 'base'
    return os.path.commonpath([os.path.realpath(path), os.path.realpath(base)]) == os.path.realpath(base)


@app.post("/api/swu/upload")
def swu_upload():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file part"}), 400
    
    save_path = ""

    # Clear current files in the dir
    try:
        for filename in os.listdir(UPLOAD_DIR):
            file_path = os.path.join(UPLOAD_DIR, filename)
            if os.path.isfile(file_path):
                os.remove(file_path)
                print(f"Removed: {file_path}")
    except OSError as e:
        print(f"Error: {e}")

    file = request.files["file"]
    orig = (file.filename or "").strip()

    if not orig.lower().endswith(".swu"):
        return jsonify({"ok": False, "error": "Only .swu files are allowed"}), 400

    import uuid, time
    save_path = os.path.join(UPLOAD_DIR, file.filename)

    if not _is_safe_dir(save_path, UPLOAD_DIR):
        return jsonify({"ok": False, "error": "Invalid path"}), 400

    print("File name: ", file.filename)
    try:
        file.save(save_path)  # streamed to disk by Werkzeug
    except Exception as e:
        return jsonify({"ok": False, "error": f"Failed to save file: {e}"}), 500

    return jsonify({"ok": True, "filename": file.filename, "path": save_path}), 200


@app.post("/api/swu/apply")
def swu_apply():
    """
    NO-OP stub: just validate inputs and return JSON.
    Fill in your swupdate call here later.
    """
    data = request.get_json(silent=True) or {}
    path = (data.get("path") or "").strip()
    filename = (data.get("filename") or "").strip()

    if not path or not filename:
        return jsonify({"ok": False, "error": "Missing filename/path"}), 400

    # keep it safe: must be a file inside UPLOAD_DIR
    real_upload = os.path.realpath(UPLOAD_DIR)
    real_path = os.path.realpath(path)
    if not real_path.startswith(real_upload + os.sep) or not os.path.isfile(real_path):
        return jsonify({"ok": False, "error": "Invalid or missing file"}), 400

    # TODO: put your swupdate call here later
    # e.g., subprocess.Popen(["swupdate", "-i", real_path, "-e", "stable", "-v"])+
    
    # Ensure /data/wifi-credentials exists and snapshot Wi-Fi info before swupdate/reboot.
    # This is best-effort; update should still proceed even if snapshotting fails.
    try:
        _persist_wifi_credentials_snapshot(source="swu_apply")
    except Exception:
        pass

    # start the updater with the validated real path (not the module-level save_path)
    ret: bool = updater.start_update(real_path)
    msg = "apply started" if ret else "ERROR"

    # create a job id and start a per-job poller thread
    job_id = str(uuid.uuid4())
    with status_lock:
        job_states[job_id] = {"msg": "starting", "state": None, "done": False, "updated": time.time()}

    stop_event = threading.Event()
    job_events[job_id] = stop_event
    t = threading.Thread(target=poll, args=(job_id, stop_event, 0.001), daemon=True)
    job_threads[job_id] = t
    t.start()

    return jsonify({
        "ok": True,
        "message": msg,
        "job_id": job_id,
        "received": {"filename": filename, "path": real_path}
    }), 200


def get_ip_address(ifname):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    return socket.inet_ntoa(fcntl.ioctl(
        s.fileno(),
        0x8915,  # SIOCGIFADDR
        struct.pack('256s', ifname[:15])
    )[20:24])


@app.get('/api/swu/progress/<job_id>')
def swu_progress(job_id):
    """Return the latest progress state for a job as JSON."""
    with status_lock:
        st = job_states.get(job_id)
        if st is None:
            return jsonify({"ok": False, "error": "unknown job"}), 404
        return jsonify({"ok": True, **st}), 200


@app.get('/api/swu/progress/<job_id>/stream')
def swu_progress_stream(job_id):
    """SSE stream of progress updates for a job."""
    from flask import Response, stream_with_context

    def event_stream():
        last_ts = 0
        while True:
            with status_lock:
                st = job_states.get(job_id)
            if st is None:
                yield f"data: {json.dumps({'error':'unknown job'})}\n\n"
                break

            # send only if updated
            if st.get('updated', 0) != last_ts:
                last_ts = st.get('updated', 0)
                yield f"data: {json.dumps(st)}\n\n"

            # stop if done
            if st.get('done'):
                break

            time.sleep(0.5)

    return Response(stream_with_context(event_stream()), mimetype='text/event-stream')


@sock.route('/ws/terminal')
def terminal_ws(ws):
    """
    WebSocket terminal bridge
    """
    stop = threading.Event()
    tcp = TcpClient(port=CLI_PORT, host="127.0.0.1", timeout=1)
    tcp.open(timeout=1)

    ws.send("\r\n\x1b[1;32mRC Car Terminal\x1b[0m\r\n")
    ws.send("\x1b[2mConnected to server — echo mode active\x1b[0m\r\n\r\n")
    ws.send("\x1b[32m$\x1b[0m ")

    def _tcp_reader():
        while not stop.is_set():
            try:
                data = tcp.read()
            except OSError:
                break
            if data is None:
                continue
            try:
                ws.send(data.decode('utf-8', errors='replace'))
            except Exception:
                break

    def _terminal_input_reader():
        while not stop.is_set():
            data = ws.receive()
            if data is None:
                break
            try:
                tcp.send(data.encode('utf-8'))
            except Exception:
                pass

    tcp_thread   = threading.Thread(target=_tcp_reader, daemon=True)
    input_thread = threading.Thread(target=_terminal_input_reader, daemon=True)

    tcp_thread.start()
    input_thread.start()

    input_thread.join()   # exits when the WebSocket disconnects
    stop.set()            # signal _tcp_reader to stop
    
    logging.info("WebSocket disconnected, closing TCP connection")
    tcp.close()


if __name__ == "__main__":
    # Create base logger
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # Formatter (add timestamps if you want)
    formatter = logging.Formatter('[%(levelname)s] %(message)s')

    # File handler
    file_handler = logging.FileHandler('/var/log/rc-car-webserver.log')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Console (stdout) handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    if len(sys.argv) < 1:
        logging.log(logging.ERROR, "No command-line arguments provided.")

    logging.log(logging.INFO, "Web server version: %s", WEB_UI_VERSION)

    # Start a background restore attempt so Wi-Fi can come back after swupdate.
    threading.Thread(target=_wifi_restore_worker, daemon=True).start()

    # Bind ONLY to Ethernet so the UI is never reachable over Wi‑Fi.
    ip = get_ip_address(b'enP8p1s0')
    if not ip:
        logging.log(logging.ERROR, "Could not determine the IP address of the ethernet interface")
        sys.exit(1)

    # Remove all files in /home/images

    if updater.init_connection() == False:
        logging.log(logging.ERROR, "ERROR: Failed to open port")
        exit(0)
        
    logging.log(logging.INFO, "Bind host: %s:%s (ethernet)", ip, WEB_PORT)

    # debug=True reloads on changes during dev
    app.run(host=ip, port=WEB_PORT, debug=True, use_reloader=False)
