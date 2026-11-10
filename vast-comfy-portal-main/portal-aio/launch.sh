#!/bin/bash

export DEBIAN_FRONTEND=NONINTERACTIVE
export PYTHONUNBUFFERED=1
export TUNNEL_MANAGER="http://localhost:11112"
export CLOUDFLARE_METRICS="localhost:11113"

# Create log directory
mkdir -p /var/log/portal/

# Array to store PIDs
declare -a PIDS=()

cleanup() {
    echo "Shutting down all processes..."
    # Kill all children in the process group
    pkill -P $$
    # Kill any remaining processes in our array
    for pid in "${PIDS[@]}"; do
        kill -TERM "$pid" 2>/dev/null
    done
    exit 0
}

trap cleanup SIGINT SIGTERM

instance_portal() {
    if [[ ! -d /opt/portal-aio/venv ]]; then
        apt install -y --no-install-recommends python3-minimal python3-venv python3-wheel jq 2>&1 | tee -a /var/log/portal/portal.log
        python3 -m venv /opt/portal-aio/venv
        /opt/portal-aio/venv/bin/pip install -r /opt/portal-aio/requirements.txt 2>&1 | tee -a /var/log/portal/portal.log
    fi

    cd /opt/portal-aio/caddy_manager || exit 1
    /opt/portal-aio/venv/bin/python caddy_config_manager.py 2>&1 | tee -a /var/log/portal/caddy.log
    /opt/portal-aio/caddy_manager/caddy run --config /etc/Caddyfile 2>&1 | tee -a /var/log/portal/caddy.log &
    PIDS+=($!)

    cd /opt/portal-aio/tunnel_manager || exit 1
    /opt/portal-aio/venv/bin/fastapi run --host 127.0.0.1 --port 11112 tunnel_manager.py 2>&1 | tee -a /var/log/portal/tunnel-manager.log &
    PIDS+=($!)

    cd /opt/portal-aio/portal || exit 1
    /opt/portal-aio/venv/bin/fastapi run --host 127.0.0.1 --port 11111 portal.py 2>&1 | tee -a /var/log/portal/portal.log &
    PIDS+=($!)

    # Wait for the config file to be present
    while [ ! -f /etc/portal.yaml ]; do sleep 1; done

    # Syncthing setup if specified in config
    if grep -qi '^\s\+name:\s*syncthing\s*$' /etc/portal.yaml && [[ -n $VAST_TCP_PORT_72299 ]]; then
        install_syncthing
        run_syncthing
    fi

    # Wait for all processes while allowing signals to be caught
    wait
}

install_syncthing() {
    if [[ ! -f /opt/syncthing/syncthing ]]; then
        echo "Installing and configuring Syncthing" | tee /var/log/portal/syncthing.log
        SYNCTHING_VERSION="$(curl -fsSL "https://api.github.com/repos/syncthing/syncthing/releases/latest" \
                    | jq -r '.tag_name' | sed 's/[^0-9\.\-]*//g')"

        SYNCTHING_URL="https://github.com/syncthing/syncthing/releases/download/v${SYNCTHING_VERSION}/syncthing-linux-amd64-v${SYNCTHING_VERSION}.tar.gz"
        mkdir /opt/syncthing/
        wget -O /opt/syncthing.tar.gz $SYNCTHING_URL && (cd /opt && tar -zxf syncthing.tar.gz -C /opt/syncthing/ --strip-components=1) && rm -f /opt/syncthing.tar.gz
        if [[ ! -f /opt/syncthing/syncthing ]]; then
            echo "Failed to fetch syncthing. Exiting" | tee /var/log/portal/syncthing.log
            exit 1
        fi
    fi
}


run_syncthing() {
    /opt/syncthing/syncthing generate
    sed -i '/^\s*<listenAddress>/d' "/root/.local/state/syncthing/config.xml"
    /opt/syncthing/syncthing --gui-address="127.0.0.1:18384" --gui-apikey="${OPEN_BUTTON_TOKEN}" --no-upgrade 2>&1 | tee -a /var/log/portal/syncthing.log &
    PIDS+=($!)

    until curl --output /dev/null --silent --head --fail "http://127.0.0.1:18384"; do
        echo "Waiting for syncthing server...\n" | tee -a /var/log/portal/syncthing.log
        sleep 1
    done

    # Execute configuration commands with retries
    run_with_retry /opt/syncthing/syncthing cli --gui-address="127.0.0.1:18384" --gui-apikey="${OPEN_BUTTON_TOKEN}" config gui insecure-admin-access set true 2>&1 | tee -a /var/log/portal/syncthing.log 
    run_with_retry /opt/syncthing/syncthing cli --gui-address="127.0.0.1:18384" --gui-apikey="${OPEN_BUTTON_TOKEN}" config gui insecure-skip-host-check set true 2>&1 | tee -a /var/log/portal/syncthing.log 
    run_with_retry /opt/syncthing/syncthing cli --gui-address="127.0.0.1:18384" --gui-apikey="${OPEN_BUTTON_TOKEN}" config options raw-listen-addresses add "tcp://0.0.0.0:${VAST_TCP_PORT_72299}" 2>&1 | tee -a /var/log/portal/syncthing.log 
}

run_with_retry() {
    until "$@"; do
        printf "Command failed. Retrying...\n"
        sleep 1
    done
}

instance_portal "$@"