"""Tests for charge-limit wiring in the coordinator and its entities."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import AsyncContextManager

from custom_components.cuktech_charger import CuktechMQTTCoordinator
from custom_components.cuktech_charger.const import (
    CHARGE_LIMIT_PORTS,
    LIMIT_BACKEND_LOCAL,
    LIMIT_BACKEND_SERVER,
    LIMIT_MODE_ALWAYS,
    LIMIT_MODE_ONCE,
)
from custom_components.cuktech_charger.select import CuktechChargeLimitMode
from custom_components.cuktech_charger.number import CuktechChargeLimit
from custom_components.cuktech_charger.sensor import CuktechSessionEnergySensor


@pytest.fixture
def coordinator(mock_hass, mock_entry):
    return CuktechMQTTCoordinator(mock_hass, mock_entry)


async def drain_tasks(hass, _captured):
    """Await coroutines queued via async_create_task.

    conftest's mock hass deliberately closes them (to silence never-awaited
    warnings elsewhere), so cutoff tests need this to observe the real publish.
    Must be drained once per test: re-draining would replay earlier coroutines
    and inflate call counts.
    """
    pending, _captured[:] = list(_captured), []
    for coro in pending:
        await coro


@pytest.fixture
def real_task_hass(mock_hass):
    """A mock hass whose async_create_task defers instead of discarding.

    Returns (hass, captured) semantics via `hass._captured` so each test drains
    only its own queued coroutines.
    """
    captured = []
    mock_hass.async_create_task = MagicMock(side_effect=lambda coro: captured.append(coro))
    mock_hass._captured = captured
    yield mock_hass
    for coro in captured:
        coro.close()


def _mqtt_payload(port, voltage=20.0, current=3.0, active=True):
    return json.dumps({"voltage": voltage, "current": current,
                       "power": voltage * current, "active": active,
                       "protocol": "PD"})


def _msg(port, **kw):
    msg = MagicMock()
    msg.topic = f"cuktech/charger/port/{port}"
    msg.payload = _mqtt_payload(port, **kw)
    return msg


def _http_session(responses):
    """Session whose GET returns successive AsyncContextManager-wrapped responses."""
    session = MagicMock()
    session.get = MagicMock(side_effect=[AsyncContextManager(r) for r in responses])
    return session


def _ok_charge_limits(limits=None):
    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(return_value={"ok": True, "limits": limits or {}})
    return resp


def _http_404():
    resp = MagicMock()
    resp.status = 404
    resp.read = AsyncMock(return_value=b"")
    return resp


class TestBackendProbe:
    """Route B capability detection — must never break ESP32 users."""

    @pytest.mark.asyncio
    async def test_esp32_404_falls_back_to_local(self, coordinator):
        """The whole point: ESP32 has no /api/charge-limits, so we stay local."""
        with patch('custom_components.cuktech_charger.async_get_clientsession',
                   return_value=_http_session([_http_404()])):
            await coordinator._async_probe_limit_backend()
        assert coordinator.limit_backend == LIMIT_BACKEND_LOCAL

    @pytest.mark.asyncio
    async def test_probe_connection_error_falls_back_to_local(self, coordinator):
        session = MagicMock()
        session.get = MagicMock(side_effect=Exception("refused"))
        with patch('custom_components.cuktech_charger.async_get_clientsession',
                   return_value=session):
            await coordinator._async_probe_limit_backend()
        assert coordinator.limit_backend == LIMIT_BACKEND_LOCAL

    @pytest.mark.asyncio
    async def test_probe_bad_json_falls_back_to_local(self, coordinator):
        resp = MagicMock()
        resp.status = 200
        resp.json = AsyncMock(side_effect=Exception("not json"))
        resp.read = AsyncMock(return_value=b"")
        with patch('custom_components.cuktech_charger.async_get_clientsession',
                   return_value=_http_session([resp])):
            await coordinator._async_probe_limit_backend()
        assert coordinator.limit_backend == LIMIT_BACKEND_LOCAL

    @pytest.mark.asyncio
    async def test_python_server_200_delegates(self, coordinator):
        limits = {"c1": {"wh": 30.0, "mode": LIMIT_MODE_ALWAYS, "session_wh": 1.0}}
        with patch('custom_components.cuktech_charger.async_get_clientsession',
                   return_value=_http_session([_ok_charge_limits(limits)])):
            await coordinator._async_probe_limit_backend()
        assert coordinator.limit_backend == LIMIT_BACKEND_SERVER
        assert coordinator.charge_limit_wh("c1") == 30.0
        assert coordinator.charge_limit_mode("c1") == LIMIT_MODE_ALWAYS

    @pytest.mark.asyncio
    async def test_delegation_starts_poll_timer(self, coordinator):
        with patch('custom_components.cuktech_charger.async_get_clientsession',
                   return_value=_http_session([_ok_charge_limits()])):
            with patch('custom_components.cuktech_charger.async_track_time_interval') as track:
                await coordinator._async_probe_limit_backend()
        assert track.called
        coordinator._limit_poll_unsub = MagicMock()


class TestServerUrlResolution:
    """Legacy entries store the address under "host", not "server_url"."""

    def test_legacy_host_key_is_honoured(self, mock_hass, mock_entry):
        """Regression: real entries look like
        {'host': 'http://127.0.0.1:8199', 'mac': ..., 'token': ...} with no
        'server_url'. Reading only the new key silently fell back to the
        default, so a server on a non-default address degraded to local mode
        and stopped syncing with the Web UI.
        """
        mock_entry.data = {"host": "http://192.168.1.50:8199", "mac": "AA:BB"}
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        assert coord.server_url == "http://192.168.1.50:8199"

    def test_new_key_wins_over_legacy(self, mock_hass, mock_entry):
        mock_entry.data = {"server_url": "http://new:8199", "host": "http://old:8199"}
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        assert coord.server_url == "http://new:8199"

    def test_default_when_neither_present(self, mock_hass, mock_entry):
        mock_entry.data = {"mac": "AA:BB"}
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        assert coord.server_url == "http://localhost:8199"


class TestLocalMeasurement:
    """Samples are metered the same regardless of which firmware publishes them."""

    def test_ingest_accumulates_energy(self, coordinator):
        t = 1000.0
        coordinator.hass.loop.time.return_value = t
        for n in range(3601):
            coordinator._async_ingest_port(1, json.loads(_mqtt_payload("c1")))
            t += 1
            coordinator.hass.loop.time.return_value = t
        assert coordinator.session_energy_wh("c1") == pytest.approx(60.0, rel=1e-2)

    def test_inactive_port_does_not_accumulate_session(self, coordinator):
        coordinator._async_ingest_port(1, {"voltage": 20.0, "current": 3.0, "active": False})
        assert coordinator.charge_limit_charging("c1") is False

    def test_string_values_coerced(self, coordinator):
        """ble_server may publish strings like "20.5" — must not raise."""
        coordinator._async_ingest_port(1, {"voltage": "20.0", "current": "3.0", "active": True})
        assert coordinator.session_energy_wh("c1") == 0.0

    def test_missing_fields_treated_as_zero(self, coordinator):
        coordinator._async_ingest_port(1, {"active": True})
        assert coordinator.session_energy_wh("c1") == 0.0

    def test_unknown_piid_ignored(self, coordinator):
        coordinator._async_ingest_port(99, {"voltage": 20.0, "current": 3.0, "active": True})

    def test_junk_values_do_not_raise(self, coordinator):
        coordinator._async_ingest_port(1, {"voltage": "abc", "current": None, "active": True})


class TestCutoff:
    @pytest.mark.asyncio
    async def test_reaching_limit_publishes_port_off(self, real_task_hass, mock_entry):
        """The port-off topic is the one channel BOTH firmwares implement."""
        coordinator = CuktechMQTTCoordinator(real_task_hass, mock_entry)
        coordinator._charge_limits.set_limit("c1", 30, LIMIT_MODE_ALWAYS)
        t = 1000.0
        with patch('custom_components.cuktech_charger.mqtt.async_publish',
                   new=AsyncMock()) as pub:
            for n in range(3601):
                real_task_hass.loop.time.return_value = t
                coordinator._async_ingest_port(1, json.loads(_mqtt_payload("c1")))
                t += 1
                await drain_tasks(real_task_hass, real_task_hass._captured)
        assert pub.called
        payload = json.loads(pub.call_args[0][2])
        assert payload == {"port": "c1", "action": "off"}
        # identical topic to what Python server & ESP32 both subscribe to
        assert pub.call_args[0][1] == "cuktech/charger/port"

    @pytest.mark.asyncio
    async def test_cutoff_suppressed_within_retry_window(self, real_task_hass, mock_entry):
        """Past the threshold but INSIDE LIMIT_RETRY_SEC -> still one command.

        Suppression is windowed, not permanent: the engine can't know the off
        landed until either the port stops drawing power or the window expires.
        """
        coordinator = CuktechMQTTCoordinator(real_task_hass, mock_entry)
        coordinator._charge_limits.set_limit("c1", 0.1, LIMIT_MODE_ALWAYS)
        t = 1000.0
        with patch('custom_components.cuktech_charger.mqtt.async_publish',
                   new=AsyncMock()) as pub:
            # 10s of 60W = 0.167Wh > 0.1Wh limit, all inside LIMIT_RETRY_SEC(15)
            for n in range(10):
                real_task_hass.loop.time.return_value = t
                coordinator._async_ingest_port(1, json.loads(_mqtt_payload("c1")))
                t += 1
                await drain_tasks(real_task_hass, real_task_hass._captured)
        assert pub.call_count == 1

    @pytest.mark.asyncio
    async def test_cutoff_stops_once_port_actually_cuts_off(self, real_task_hass, mock_entry):
        """Realistic case: off lands, port reports inactive -> no further publishes."""
        coordinator = CuktechMQTTCoordinator(real_task_hass, mock_entry)
        coordinator._charge_limits.set_limit("c1", 0.1, LIMIT_MODE_ALWAYS)
        t = 1000.0
        with patch('custom_components.cuktech_charger.mqtt.async_publish',
                   new=AsyncMock()) as pub:
            for n in range(10):
                real_task_hass.loop.time.return_value = t
                coordinator._async_ingest_port(1, json.loads(_mqtt_payload("c1")))
                t += 1
                await drain_tasks(real_task_hass, real_task_hass._captured)
            after_cut = pub.call_count
            # port goes dead -> session ends
            real_task_hass.loop.time.return_value = t
            coordinator._async_ingest_port(1, json.loads(_mqtt_payload("c1", active=False)))
            await drain_tasks(real_task_hass, real_task_hass._captured)
            # long idle well past the retry window: still nothing new
            t += 60
            for n in range(5):
                real_task_hass.loop.time.return_value = t
                coordinator._async_ingest_port(1, json.loads(_mqtt_payload("c1", active=False)))
                t += 1
                await drain_tasks(real_task_hass, real_task_hass._captured)
        assert after_cut == 1
        assert pub.call_count == 1

    @pytest.mark.asyncio
    async def test_cutoff_retries_if_port_keeps_charging(self, real_task_hass, mock_entry):
        """Port ignored off and is still charging past the window -> retry.

        This is why suppression is windowed: if the command silently failed we
        must not abandon the user's limit forever (mirrors ble_server's
        LIMIT_RETRY_SEC behaviour).
        """
        coordinator = CuktechMQTTCoordinator(real_task_hass, mock_entry)
        coordinator._charge_limits.set_limit("c1", 0.1, LIMIT_MODE_ALWAYS)
        t = 1000.0
        with patch('custom_components.cuktech_charger.mqtt.async_publish',
                   new=AsyncMock()) as pub:
            for n in range(10):
                real_task_hass.loop.time.return_value = t
                coordinator._async_ingest_port(1, json.loads(_mqtt_payload("c1")))
                t += 1
                await drain_tasks(real_task_hass, real_task_hass._captured)
            first = pub.call_count
            # jump past the retry window, port STILL delivering power
            t += coordinator._charge_limits.LIMIT_RETRY_SEC + 1
            real_task_hass.loop.time.return_value = t
            coordinator._async_ingest_port(1, json.loads(_mqtt_payload("c1")))
            await drain_tasks(real_task_hass, real_task_hass._captured)
        assert first == 1
        assert pub.call_count == 2

    @pytest.mark.asyncio
    async def test_no_publish_without_limit(self, real_task_hass, mock_entry):
        coordinator = CuktechMQTTCoordinator(real_task_hass, mock_entry)
        with patch('custom_components.cuktech_charger.mqtt.async_publish',
                   new=AsyncMock()) as pub:
            coordinator._async_ingest_port(1, json.loads(_mqtt_payload("c1")))
            await drain_tasks(real_task_hass, real_task_hass._captured)
        assert not pub.called

    @pytest.mark.asyncio
    async def test_server_backend_meters_locally_but_does_not_enforce(
            self, real_task_hass, mock_entry):
        """Delegated mode must still meter — that's what the entities read.

        Regression for the reported "charge amount always 0 under a Python
        server": metering used to be gated on the backend along with
        enforcement, so nothing ever fed the local engine.
        """
        coordinator = CuktechMQTTCoordinator(real_task_hass, mock_entry)
        coordinator._limit_backend = LIMIT_BACKEND_SERVER
        t = 1000.0
        with patch('custom_components.cuktech_charger.mqtt.async_publish',
                   new=AsyncMock()) as pub:
            for n in range(60):  # 60W for 60s = ~1Wh
                real_task_hass.loop.time.return_value = t
                coordinator._async_ingest_port(1, json.loads(_mqtt_payload("c1")))
                t += 1
                await drain_tasks(real_task_hass, real_task_hass._captured)
        assert coordinator._charge_limits.session_wh("c1") == pytest.approx(1.0, rel=0.05)
        assert not pub.called  # server owns enforcement; we must not race it


class TestServerMirroring:
    """Delegated mode must surface the server's live progress, not zeros.

    Regression suite for the reported symptoms: "session energy always 0 and
    the BLE server's figures never showed up".
    """

    def _server_entry(self, wh=30.0, mode=LIMIT_MODE_ALWAYS, session_wh=12.5,
                      is_charging=True, fired=False):
        return {"wh": wh, "mode": mode, "session_wh": session_wh,
                "is_charging": is_charging, "fired": fired}

    @pytest.mark.asyncio
    async def test_probe_adopts_full_snapshot(self, coordinator):
        limits = {"c1": self._server_entry()}
        with patch('custom_components.cuktech_charger.async_get_clientsession',
                   return_value=_http_session([_ok_charge_limits(limits)])):
            await coordinator._async_probe_limit_backend()
        coordinator._limit_poll_unsub = MagicMock()
        # config
        assert coordinator.charge_limit_wh("c1") == 30.0
        assert coordinator.charge_limit_mode("c1") == LIMIT_MODE_ALWAYS
        # live progress — this is what used to stay stuck at 0
        assert coordinator.session_energy_wh("c1") == 12.5
        assert coordinator.charge_limit_charging("c1") is True
        assert coordinator.charge_limit_fired("c1") is False
        # remaining derived from the SERVER's figures
        assert coordinator.charge_limit_remaining_wh("c1") == 17.5

    @pytest.mark.asyncio
    async def test_poll_updates_live_progress(self, coordinator):
        """Progress advances between polls without any MQTT sample at all."""
        coordinator._limit_backend = LIMIT_BACKEND_SERVER
        resp = MagicMock()
        resp.status = 200
        resp.json = AsyncMock(return_value={
            "ok": True, "limits": {"c1": self._server_entry(session_wh=25.0)}})
        with patch('custom_components.cuktech_charger.async_get_clientsession',
                   return_value=_http_session([resp])):
            await coordinator._async_poll_server_limits(None)
        assert coordinator.session_energy_wh("c1") == 25.0

    @pytest.mark.asyncio
    async def test_poll_notifies_port_callbacks(self, coordinator):
        """The session sensor is CB_TYPE_PORT; a config-only notify would
        leave it stale until the next MQTT frame."""
        coordinator._limit_backend = LIMIT_BACKEND_SERVER
        port_cb, settings_cb = MagicMock(), MagicMock()
        coordinator.register_port_callback(port_cb)
        coordinator.register_settings_callback(settings_cb)
        resp = MagicMock()
        resp.status = 200
        resp.json = AsyncMock(return_value={
            "ok": True, "limits": {"c1": self._server_entry()}})
        with patch('custom_components.cuktech_charger.async_get_clientsession',
                   return_value=_http_session([resp])):
            await coordinator._async_poll_server_limits(None)
        assert port_cb.called
        assert settings_cb.called

    def test_local_mode_ignores_server_snapshot(self, coordinator):
        """Stale server data must never leak into local mode's display."""
        coordinator._server_limits = {"c1": self._server_entry(session_wh=99.0)}
        coordinator._limit_backend = LIMIT_BACKEND_LOCAL
        assert coordinator.session_energy_wh("c1") == 0.0
        assert coordinator.charge_limit_wh("c1") == 0.0

    def test_server_snapshot_does_not_leak_into_local_engine(self, coordinator):
        """Server wh must not become a locally-enforced limit.

        Otherwise the local `once` consumption logic would zero it and the
        entity would flip to 0 while the server still reports the limit.
        """
        coordinator._limit_backend = LIMIT_BACKEND_SERVER
        coordinator._apply_server_limits({"c1": self._server_entry()})
        assert coordinator._charge_limits.get_limit("c1").wh == 0.0
        # a local session end cannot consume the mirrored value
        coordinator._charge_limits.end_session("c1", "port_off", 2000.0)
        assert coordinator.charge_limit_wh("c1") == 30.0

    @pytest.mark.asyncio
    async def test_fallback_adopts_last_snapshot_for_all_ports(self, coordinator):
        """When delegation breaks, other ports keep their displayed limits."""
        coordinator._limit_backend = LIMIT_BACKEND_SERVER
        coordinator._apply_server_limits({
            "c1": self._server_entry(wh=30.0),
            "c2": self._server_entry(wh=15.0),
        })
        resp = MagicMock()
        resp.status = 400
        resp.read = AsyncMock(return_value=b"")
        session = MagicMock()
        session.post = MagicMock(return_value=AsyncContextManager(resp))
        coordinator._limit_poll_unsub = MagicMock()
        with patch('custom_components.cuktech_charger.async_get_clientsession',
                   return_value=session):
            await coordinator.async_set_charge_limit("c1", 5.0, LIMIT_MODE_ONCE)
        assert coordinator.limit_backend == LIMIT_BACKEND_LOCAL
        assert coordinator.charge_limit_wh("c1") == 5.0   # the user's new value
        assert coordinator.charge_limit_wh("c2") == 15.0  # preserved from server
        assert coordinator._limit_poll_unsub is None      # polling stopped

    def test_server_snapshot_tolerates_junk(self, coordinator):
        coordinator._limit_backend = LIMIT_BACKEND_SERVER
        coordinator._apply_server_limits({
            "c1": {"wh": "abc", "mode": "bogus", "session_wh": None},
            "c2": "not-a-dict",
            "unknown": {"wh": 5},
        })
        # falls back to the local engine's defaults rather than raising
        assert coordinator.charge_limit_wh("c1") == 0.0
        assert coordinator.charge_limit_mode("c1") == LIMIT_MODE_ONCE
        assert coordinator.session_energy_wh("c1") == 0.0

    def test_client_only_snapshot_has_no_server_fields(self, coordinator):
        coordinator._limit_backend = LIMIT_BACKEND_SERVER
        with patch('custom_components.cuktech_charger.async_get_clientsession',
                   return_value=MagicMock()):
            coordinator._apply_server_limits({"c1": {"wh": 8.0, "mode": LIMIT_MODE_ONCE}})
        assert coordinator.charge_limit_wh("c1") == 8.0
        # no session_wh in the payload -> local figure (0) is used, no crash
        assert coordinator.session_energy_wh("c1") == 0.0
        assert coordinator.charge_limit_remaining_wh("c1") == 8.0


