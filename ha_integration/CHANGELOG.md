# Changelog

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
