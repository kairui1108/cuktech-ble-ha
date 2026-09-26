# Changelog

## [1.1.1] - 2026-09-25

### Fixed
- **Charge-limit entities showed 0 and never reflected the BLE server.** Metering was gated on the backend together with enforcement, so under a Python server (delegated mode) no port sample ever reached the local engine and session energy stayed 0 forever. `_async_ingest_port` no longer returns early — only *enforcement* is backend-specific
- **Invalid `(device_class, state_class)` pair on the session-energy sensor**: HA rejects `measurement` together with `device_class=energy` and logged a warning per port at startup. Switched to `TOTAL`, which is the documented class for a resettable total (the value drops to 0 each session, so `TOTAL_INCREASING` would also be wrong)
- **Legacy config entries ignored their stored address.** Entries created by older versions keep the URL under `host` (alongside `mac`/`token`/`ble_key`), not `server_url`; reading only the new key silently fell back to `http://localhost:8199`, so a server on any other address degraded to local mode and stopped syncing. Resolution is now `server_url` → `host` → default
- **A fired `once` limit resurrected after an HA restart.** Limits were only persisted from `async_set_charge_limit`, but a one-shot limit is consumed *inside* the engine (`end_session`), with no user action — so disk kept the pre-firing value and the next restart silently re-armed a limit that had already fired. Limits are now saved whenever the snapshot changes (compared per sample; writes only on an actual change). `ble_server` already persisted consumption via `_persist_limits_async`, so this also restores parity between the two backends
- Delegated mode adopted only `wh`/`mode` from `GET /api/charge-limits`, discarding the live fields. It now mirrors the full server snapshot (`session_wh` / `is_charging` / `fired`), so entities match the Web UI and progress survives HA restarts
- The server snapshot is kept separate from the local engine instead of being written into it: otherwise a local session end would consume a mirrored `once` limit and blank the entity while the server still reported it
- The delegated poll now notifies port callbacks too (not just settings): the session-energy sensor is `CB_TYPE_PORT` and previously stayed stale until the next MQTT frame
- Poll interval 15s → 5s so a mirrored `session_wh` doesn't visibly tick in steps (`GET` reads in-memory state, no I/O)
- Backend choice is now diagnosable: the probe reports at INFO (was DEBUG, invisible at the default log level) and logs *why* it fell back to local; the active backend is also exposed as a `backend` entity attribute
- **The limit entities didn't refresh when a `once` limit was consumed in local mode.** Consumption happens on the port-callback path while number/select listen on settings callbacks, so the UI kept showing the old limit until the next `settings` frame (20-90s on ESP32). The change-detection hook now also notifies the settings listeners
- A failed REST write now hands ownership to HA explicitly (`_adopt_server_limits_locally`), preserving the other ports' limits and stopping the poll, instead of leaving the display split between two sources
- **A BLE blip consumed a `once` limit.** The local engine could never produce `END_REASON_LINK_LOSS` (only the tests did), so after a reconnect the port reading inactive was attributed `USER_OFF` and the freshly armed one-shot was eaten — while the Python side preserves it (`ble_manager.py:900` closes sessions as `link_loss` on disconnect). The BLE-disconnect transition now closes sessions with that reason
- **Editing the threshold silently reset the mode.** In local mode `set_limit(port, wh)` without a mode normalised to the default `once`, turning a user's `always` back into `once`; the number entity supplies only a threshold, so this happened on every threshold edit. An omitted mode now keeps the port's current one, matching the server's merge semantics (`ha_server.py:766`)

### Added
- **Charge limits** (充电量限额): auto power-off at a configured Wh per port — 12 new entities across three platforms:
  - `number._{port}_charge_limit` — threshold in Wh, `0` disables (range 0-1000, step 0.5)
  - `select._{port}_charge_limit_mode` — `once` (clears after firing) / `always` (re-arms every session)
  - `sensor._{port}_session_energy` — energy delivered in the current session, with `remaining_wh` / `limit_wh` / `limit_mode` / `is_charging` / `total_wh` attributes
- New `energy_engine` module: pure-python port of the server's `AdaptiveEnergyIntegrator` (trapezoidal V×I integration), `ChargeEndDetector` and `ChargeLimitTracker`, following the same extraction pattern as `protocol_codec` — core logic unit-tested without a HA runtime
- **Backend auto-detection** so limits work under both firmwares with no firmware change:
  - Python BLE server answers `GET /api/charge-limits` with 200 → `server` mode: configuration is delegated via REST and stays two-way synced with the Web UI
  - ESP32 has no such endpoint (see `esp32_ble/main/http_server.c` URI table) and returns 404 → `local` mode: HA meters the same 1Hz MQTT port samples itself. The ESP32 does no energy accounting at all, so this is its only viable path
  - Probe failures are non-fatal; local mode is fully functional on its own
- Local mode persistence via HA `Store` (`limits` + long-run `totals`), restoring across restarts

### Changed
- Session energy uses `SensorStateClass.TOTAL`: the value legitimately drops to 0 at each session start, so `TOTAL_INCREASING` would be wrong, and `MEASUREMENT` is rejected outright by HA for `device_class=energy`

### Notes
- Cutoff reuses the existing MQTT port-control topic (`{"port":..,"action":"off"}`), which **both** the Python server and the ESP32 firmware already subscribe to — hence no firmware change is required
- `once` limits survive BLE hiccups and HA restarts (`END_REASONS_PRESERVING_LIMIT`); only a real session termination consumes them
- Overshoot is ~0.05 Wh at a 100 W load; a failed off command is retried after `LIMIT_RETRY_SEC` (15s) rather than silencing the user's limit
- Documented that the two configurations should be managed in one place: with the Python server prefer delegated mode, otherwise Web UI and HA enforce independently

### Tests
- 123 new cases: 58 for the energy engine (integration math, `MAX_GAP_SEC` retained-frame guard, session boundaries, once/always semantics, junk-input handling) and 65 for coordinator wiring (backend probe incl. ESP32 404 fallback, link-loss limit preservation, cutoff idempotence and retry, mode preservation, Store roundtrip/corruption, once-consumption persistence, entity refresh + unique-id stability, delegated-mode snapshot mirroring and take-over)

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
