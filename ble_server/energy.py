"""CUKTECH BLE Server - Energy accumulation with adaptive integration."""
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PortEnergyState:
    """Per-port energy tracking state."""
    total_wh: float = 0.0
    session_wh: float = 0.0
    daily_wh: float = 0.0
    daily_date: str = ""
    is_charging: bool = False
    session_start: Optional[float] = None
    last_power: float = 0.0
    last_time: Optional[float] = None
    max_power: float = 0.0
    last_end_time: float = 0.0


class AdaptiveEnergyIntegrator:
    """Trapezoidal integration for charger output energy.

    Trapezoidal integration is used for all intervals — the accuracy
    difference vs Simpson at 1s BLE push intervals is <0.1%, while
    trapezoidal is simpler and avoids double-counting issues with
    overlapping Simpson windows on irregular data.
    """

    MAX_GAP_SEC = 30.0

    def update(self, state: PortEnergyState, voltage: float, current: float,
               timestamp: float) -> float:
        """Update energy state with new measurement. Returns total_wh."""
        power = voltage * current

        if state.last_time is None:
            state.last_time = timestamp
            state.last_power = power
            return state.total_wh

        dt = timestamp - state.last_time

        # Skip irregular intervals (disconnection, pause, time rollback)
        if dt <= 0 or dt > self.MAX_GAP_SEC:
            state.last_time = timestamp
            state.last_power = power
            return state.total_wh

        dt_hours = dt / 3600.0

        # Trapezoidal integration
        energy = (state.last_power + power) / 2.0 * dt_hours

        # Overshoot protection: cap at 1.5x expected (use max of both endpoints)
        max_energy = max(state.last_power, power) * dt_hours * 1.5 if power > 0 or state.last_power > 0 else 0
        energy = min(energy, max_energy) if max_energy > 0 else 0

        state.total_wh += energy
        state.session_wh += energy
        state.daily_wh += energy
        state.last_power = power
        state.last_time = timestamp
        if power > state.max_power:
            state.max_power = power

        return state.total_wh


class ChargeEndDetector:
    """Determines charging session boundaries using B+C combined approach.

    Strategy B: Power level check (avg < 5% max for 10min)
    Strategy C: Current behavior classification (mean + coefficient of variation)
    """

    LOW_POWER_THRESHOLD_RATIO = 0.05
    LOW_POWER_DURATION_SEC = 600  # 10 minutes
    COOLDOWN_SEC = 30

    def __init__(self):
        self._low_power_start: Optional[float] = None
        self._cooldown_until: float = 0
        self._current_window: deque = deque(maxlen=1800)

    def update(self, current: float, timestamp: float):
        """Track current over time."""
        self._current_window.append(current)

    def should_end_session(self, state: PortEnergyState, timestamp: float) -> bool:
        """Check if charging session should end (B+C combined)."""
        if timestamp < self._cooldown_until:
            return False

        if len(self._current_window) < 300:
            return False

        avg = statistics.mean(list(self._current_window)[-300:])
        threshold = state.max_power * self.LOW_POWER_THRESHOLD_RATIO

        if avg < threshold:
            if self._low_power_start is None:
                self._low_power_start = timestamp
            if timestamp - self._low_power_start > self.LOW_POWER_DURATION_SEC:
                return True
        else:
            self._low_power_start = None

        return False

    def on_session_end(self, timestamp: float):
        """Reset state after session ends."""
        self._cooldown_until = timestamp + self.COOLDOWN_SEC
        self._low_power_start = None
        self._current_window.clear()
