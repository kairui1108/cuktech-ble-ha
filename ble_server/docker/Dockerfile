FROM python:3.11-slim

LABEL org.opencontainers.image.title="CUKTECH BLE Server"
LABEL org.opencontainers.image.description="CUKTECH 10 GaN Charger BLE Server for Home Assistant"
LABEL org.opencontainers.image.source="https://github.com/kairui1108/cuktech-ble-ha"

RUN apt-get update && apt-get install -y --no-install-recommends \
    bluez \
    dbus \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the cuktech_ble package + all dependencies from pyproject.toml
COPY pyproject.toml ./
COPY src/ src/
RUN pip install --no-cache-dir .

COPY web/ web/
# 顶层模块**整目录**拷：以前这里是手抄的文件清单，多设备改造新增的
# adapter_coordinator.py / log_utils.py 就漏了，而 ha_server.py 顶层 import 它们
# ——镜像一启动就 ModuleNotFoundError，而测试与 CI 都看不到（它们跑的是源码树）。
COPY *.py ./

RUN mkdir -p /data

# Copy default config (user can override by mounting their own)
COPY config.yaml.example ./config.yaml

EXPOSE 8199

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
