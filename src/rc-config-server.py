from flask import Flask, render_template, request, jsonify
import subprocess


app = Flask(__name__)


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


if __name__ == "__main__":
    # debug=True reloads on changes during dev
    app.run(host="0.0.0.0", port=5000, debug=True)