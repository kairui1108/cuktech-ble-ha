# Changelog

## [1.0.10] - 2026-08-16

### Fixed
- BLE 断连时清空端口数据，避免实体展示断连前的陈旧读数（端口值变为 unknown 而非误导性数字）
- `_async_wait_mqtt_ready` 改用官方 `mqtt.async_wait_for_mqtt_client()`（内部限时 50s），不再阻塞最长 ~90s 的自制重试/探测发布

### Changed
- 移除 6 个平台文件中未使用的 `logging` / `_LOGGER` 导入（sensor/switch/binary_sensor/select/number/event）
- 删除不再使用的 `TOPIC_PROBE` 常量与 `MQTT_RETRY_*` 常量
- 声明开发测试依赖 `pytest` / `pytest-asyncio`（`pyproject.toml [project.optional-dependencies].dev`）

### Tests
- 补齐 `pytest-asyncio`（此前 `@pytest.mark.asyncio` 用例在本环境无法运行，13 例失败，现全部通过）
- 新增用例：BLE 断连清空端口数据、MQTT 就绪等待成功/失败（ConfigEntryNotReady）
- 新增嵌套 CI 工作流 `.github/workflows/tests.yml`（Python 3.11/3.12 矩阵）

## [1.0.9] - 2026-07-30

### Added
- `CuktechBaseEntity` base class unifying entity lifecycle across 11 entity classes
- Fine-grained callbacks: port/settings-specific notification replaces full broadcast

### Changed
- Coordinator: extracted shared device-info/BLE state sync methods, eliminating duplicate logic between MQTT status and HTTP health check paths
- Entity classes migrated to `CuktechBaseEntity`, eliminating ~150 lines of duplicate code
- `CuktechConnectionSwitch.available` no longer blocked by `ble_pending`
- `TotalPowerSensor` uses `PORT_MAP.values()` instead of hardcoded keys

### Fixed
- Event entity: redundant `async_write_ha_state` after `_trigger_event`
- ConfigFlow: removed dead `except ValueError: raise` stub

## [1.0.8] - 2026-07-25

### Changed
- BLE enable/disable: MQTT as primary channel, HTTP as fallback (no more dual write)
- `time.time()` replaced with `hass.loop.time()` to avoid system clock shift issues

### Fixed
- reauth flow missing `async_set_unique_id`, preventing proper config entry matching
- Silent exception in `_async_health_check` JSON parsing now logged as warning
- Health check HTTP success now updates `_last_status_time` for correct availability
- `_last_status_time` initialized to negative value for correct startup availability

## [1.0.7] - 2026-07-22

### Added
- Charge session event entity: fires `charge_end` event via MQTT on session completion

## [1.0.6] - 2026-07-19

### Added
- Compatible with ESP32 firmware and BLE Server 

## [1.0.5] - 2026-07-14

### Added
- CuktechProtocolSwitch: 10 protocol switch entities for per-port PD/PPS/UFCS/SCP control
- PPS PD dependency: C1/C2 PPS automatically shows OFF when PD is OFF
- Protocol_switches decode/encode in coordinator (PIID 21)
- Lock-protected async_set_protocol for read-modify-write safety
- PROTOCOL_BITS constant definition in const.py
- Entity tests: is_on, PD dependency, unique_id, async_turn_on/off
- Coordinator tests: protocol_switches decode/encode/roundtrip/unknown

### Changed
- BLE Server dependency bumped to v1.0.5

### Fixed
- PIID 21 SET encoding: 2-byte piid LE, proper tl, dynamic total_len
- Session key leak: print() → _LOGGER.debug()

### Security
- Key material no longer printed to stderr (debug log only)

## [1.0.4] - 2026-07-13

### Added
- Protocol detection V2 engine (state_protocol_v2.py)
- PROTOCOL_OPTIONS aligned with Mi Home: 5V/QC/AFC/FCP/SCP/PD/PPS/UFCS
- Integration test fixtures for protocol detection

### Changed
- BLE Server dependency bumped to v1.0.4
- MQTT is now opt-in: mqtt.enabled defaults to false
- ConfigFlow default name updated to full product name

