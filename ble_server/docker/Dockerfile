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

# Python 在非 TTY 下按块缓冲 stdout：容器里 `docker logs` 只会看到 entrypoint 那几行
# shell 输出，业务日志要等缓冲区满才落出来（出事时看起来像"服务没在跑"）。
# 这一行让它逐行刷出；日志文件（CUKTECH_LOG_FILE）不受影响。
ENV PYTHONUNBUFFERED=1
# 没有 TTY 时默认不加 StreamHandler（避免 ctl 场景日志整份重复），容器里显式打开，
# 这样 `docker logs` 能看到业务日志而不是只有启动横幅。
ENV CUKTECH_LOG_STREAM=1

RUN mkdir -p /data

# Copy default config (user can override by mounting their own)
COPY config.yaml.example ./config.yaml

EXPOSE 8199

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
