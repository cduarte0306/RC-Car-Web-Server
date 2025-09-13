from flask import Flask, render_template, request, jsonify
import subprocess
import sys
import socket
import fcntl
import struct
import os
import time
import uuid
import threading

from connection_manager import UpdatePipe
import time


# Defines
UPLOAD_DIR = "/home/images"
updater = UpdatePipe()

status_lock  = threading.Lock()
thread_can_run : bool = False
progress : float = 0.0
thread = None

app = Flask(__name__)


def poll() -> None:
    thread_can_run = True

    while thread_can_run:
        status_lock.acquire()
        progress = updater.read_state()
        status_lock.release()
        time.sleep(0.1)

        if progress == 100.0:
            break


@app.route("/")
def index():
    # will look for templates/index.html
    return render_template("index.html")


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
        if password:
            cmd = ["nmcli", "dev", "wifi", "connect", ssid, "password", password]
        else:
            cmd = ["nmcli", "dev", "wifi", "connect", ssid]
        subprocess.check_call(cmd)
        return jsonify({"ok": True}), 200
    except subprocess.CalledProcessError as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _is_safe_dir(path, base):
    # make sure 'path' stays within 'base'
    return os.path.commonpath([os.path.realpath(path), os.path.realpath(base)]) == os.path.realpath(base)


@app.post("/api/swu/upload")
def swu_upload():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file part"}), 400
    
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
    ret : bool = updater.start_update()
    msg = "apply stub (no-op)" if ret else "ERROR"
    
    thread = threading.Thread(target=poll)
    thread.start()

    return jsonify({
        "ok": True,
        "message": msg,
        "received": {"filename": filename, "path": real_path}
    }), 200


def get_ip_address(ifname):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    return socket.inet_ntoa(fcntl.ioctl(
        s.fileno(),
        0x8915,  # SIOCGIFADDR
        struct.pack('256s', ifname[:15])
    )[20:24])


if __name__ == "__main__":
    if len(sys.argv) < 1:
        print("No command-line arguments provided.")

    print("Web server version: 1.0.0")

    # Interface to bind to
    ip = get_ip_address(b'enP8p1s0')
    if not ip:
        print("Could not determine the IP address of the interface")
        sys.exit(1)

    # Remove all files in /home/images

    # ip = "127.0.0.1"
    if updater.init_connection() == False:
        print("ERROR: Failed to open port")
        exit(0)
        
    print("Inteface IPL: ", ip)

    # debug=True reloads on changes during dev
    app.run(host=ip, port=5000, debug=True, use_reloader=False)