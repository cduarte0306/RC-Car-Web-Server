#!/bin/bash

# === CONFIGURATION ===
JETSON_IP="192.168.1.10"
JETSON_USER="root"
JETSON_TARGET_DIR="/tmp"
REMOTE_DIR="${JETSON_TARGET_DIR}/rc-car-web-server"
REMOTE_APP_PATH="${REMOTE_DIR}/rc-config-server.py"
WEB_PORT=5000

MODE="$1"

if [[ "$MODE" == "local" ]]; then
    echo "[*] Selected MODE: local"

    if [[ ! -f "src/rc-config-server.py" ]]; then
        echo "[!] Web server entry point not found: src/rc-config-server.py"
        exit 1
    fi

    echo "[*] Starting web server locally..."
    cd src && python3 rc-config-server.py
    exit 0

elif [[ "$MODE" == "remote" ]]; then
    echo "[*] Selected MODE: Jetson Nano (remote)"

    if [[ ! -f "src/rc-config-server.py" ]]; then
        echo "[!] Web server entry point not found: src/rc-config-server.py"
        exit 1
    fi

    echo "[*] Stopping any previous web server instance on Jetson..."
    ssh "${JETSON_USER}@${JETSON_IP}" "pkill -f '${REMOTE_APP_PATH}' || true"

    echo "[*] Ensuring remote directory exists on Jetson..."
    ssh "${JETSON_USER}@${JETSON_IP}" "mkdir -p '${REMOTE_DIR}'" \
        || { echo "[!] Remote directory creation failed"; exit 1; }

    echo "[*] Uploading web app to Jetson..."
    scp -r ./src/* "${JETSON_USER}@${JETSON_IP}:${REMOTE_DIR}/" \
        || { echo "[!] SCP failed"; exit 1; }

    echo "[*] Purging stale __pycache__ on Jetson..."
    ssh "${JETSON_USER}@${JETSON_IP}" "find '${REMOTE_DIR}' -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true"

    echo "[*] Starting web server on Jetson..."
    ssh "${JETSON_USER}@${JETSON_IP}" \
        "cd '${REMOTE_DIR}' && nohup python3 '${REMOTE_APP_PATH}' > /dev/null 2>&1 &"

    sleep 1

    echo "[*] Waiting for web server to listen on ${JETSON_IP}:${WEB_PORT}..."
    for i in {1..20}; do
        if nc -z "${JETSON_IP}" "${WEB_PORT}"; then
            echo "[*] Web server is up!"
            exit 0
        fi
        sleep 0.5
    done

    echo "[!] Web server never opened port ${WEB_PORT}"
    exit 1

elif [[ "$MODE" == "upload" ]]; then
    echo "[*] Selected MODE: Upload only"

    if [[ ! -f "src/rc-config-server.py" ]]; then
        echo "[!] Web server entry point not found: src/rc-config-server.py"
        exit 1
    fi

    echo "[*] Ensuring remote directory exists on Jetson..."
    ssh "${JETSON_USER}@${JETSON_IP}" "mkdir -p '${REMOTE_DIR}'" \
        || { echo "[!] Remote directory creation failed"; exit 1; }

    echo "[*] Uploading web app to Jetson..."
    scp -r ./src/* "${JETSON_USER}@${JETSON_IP}:${REMOTE_DIR}/" \
        || { echo "[!] SCP failed"; exit 1; }

    echo "[*] Purging stale __pycache__ on Jetson..."
    ssh "${JETSON_USER}@${JETSON_IP}" "find '${REMOTE_DIR}' -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true"

    echo "[*] Cleaning old update files..."
    ssh "${JETSON_USER}@${JETSON_IP}" "rm -f /home/images/*.swu /data/firmware/*.swu 2>/dev/null || true"

    echo "[*] Upload complete: ${REMOTE_DIR}"
    exit 0

else
    echo "Usage: $0 [local|remote|upload]"
    exit 1
fi