class TestPersistence:
    @pytest.mark.asyncio
    async def test_set_limit_persists(self, coordinator):
        await coordinator.async_set_charge_limit("c1", 30, LIMIT_MODE_ALWAYS)
        assert coordinator.charge_limit_wh("c1") == 30.0
        # (was a bare expression before — asserted nothing)
        assert coordinator._limits_store._data["limits"]["c1"]["wh"] == 30.0

    @pytest.mark.asyncio
    async def test_once_consumption_is_persisted(self, real_task_hass, mock_entry):
        """A fired `once` limit must not resurrect after an HA restart.

        Regression: limits were only saved from async_set_charge_limit, but
        consumption happens inside the engine (end_session), so disk kept the
        old value and the next restart silently re-armed a one-shot limit.
        """
        coord = CuktechMQTTCoordinator(real_task_hass, mock_entry)
        await coord.async_set_charge_limit("c1", 0.1, LIMIT_MODE_ONCE)
        assert coord._limits_store._data["limits"]["c1"]["wh"] == 0.1

        t = 1000.0
        with patch('custom_components.cuktech_charger.mqtt.async_publish',
                   new=AsyncMock()):
            for n in range(10):  # 10s @60W = 0.167Wh > 0.1Wh -> fires
                real_task_hass.loop.time.return_value = t
                coord._async_ingest_port(1, json.loads(_mqtt_payload("c1")))
                t += 1
                await drain_tasks(real_task_hass, real_task_hass._captured)
            # port goes dead -> session ends -> once limit consumed
            real_task_hass.loop.time.return_value = t
            coord._async_ingest_port(1, json.loads(_mqtt_payload("c1", active=False)))
            await drain_tasks(real_task_hass, real_task_hass._captured)

        assert coord.charge_limit_wh("c1") == 0.0                       # consumed
        assert coord._limits_store._data["limits"]["c1"]["wh"] == 0.0   # persisted
        assert coord._charge_limits.get_limit("c1").mode == LIMIT_MODE_ONCE

        # Simulate the restart: reload from the same store payload.
        reborn = CuktechMQTTCoordinator(real_task_hass, mock_entry)
        reborn._limits_store._data = coord._limits_store._data
        await reborn._async_load_limits()
        assert reborn.charge_limit_wh("c1") == 0.0, "consumed once-limit came back"

    @pytest.mark.asyncio
    async def test_consumption_notifies_settings_entities(self, real_task_hass, mock_entry):
        """Consumption happens on the port path, but number/select listen on
        settings — without an explicit notify they'd keep showing the old limit
        until the next settings frame (20-90s on ESP32)."""
        coord = CuktechMQTTCoordinator(real_task_hass, mock_entry)
        settings_cb = MagicMock()
        coord.register_settings_callback(settings_cb)
        await coord.async_set_charge_limit("c1", 0.1, LIMIT_MODE_ONCE)
        settings_cb.reset_mock()

        t = 1000.0
        with patch('custom_components.cuktech_charger.mqtt.async_publish',
                   new=AsyncMock()):
            for n in range(10):
                real_task_hass.loop.time.return_value = t
                coord._async_ingest_port(1, json.loads(_mqtt_payload("c1")))
                t += 1
                await drain_tasks(real_task_hass, real_task_hass._captured)
            real_task_hass.loop.time.return_value = t
            coord._async_ingest_port(1, json.loads(_mqtt_payload("c1", active=False)))
            await drain_tasks(real_task_hass, real_task_hass._captured)

        assert settings_cb.called, "consumed limit never reached the entities"

    @pytest.mark.asyncio
    async def test_no_notify_when_nothing_changes(self, real_task_hass, mock_entry):
        """The 1Hz sample path must not spam entity refreshes."""
        coord = CuktechMQTTCoordinator(real_task_hass, mock_entry)
        await coord.async_set_charge_limit("c1", 500.0, LIMIT_MODE_ALWAYS)
        settings_cb = MagicMock()
        coord.register_settings_callback(settings_cb)
        t = 1000.0
        with patch('custom_components.cuktech_charger.mqtt.async_publish',
                   new=AsyncMock()):
            for n in range(30):
                real_task_hass.loop.time.return_value = t
                coord._async_ingest_port(1, json.loads(_mqtt_payload("c1")))
                t += 1
                await drain_tasks(real_task_hass, real_task_hass._captured)
        assert not settings_cb.called

    @pytest.mark.asyncio
    async def test_always_limit_survives_reload(self, real_task_hass, mock_entry):
        """The counterpart: `always` must still be there after a restart."""
        coord = CuktechMQTTCoordinator(real_task_hass, mock_entry)
        await coord.async_set_charge_limit("c1", 25.0, LIMIT_MODE_ALWAYS)
        reborn = CuktechMQTTCoordinator(real_task_hass, mock_entry)
        reborn._limits_store._data = coord._limits_store._data
        await reborn._async_load_limits()
        assert reborn.charge_limit_wh("c1") == 25.0
        assert reborn.charge_limit_mode("c1") == LIMIT_MODE_ALWAYS

    @pytest.mark.asyncio
    async def test_unchanged_limits_are_not_rewritten(self, real_task_hass, mock_entry):
        """The change-detector must not hit the disk on every 1Hz sample."""
        coord = CuktechMQTTCoordinator(real_task_hass, mock_entry)
        await coord.async_set_charge_limit("c1", 500.0, LIMIT_MODE_ALWAYS)
        coord._limits_store.async_save = AsyncMock()
        t = 1000.0
        with patch('custom_components.cuktech_charger.mqtt.async_publish',
                   new=AsyncMock()):
            for n in range(30):  # far below the limit, nothing consumes
                real_task_hass.loop.time.return_value = t
                coord._async_ingest_port(1, json.loads(_mqtt_payload("c1")))
                t += 1
                await drain_tasks(real_task_hass, real_task_hass._captured)
        assert not coord._limits_store.async_save.called

    @pytest.mark.asyncio
    async def test_load_restores_limits_and_totals(self, coordinator):
        coordinator._limits_store._data = {
            "limits": {"c1": {"wh": 25.0, "mode": LIMIT_MODE_ALWAYS}},
            "totals": {"c1": 123.4},
        }
        await coordinator._async_load_limits()
        assert coordinator.charge_limit_wh("c1") == 25.0
        assert coordinator.total_energy_wh("c1") == 123.4

    @pytest.mark.asyncio
    async def test_load_handles_missing_store_file(self, coordinator):
        await coordinator._async_load_limits()
        assert coordinator.charge_limit_wh("c1") == 0.0

    @pytest.mark.asyncio
    async def test_load_survives_corrupt_store(self, coordinator):
        coordinator._limits_store._data = {"limits": "garbage", "totals": "junk"}
        await coordinator._async_load_limits()
        assert coordinator.charge_limit_wh("c1") == 0.0

    @pytest.mark.asyncio
    async def test_store_failure_does_not_raise(self, coordinator):
        coordinator._limits_store.async_load = AsyncMock(side_effect=Exception("disk"))
        await coordinator._async_load_limits()  # must not raise
        assert coordinator.charge_limit_wh("c1") == 0.0

    @pytest.mark.asyncio
    async def test_save_failure_does_not_raise(self, coordinator):
        coordinator._limits_store.async_save = AsyncMock(side_effect=Exception("disk"))
        await coordinator._async_save_limits()  # must not raise


