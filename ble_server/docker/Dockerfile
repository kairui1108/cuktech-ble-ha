FROM python:3.11-slim

LABEL org.opencontainers.image.title="CUKTECH BLE Server"
LABEL org.opencontainers.image.description="CUKTECH 10 GaN Charger BLE Server for Home Assistant"
LABEL org.opencontainers.image.source="https://github.com/kairui1108/cuktech-ble-ha"

RUN apt-get update && apt-get install -y --no-install-recommends \
    bluez \
    dbus \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .

RUN pip install --no-cache-dir \
    aiohttp>=3.9 \
    paho-mqtt>=2.0 \
    bleak>=0.21 \
    cryptography>=41 \
    pyyaml>=6.0

COPY src/ src/
COPY web/ web/
COPY state.py state_protocol_v2.py history.py config.py ble_manager.py ha_server.py .

RUN mkdir -p /data

EXPOSE 8199

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
