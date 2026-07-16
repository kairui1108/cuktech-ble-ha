# CUKTECH BLE Server — Docker 部署

## 前置条件

- 已安装 Docker 和 Docker Compose
- 主机已配置蓝牙适配器（`bluetoothctl` 可用）
- 已获取充电器的 MAC、Token、BLE Key

## 快速开始

### 方式 A：配置文件

```bash
cd ble_server

# 1. 准备配置文件
cp config.yaml.example config.yaml
# 编辑 config.yaml，填入 MAC、Token、BLE Key

# 2. 构建并启动
docker compose -f docker/docker-compose.yml up -d

# 3. 查看日志
docker compose -f docker/docker-compose.yml logs -f
```

### 方式 B：环境变量

```bash
cd ble_server

# 1. 编辑环境变量
vim docker/docker-compose.env.yml
# 修改 CUKTECH_DEVICE_MAC / TOKEN / BLE_KEY

# 2. 构建并启动
docker compose -f docker/docker-compose.env.yml up -d

# 3. 查看日志
docker compose -f docker/docker-compose.env.yml logs -f
```

### 方式 C：docker run

```bash
docker run -d \
  --name cuktech-ble \
  --network host \
  --privileged \
  --restart unless-stopped \
  -v $(pwd)/config.yaml:/app/config.yaml:ro \
  -v /var/run/dbus/system_bus_socket:/var/run/dbus/system_bus_socket:ro \
  -v cuktech-data:/data \
  cuktech-ble-server:latest
```

## 验证

```bash
# 检查容器状态
docker ps | grep cuktech

# 访问 Web UI
curl http://localhost:8199/api/status

# 浏览器打开
# http://<服务器IP>:8199
```

## 蓝牙说明

- 容器通过 `--privileged` 和 D-Bus 挂载使用主机蓝牙适配器
- 主机 BlueZ 不受影响，其他蓝牙应用可正常使用
- BLE 恢复机制（`bluetoothctl power off/on`）会影响主机蓝牙，属于预期行为
- 确保主机 `bluetooth` 服务正常运行

## 常用命令

```bash
# 停止
docker compose -f docker/docker-compose.yml down

# 重启
docker compose -f docker/docker-compose.yml restart

# 进入容器
docker exec -it cuktech-ble bash

# 查看蓝牙状态
docker exec cuktech-ble bluetoothctl show
```