class TestServerDelegationWrite:
    @pytest.mark.asyncio
    async def test_set_limit_posts_to_server(self, coordinator):
        coordinator._limit_backend = LIMIT_BACKEND_SERVER
        resp = MagicMock()
        resp.status = 200
        resp.json = AsyncMock(return_value={
            "ok": True, "limits": {"c1": {"wh": 40.0, "mode": LIMIT_MODE_ONCE}}})
        session = MagicMock()
        session.post = MagicMock(return_value=AsyncContextManager(resp))
        with patch('custom_components.cuktech_charger.async_get_clientsession',
                   return_value=session):
            await coordinator.async_set_charge_limit("c1", 40, LIMIT_MODE_ONCE)
        body = session.post.call_args[1]["json"]
        assert body == {"port": "c1", "wh": 40.0, "mode": LIMIT_MODE_ONCE}
        # authoritative value adopted from the response
        assert coordinator.charge_limit_wh("c1") == 40.0

    @pytest.mark.asyncio
    async def test_server_rejection_falls_back_to_local(self, coordinator):
        """A failed REST write must not silently drop the user's intent."""
        coordinator._limit_backend = LIMIT_BACKEND_SERVER
        resp = MagicMock()
        resp.status = 400
        resp.read = AsyncMock(return_value=b"")
        session = MagicMock()
        session.post = MagicMock(return_value=AsyncContextManager(resp))
        with patch('custom_components.cuktech_charger.async_get_clientsession',
                   return_value=session):
            await coordinator.async_set_charge_limit("c1", 25, LIMIT_MODE_ALWAYS)
        assert coordinator.charge_limit_wh("c1") == 25.0

    @pytest.mark.asyncio
    async def test_unknown_port_rejected(self, coordinator):
        await coordinator.async_set_charge_limit("nope", 10)
        assert coordinator.charge_limit_wh("c1") == 0.0

    @pytest.mark.asyncio
    async def test_poll_refreshes_server_limits(self, coordinator):
        coordinator._limit_backend = LIMIT_BACKEND_SERVER
        resp = MagicMock()
        resp.status = 200
        resp.json = AsyncMock(return_value={
            "ok": True, "limits": {"c1": {"wh": 55.0, "mode": LIMIT_MODE_ALWAYS}}})
        with patch('custom_components.cuktech_charger.async_get_clientsession',
                   return_value=_http_session([resp])):
            await coordinator._async_poll_server_limits(None)
        assert coordinator.charge_limit_wh("c1") == 55.0

    @pytest.mark.asyncio
    async def test_poll_failure_is_silent(self, coordinator):
        coordinator._limit_backend = LIMIT_BACKEND_SERVER
        session = MagicMock()
        session.get = MagicMock(side_effect=Exception("timeout"))
        with patch('custom_components.cuktech_charger.async_get_clientsession',
                   return_value=session):
            await coordinator._async_poll_server_limits(None)  # must not raise

    @pytest.mark.asyncio
    async def test_local_backend_poll_is_noop(self, coordinator):
        with patch('custom_components.cuktech_charger.async_get_clientsession') as cs:
            await coordinator._async_poll_server_limits(None)
        assert not cs.called


