# Changelog

## [1.1.0] - 2026-09-22

### Fixed
- Charge event dedup used `session_id` as key; ble_server sends `session_id=0` for unrecorded sessions, so two real distinct events on the same port would falsely collide and the second was silently dropped. Now dedupes by `(port, end_time)`, which is always present and unique per event (regression-tested for the `session_id=0` case)
- Event entity now inherits `CuktechBaseEntity` (`CB_TYPE_CHARGE`), eliminating duplicated `device_info`/`available`/callback-registration code
- Numeric type-safety across platforms:
  - Sensor values coerced to `float` (ble_server may send strings like `"20.5"`); invalid values return `unknown` and are logged instead of raising
  - `TotalPowerSensor` no longer raises `TypeError` when a port's `power` is `None`/a string
  - Setting/port switches coerce via `int()` instead of `bool()`, fixing the `bool("0") is True` trap and `str & int` bitwise `TypeError`
- MQTT handlers now guard non-`dict` JSON payloads instead of falling into broad exception branches
- `config_flow` releases aiohttp connections via `async with` + `read()` instead of leaking them to GC

### Added
- Availability flip notification: health-check-driven `available` changes now notify entities, so the UI no longer stays stuck "available" when MQTT drops
- BLE control rollback: if both the MQTT and HTTP channels fail, the switch reverts to its prior state instead of showing a false "on" with no correction source
- Bounded charge-event history (`deque(maxlen=50)`) and explicit ordering for event-dedup key eviction
- Independent `protocol_codec` module: PIID 21 protocol bit decode/encode extracted into pure, unit-testable functions
- `strings.json` / `translations/zh-Hans.json`: added `reauth_confirm` step and `reauth_successful` abort, keeping both translation packs symmetric

### Changed
- Port subscriptions iterate `PORT_MAP` (single source of truth) instead of hardcoded `("c1","c2","c3","a")`
- Protocol switches, setting/port switch configs, countdown PIIDs and health/HTTP timeouts centralized in `const.py` (magic values removed)

### Tests
- Added 16 regression cases: charge-event dedup (incl. `session_id=0` collision), BLE rollback on both-channel failure, health-check availability change/stable notify, numeric validation (string/invalid/None/`"0"`), event-entity lifecycle

## [1.0.10] - 2026-08-16

### Fixed
- Clear port data on BLE disconnect so entities show `unknown` instead of stale readings from before the disconnect
- Screen-save-time (`select.cuktech_screen_save_time`) mapping corrected to match the Mi Home plugin: PIID 6 raw values are `1=5min, 2=10min, 3=30min, 4=always-on, 5=1min` (value 5 is the actual 1-minute encoding, not an alias of value 1; value 0 is invalid)
- `_async_wait_mqtt_ready` now uses the official `mqtt.async_wait_for_mqtt_client()` (internal 50s timeout) instead of blocking up to ~90s on a custom retry/probe publish

### Changed
- Removed unused `logging` / `_LOGGER` imports from 6 platform files (sensor/switch/binary_sensor/select/number/event)
- Removed the unused `TOPIC_PROBE` constant and `MQTT_RETRY_*` constants
- Declared dev test dependencies `pytest` / `pytest-asyncio` (`pyproject.toml [project.optional-dependencies].dev`)

### Tests
- Enabled `pytest-asyncio` (previously `@pytest.mark.asyncio` cases could not run in this environment, 13 failing, now all pass)
- Added cases: clear port data on BLE disconnect, MQTT readiness wait success/failure (ConfigEntryNotReady)
- Added nested CI workflow `.github/workflows/tests.yml` (Python 3.11/3.12 matrix)

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