### Fixed
- PIID 6 duplicate value comment added
- test_health_failures renamed to match actual assertions
- Availability logic: HTTP failure respects MQTT connected state
- MQTT connected: false no longer falsely marks device available
- Duplicate entities: removed PIID 19/20 from SENSOR_PIIDS (already in SETTING_PIIDS)
- MQTT publish error handling: async_set_value/port_control wrapped in try/except
- conftest.py: real HA base classes for proper @property support

### Removed
- CuktechProtocolSwitch (10 protocol switch entities) — control moved to BLE Server side
- TOPIC_PROTOCOL from const.py
- /api/protocol endpoint from ble_server

## [1.0.3] - 2026-07-11

### Added
- CuktechConnectionSwitch: BLE enable/disable control via HTTP API
- CuktechConnectionBinarySensor: BLE connection status display
- async_enable_ble with asyncio.Lock, 30s timeout, optimistic state
- ble_enabled synced with ble_connected from MQTT status
- Switch available property includes ble_pending check
- ConfigFlow default name updated to full product name

### Fixed
- BLE connection stability: power cycle LL disconnect wait, GATT settle time
- NoneType errors: null checks for self.ctrl in main loop and handlers
- handle_enable(false): await ble_task before power cycle to prevent race
- controller: start_notify wrapped in try/except for partial failure

## [1.0.2] - 2026-07-10

### Added
- Real entity class unit tests (30 tests for Sensor/Switch/BinarySensor/Select/Number)
- ConfigFlow tests: async_step_user form/create/unique_id/errors/abort
- Coordinator tests: async_set_value, async_port_control with payload verification
- MQTT LWT (Last Will and Testament) for crash detection
- async_will_remove_from_hass super() calls on all entities
- _notify_callbacks iterates list copy to prevent mutation during iteration

### Fixed
- Availability logic: HTTP failure respects MQTT connected state
- MQTT `connected: false` no longer falsely marks device available
- Duplicate entities: removed PIID 19/20 from SENSOR_PIIDS (already in SETTING_PIIDS)
- MQTT publish error handling: async_set_value/port_control wrapped in try/except
- Config flow error messages now use HA translation keys
- test_health_failures renamed to match actual assertion
- conftest.py: real HA base classes for proper @property support

### Fixed
- Availability logic: HTTP failure respects MQTT connected state
- MQTT `connected: false` no longer falsely marks device available
- Duplicate entities: removed PIID 19/20 from SENSOR_PIIDS (already in SETTING_PIIDS)
- MQTT publish error handling: async_set_value/port_control wrapped in try/except
- Config flow error messages now use HA translation keys
- conftest.py: real HA base classes for proper @property support

### Changed
- BLE module split: ble.py → protocol.py + controller.py + cli.py
- CORS restricted to localhost origins only
- Removed unused PUT/DELETE from CORS allowed methods

## [1.0.1] - 2026-07-09

### Added
- HACS support for easy installation
- My Home Assistant badges for one-click integration setup
- Bilingual README (Chinese/English) with language switcher
- Server URL configuration in config flow
- Dual availability detection (MQTT + HTTP health check)
- SQLite port history storage with configurable retention
- Log level management API
- Chart API with backend-computed data alignment
- Statistics and CSV export APIs
- systemd service and logrotate configs
- Log rotation in startup script

### Fixed
- ConfigEntry import missing in sensor.py
- MQTT port command missing cmd_future
- Multiframe data handling
- History data retention (default 2 days)
- Chart data alignment between frontend and backend
- Exponential backoff for BLE reconnection

### Changed
- Coordinator data property returns settings directly (no wrapper)
- Health check interval increased to 30 seconds
- MQTT reconnection uses exponential backoff (1s→30s)
- SQLite writes use threading.Lock for thread safety
- Static assets served with Cache-Control headers (7 days)

## [1.0.0] - 2026-07-07

### Added
- Initial release
- BLE Server with MiOT authentication
- Real-time power monitoring via MQTT
- Web UI with power charts and port control
- Home Assistant integration