class TestShutdown:
    @pytest.mark.asyncio
    async def test_unload_preserves_once_limit(self, coordinator):
        coordinator._charge_limits.set_limit("c1", 30, LIMIT_MODE_ONCE)
        coordinator._charge_limits.ingest("c1", 20.0, 3.0, True, 1000.0)
        coordinator._limit_poll_unsub = MagicMock()
        await coordinator.async_unload()
        # session closed, but the user's once-limit survived the restart
        assert coordinator.charge_limit_charging("c1") is False
        assert coordinator.charge_limit_wh("c1") == 30.0


class TestLinkLoss:
    """A BLE blip must not eat a `once` limit (R8 / ble_manager.py:900)."""

    def test_ble_disconnect_preserves_once_limit(self, coordinator):
        """Regression: the local engine used to never produce LINK_LOSS.

        Modelled as the full blip, because that is where the damage happened:
        the link drops, and once it comes back the port reads inactive. With no
        LINK_LOSS the session was still open, so that later inactive sample was
        attributed USER_OFF and consumed the user's freshly armed one-shot.
        """
        coordinator._charge_limits.set_limit("c1", 30, LIMIT_MODE_ONCE)
        coordinator._charge_limits.ingest("c1", 20.0, 3.0, True, 1000.0)
        assert coordinator.charge_limit_charging("c1") is True

        coordinator._ble_connected = True
        coordinator._sync_ble_state(False)          # BLE drops

        # link returns; the port now reports inactive (as it did during the blip)
        coordinator._ble_connected = False
        coordinator._sync_ble_state(True)
        coordinator._charge_limits.ingest("c1", 0.0, 0.0, False, 1100.0)

        # The limit is the thing that must survive; assert it first so a failure
        # reports the actual damage rather than a secondary symptom.
        assert coordinator.charge_limit_wh("c1") == 30.0, "link loss consumed the limit"
        assert coordinator.charge_limit_charging("c1") is False

    def test_ble_disconnect_preserves_always_limit(self, coordinator):
        coordinator._charge_limits.set_limit("c1", 30, LIMIT_MODE_ALWAYS)
        coordinator._charge_limits.ingest("c1", 20.0, 3.0, True, 1000.0)
        coordinator._ble_connected = True
        coordinator._sync_ble_state(False)
        assert coordinator.charge_limit_wh("c1") == 30.0

    def test_real_port_off_still_consumes_once(self, coordinator):
        """Contrast case: an actual port-off is a real termination."""
        coordinator._charge_limits.set_limit("c1", 30, LIMIT_MODE_ONCE)
        coordinator._charge_limits.ingest("c1", 20.0, 3.0, True, 1000.0)
        coordinator._charge_limits.ingest("c1", 0.0, 0.0, False, 1010.0)
        assert coordinator.charge_limit_wh("c1") == 0.0

    def test_link_loss_only_on_transition(self, coordinator):
        """Repeated disconnected status messages must not re-close sessions."""
        coordinator._charge_limits.set_limit("c1", 30, LIMIT_MODE_ONCE)
        coordinator._charge_limits.ingest("c1", 20.0, 3.0, True, 1000.0)
        coordinator._ble_connected = False           # already disconnected
        coordinator._sync_ble_state(False)
        # nothing to close, and the limit is untouched either way
        assert coordinator.charge_limit_wh("c1") == 30.0


