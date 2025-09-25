from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
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

from connection_manager import UpdatePipe
import time


WEB_UI_VERSION = "1.00.0002"


# Defines
UPLOAD_DIR = "/home/images"
updater = UpdatePipe()

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

socketio = SocketIO(app=app, async_mode='gevent')

UPDATE_FINISHED = 3


def poll(job_id: str, stop_event: threading.Event, interval: float = 0.5) -> None:
    """
    Monitor the updater for a specific job_id. Writes the latest message and progress
    into `job_states[job_id]` so HTTP endpoints or SSE streams can read it.

    This function returns when the update finishes or when `stop_event` is set.
    """
    logging.info("Starting status request thread for job %s", job_id)

    try:
        while not stop_event.is_set():
            update_state, msg = updater.read_state()
            with status_lock:
                st = job_states.get(job_id, {})
                st['msg'] = msg
                st['state'] = update_state
                st['done'] = (update_state == UPDATE_FINISHED)
                st['updated'] = time.time()
                job_states[job_id] = st

            if update_state is not None and update_state == UPDATE_FINISHED:
                break
            stop_event.wait(interval)

    except Exception:
        logging.exception("Error while polling updater for job %s", job_id)
    finally:
        with status_lock:
            st = job_states.get(job_id, {})
            st.setdefault('done', True)
            st.setdefault('msg', st.get('msg', 'finished'))
            st['updated'] = time.time()
            job_states[job_id] = st

        logging.info("Update finished for job %s", job_id)
        try:
            logging.info("Rebooting in 5 seconds... ")
            while True:
                subprocess.run(["shutdown", "-r", "now"], check=True)
        except Exception:
            logging.exception("Failed to reboot after update")


@app.route("/")
def index():
    version : str = "0.00.0000"
    try:
        with open('/etc/versions/oe-version.txt') as file:
            for line in file:
                version = line.strip()
                break
    except FileNotFoundError:
        pass
    return render_template("index.html", version=version, webui_version=WEB_UI_VERSION)


@app.get("/api/wifi/scan")
def wifi_scan():
    try:
        subprocess.run(["nmcli", "dev", "wifi", "rescan"], check=False)
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
        if password:
            cmd = ["nmcli", "dev", "wifi", "connect", ssid, "password", password]
        else:
            cmd = ["nmcli", "dev", "wifi", "connect", ssid]
        subprocess.check_call(cmd)
        return jsonify({"ok": True}), 200
    except subprocess.CalledProcessError as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _is_safe_dir(path, base):
    return os.path.commonpath([os.path.realpath(path), os.path.realpath(base)]) == os.path.realpath(base)


@app.post("/api/swu/upload")
def swu_upload():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file part"}), 400

    save_path = ""
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

    save_path = os.path.join(UPLOAD_DIR, file.filename)
    if not _is_safe_dir(save_path, UPLOAD_DIR):
        return jsonify({"ok": False, "error": "Invalid path"}), 400

    print("File name: ", file.filename)
    try:
        file.save(save_path)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Failed to save file: {e}"}), 500

    return jsonify({"ok": True, "filename": file.filename, "path": save_path}), 200


@app.post("/api/swu/apply")
def swu_apply():
    data = request.get_json(silent=True) or {}
    path = (data.get("path") or "").strip()
    filename = (data.get("filename") or "").strip()

    if not path or not filename:
        return jsonify({"ok": False, "error": "Missing filename/path"}), 400

    real_upload = os.path.realpath(UPLOAD_DIR)
    real_path = os.path.realpath(path)
    if not real_path.startswith(real_upload + os.sep) or not os.path.isfile(real_path):
        return jsonify({"ok": False, "error": "Invalid or missing file"}), 400

    ret: bool = updater.start_update(real_path)
    msg = "apply started" if ret else "ERROR"

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
        0x8915,
        struct.pack('256s', ifname[:15])
    )[20:24])


@app.get('/api/swu/progress/<job_id>')
def swu_progress(job_id):
    with status_lock:
        st = job_states.get(job_id)
        if st is None:
            return jsonify({"ok": False, "error": "unknown job"}), 404
        return jsonify({"ok": True, **st}), 200


@app.get('/api/swu/progress/<job_id>/stream')
def swu_progress_stream(job_id):
    from flask import Response, stream_with_context

    def event_stream():
        last_ts = 0
        while True:
            with status_lock:
                st = job_states.get(job_id)
            if st is None:
                yield f"data: {json.dumps({'error':'unknown job'})}\n\n"
                break

            if st.get('updated', 0) != last_ts:
                last_ts = st.get('updated', 0)
                yield f"data: {json.dumps(st)}\n\n"

            if st.get('done'):
                break
            time.sleep(0.5)

    return Response(stream_with_context(event_stream()), mimetype='text/event-stream')


@socketio.on('connect', namespace='/console')
def connect_console():
    logging.info("Console connected")
    # 🔗 HOOK: open/init connection to your CLI backend here
    # Example: open a TCP/UNIX socket to your app listening on 127.0.0.1:PORT
    # or register this web client in your own connection manager
    #
    # e.g.,
    # cli_conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # cli_conn.connect(("127.0.0.1", 5555))
    # save cli_conn in a dict keyed by request.sid

    emit('output', "Connected to RC Car console.\r\n", namespace='/console')


@socketio.on('input', namespace='/console')
def console_input(data):
    # 🔗 HOOK: forward web client keystrokes/commands to your CLI here
    # Example: write to TCP socket or message queue that your CLI is reading
    #
    # e.g.,
    # cli_conn = cli_connections.get(request.sid)
    # if cli_conn:
    #     cli_conn.sendall(data.encode())

    logging.info(f"Console input: {data!r}")


@socketio.on('disconnect', namespace='/console')
def disconnect_console():
    # 🔗 HOOK: clean up CLI connection here
    # Example: close the socket or unregister client
    #
    # e.g.,
    # cli_conn = cli_connections.pop(request.sid, None)
    # if cli_conn:
    #     cli_conn.close()

    logging.info(f"Client {request.sid} disconnected from /console")



if __name__ == "__main__":
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('[%(levelname)s] %(message)s')

    file_handler = logging.FileHandler('/var/log/rc-car-webserver.log')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    logging.log(logging.INFO, "Web server version: %s", WEB_UI_VERSION)

    ip = get_ip_address(b'enP8p1s0')
    if not ip:
        logging.log(logging.ERROR, "Could not determine the IP address of the interface")
        sys.exit(1)

    if not updater.init_connection():
        logging.log(logging.ERROR, "ERROR: Failed to open port")
        exit(0)

    logging.log(logging.INFO, "Interface IPL: %s", ip)

    # 🔑 Use socketio.run instead of app.run
    socketio.run(app, host=ip, port=5000, debug=True, use_reloader=False)