class TestModePreservation:
    """Omitting `mode` must keep the current one, as the server does."""

    @pytest.mark.asyncio
    async def test_number_write_keeps_always_mode(self, coordinator):
        """The number entity only supplies a threshold — it must not silently
        reset an `always` limit back to `once` (server merge semantics)."""
        await coordinator.async_set_charge_limit("c1", 30, LIMIT_MODE_ALWAYS)
        await coordinator.async_set_charge_limit("c1", 12.5)   # mode omitted
        assert coordinator.charge_limit_wh("c1") == 12.5
        assert coordinator.charge_limit_mode("c1") == LIMIT_MODE_ALWAYS

    @pytest.mark.asyncio
    async def test_explicit_mode_still_applies(self, coordinator):
        await coordinator.async_set_charge_limit("c1", 30, LIMIT_MODE_ALWAYS)
        await coordinator.async_set_charge_limit("c1", 30, LIMIT_MODE_ONCE)
        assert coordinator.charge_limit_mode("c1") == LIMIT_MODE_ONCE

    def test_engine_set_limit_preserves_mode(self, coordinator):
        tracker = coordinator._charge_limits
        tracker.set_limit("c1", 30, LIMIT_MODE_ALWAYS)
        tracker.set_limit("c1", 5)                            # mode omitted
        assert tracker.get_limit("c1").mode == LIMIT_MODE_ALWAYS
        assert tracker.get_limit("c1").wh == 5.0

    def test_disabling_keeps_mode_for_next_time(self, coordinator):
        tracker = coordinator._charge_limits
        tracker.set_limit("c1", 30, LIMIT_MODE_ALWAYS)
        tracker.set_limit("c1", 0)                            # disable
        assert tracker.get_limit("c1").wh == 0.0
        assert tracker.get_limit("c1").mode == LIMIT_MODE_ALWAYS


class TestEntities:
    """The 12 new entities (4 ports × number/select/sensor)."""

    def test_entity_counts(self):
        assert len(CHARGE_LIMIT_PORTS) == 4

    def test_number_reads_and_writes(self, coordinator, mock_entry):
        coordinator._charge_limits.set_limit("c1", 30, LIMIT_MODE_ALWAYS)
        entity = CuktechChargeLimit(coordinator, mock_entry, "c1", "C1 charge limit")
        assert entity.native_value == 30.0
        assert entity._attr_unique_id.endswith("_charge_limit_c1")
        assert entity.extra_state_attributes["mode"] == LIMIT_MODE_ALWAYS

    @pytest.mark.asyncio
    async def test_number_write_routes_to_coordinator(self, coordinator, mock_entry):
        entity = CuktechChargeLimit(coordinator, mock_entry, "c1", "C1 charge limit")
        await entity.async_set_native_value(12.5)
        assert coordinator.charge_limit_wh("c1") == 12.5

    def test_select_reads_current_mode(self, coordinator, mock_entry):
        coordinator._charge_limits.set_limit("c1", 30, LIMIT_MODE_ALWAYS)
        entity = CuktechChargeLimitMode(coordinator, mock_entry, "c1", "C1 charge limit")
        assert entity.current_option == LIMIT_MODE_ALWAYS
        assert entity._attr_unique_id.endswith("_charge_limit_mode_c1")

    @pytest.mark.asyncio
    async def test_select_write_preserves_wh(self, coordinator, mock_entry):
        coordinator._charge_limits.set_limit("c1", 30, LIMIT_MODE_ONCE)
        entity = CuktechChargeLimitMode(coordinator, mock_entry, "c1", "C1 charge limit")
        await entity.async_select_option(LIMIT_MODE_ALWAYS)
        assert coordinator.charge_limit_wh("c1") == 30.0
        assert coordinator.charge_limit_mode("c1") == LIMIT_MODE_ALWAYS

    @pytest.mark.asyncio
    async def test_select_rejects_unknown_option(self, coordinator, mock_entry):
        coordinator._charge_limits.set_limit("c1", 30, LIMIT_MODE_ONCE)
        entity = CuktechChargeLimitMode(coordinator, mock_entry, "c1", "C1 charge limit")
        await entity.async_select_option("bogus")
        assert coordinator.charge_limit_mode("c1") == LIMIT_MODE_ONCE

    def test_sensor_reports_session_energy(self, coordinator, mock_entry):
        coordinator._charge_limits.ingest("c1", 20.0, 3.0, True, 1000.0)
        coordinator._charge_limits.ingest("c1", 20.0, 3.0, True, 1001.0)
        entity = CuktechSessionEnergySensor(coordinator, mock_entry, "c1", "C1 charge limit")
        assert entity.native_value > 0.0
        attrs = entity.extra_state_attributes
        assert attrs["is_charging"] is True
        assert attrs["remaining_wh"] is None  # no limit armed

    def test_sensor_remaining_wh_with_limit(self, coordinator, mock_entry):
        coordinator._charge_limits.set_limit("c1", 100, LIMIT_MODE_ALWAYS)
        coordinator._charge_limits.ingest("c1", 20.0, 3.0, True, 1000.0)
        coordinator._charge_limits.ingest("c1", 20.0, 3.0, True, 1001.0)
        entity = CuktechSessionEnergySensor(coordinator, mock_entry, "c1", "C1 charge limit")
        assert entity.extra_state_attributes["remaining_wh"] < 100.0
        assert entity.extra_state_attributes["limit_mode"] == LIMIT_MODE_ALWAYS

    def test_unique_ids_stable_per_port(self, coordinator, mock_entry):
        """Entity registry must not collide across the 4 ports."""
        ids = {
            CuktechChargeLimit(coordinator, mock_entry, p, "x")._attr_unique_id
            for p in CHARGE_LIMIT_PORTS
        }
        assert len(ids) == 4

    def test_energy_sensor_state_class_is_ha_valid(self, coordinator, mock_entry):
        """device_class=ENERGY forbids MEASUREMENT and requires TOTAL*.

        Real HA logged, per entity, at startup:
          "using state class 'measurement' which is impossible considering
           device class ('energy'); expected None or one of
           'total_increasing', 'total'"
        This pins the pair so the invalid combination can't return.
        """
        entity = CuktechSessionEnergySensor(coordinator, mock_entry, "c1", "C1")
        assert entity._attr_state_class in ("total", "total_increasing")
        # ...and TOTAL_INCREASING specifically would be wrong for a counter that
        # drops to 0 every session, so the only correct choice is plain TOTAL.
        assert entity._attr_state_class == "total"
        assert entity._attr_device_class == "energy"

    def test_energy_sensor_reports_wh(self, coordinator, mock_entry):
        entity = CuktechSessionEnergySensor(coordinator, mock_entry, "c1", "C1")
        assert entity._attr_native_unit_of_measurement == "Wh"
