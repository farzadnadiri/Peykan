"""Correlated vehicle driving state.

Without this, `SimThread`'s per-signal independent random draws mean e.g.
ENGINE_SPEED and THROTTLE_POSITION have zero relationship tick to tick --
not much like a moving vehicle. This module holds a few "driver input"-like
base variables that evolve smoothly over time, plus a table mapping DBC
signal names to values derived from them.

The same goes for the cabin/environment signals (doors, seatbelts, lights,
wipers, crash/fault flags): drawn independently at random, a car doing
60 km/h reported CRASH_DETECTED, open doors and a 61 degC cabin every few
frames, which an LLM reading the bus (rightly) reports as an emergency. They
now follow slow-changing situational state instead -- a normal, uneventful
drive -- and abnormal values only appear via fault presets (`faults.py`).
`SimThread`'s random draws remain only as a fallback for signals a custom
DBC adds that this table doesn't know.

`tick()` is a pure function (state in, state out) so it's testable without
any threading or timing; `VehicleState` just calls it from a background
loop and exposes a thread-safe snapshot.
"""
import math
import random
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Dict, Optional

IDLE_RPM = 800.0
AMBIENT_TEMP_C = 20.0
# vehicle.dbc declares ENGINE_TEMP's range as -40..127.5, but it's only 8
# bits at scale 0.5/offset -40, so the actual encodable ceiling is
# 255*0.5-40 = 87.5 -- a value above that raises in cantools' Message.encode
# rather than just clamping. Target and clamp below that ceiling so the
# simulator doesn't silently stop sending ENGINE_STATUS once warmed up.
OPERATING_TEMP_C = 85.0
ENGINE_TEMP_MAX_C = 87.5
CABIN_SETPOINT_C = 21.5  # climate control target
AUTO_LOCK_KPH = 15.0  # doors lock once the vehicle passes this speed
HEADLIGHT_LUX = 80.0  # automatic headlights below this ambient light
CHARGING_VOLTAGE = 14.2  # alternator output with the engine running
RESTING_VOLTAGE = 12.6  # healthy battery, engine off

# WIPER_STATUS choice values (vehicle.dbc VAL_ table).
WIPER_OFF = 0
WIPER_INTERMITTENT_2 = 2
WIPER_LOW_SPEED = 4
WIPER_HIGH_SPEED = 5


@dataclass
class DrivingState:
    throttle_pct: float = 0.0
    rpm: float = IDLE_RPM
    speed_kph: float = 0.0
    engine_temp_c: float = AMBIENT_TEMP_C
    fuel_pct: float = 80.0
    battery_v: float = RESTING_VOLTAGE
    # Cabin / environment situation: changes slowly or not at all.
    ambient_light_lux: float = 180.0
    raining: bool = False
    interior_temp_c: float = AMBIENT_TEMP_C
    locked: bool = False
    passenger_present: bool = False


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _approach(current: float, target: float, time_constant_s: float, dt_s: float) -> float:
    """Move `current` toward `target` with exponential-decay lag.

    Standard first-order-lag smoothing: `time_constant_s` is roughly how
    long it takes to close ~63% of the gap, independent of how often
    `tick()` is actually called.
    """
    alpha = 1 - math.exp(-dt_s / time_constant_s)
    return current + (target - current) * alpha


def tick(state: DrivingState, dt_s: float) -> DrivingState:
    """Advance driving state by `dt_s` seconds. Not real vehicle physics --
    just enough correlation that related signals move together."""
    throttle = state.throttle_pct + random.uniform(-8, 8) * dt_s
    if random.random() < 0.3 * dt_s:  # an occasional bigger press/lift
        throttle += random.uniform(-30, 30)
    throttle = _clamp(throttle, 0.0, 100.0)

    rpm = _approach(state.rpm, IDLE_RPM + throttle * 45, time_constant_s=2.0, dt_s=dt_s)
    target_speed = max(0.0, (rpm - IDLE_RPM) * 0.045)
    speed = _approach(state.speed_kph, target_speed, time_constant_s=3.0, dt_s=dt_s)
    engine_temp = _approach(
        state.engine_temp_c, OPERATING_TEMP_C, time_constant_s=60.0, dt_s=dt_s
    ) + random.uniform(-0.3, 0.3)
    fuel = state.fuel_pct - (0.002 + throttle * 0.0002) * dt_s
    target_v = CHARGING_VOLTAGE if rpm > IDLE_RPM / 2 else RESTING_VOLTAGE
    battery = _approach(state.battery_v, target_v, time_constant_s=5.0, dt_s=dt_s)
    battery += random.uniform(-0.03, 0.03)

    light = state.ambient_light_lux + random.uniform(-4, 4) * dt_s
    if random.random() < 0.01 * dt_s:  # tunnel, overpass, clouds clearing...
        light = random.uniform(5, 255)
    raining = state.raining
    if random.random() < 0.003 * dt_s:  # weather changes every few minutes
        raining = not raining
    interior = _approach(
        state.interior_temp_c, CABIN_SETPOINT_C, time_constant_s=120.0, dt_s=dt_s
    ) + random.uniform(-0.02, 0.02)

    return DrivingState(
        throttle_pct=throttle,
        rpm=_clamp(rpm, 0.0, 16383.0),
        speed_kph=_clamp(speed, 0.0, 300.0),
        engine_temp_c=_clamp(engine_temp, -40.0, ENGINE_TEMP_MAX_C),
        fuel_pct=_clamp(fuel, 0.0, 100.0),
        battery_v=_clamp(battery, 0.0, 25.5),
        ambient_light_lux=_clamp(light, 5.0, 255.0),
        raining=raining,
        interior_temp_c=_clamp(interior, -40.0, 87.5),
        locked=state.locked or speed > AUTO_LOCK_KPH,
        passenger_present=state.passenger_present,
    )


def wiper_setting(state: DrivingState) -> int:
    if not state.raining:
        return WIPER_OFF
    if state.speed_kph < 30:
        return WIPER_INTERMITTENT_2
    return WIPER_LOW_SPEED if state.speed_kph < 90 else WIPER_HIGH_SPEED


CorrelatedFn = Callable[[DrivingState], float]

CORRELATED_SIGNALS: Dict[str, CorrelatedFn] = {
    "ENGINE_SPEED": lambda s: round(s.rpm),
    "ENGINE_TEMP": lambda s: round(s.engine_temp_c, 1),
    "THROTTLE_POSITION": lambda s: round(s.throttle_pct, 1),
    "ENGINE_LOAD": lambda s: round(
        _clamp(s.throttle_pct * 0.9 + random.uniform(-3, 3), 0.0, 100.0), 1
    ),
    "FUEL_LEVEL": lambda s: round(s.fuel_pct, 1),
    "BATTERY_VOLTAGE": lambda s: round(s.battery_v, 1),
    "WHEEL_SPEED_FL": lambda s: round(
        _clamp(s.speed_kph + random.uniform(-1.0, 1.0), 0.0, 300.0), 2
    ),
    "WHEEL_SPEED_FR": lambda s: round(
        _clamp(s.speed_kph + random.uniform(-1.2, 1.2), 0.0, 300.0), 2
    ),
    "WHEEL_SPEED_RL": lambda s: round(
        _clamp(s.speed_kph + random.uniform(-1.0, 1.0), 0.0, 300.0), 2
    ),
    "WHEEL_SPEED_RR": lambda s: round(
        _clamp(s.speed_kph + random.uniform(-1.2, 1.2), 0.0, 300.0), 2
    ),
    # AIRBAG_STATUS: an uneventful drive. SYSTEM_STATUS faults and crashes
    # only come from fault presets, which override these.
    "CRASH_DETECTED": lambda s: 0,
    "SYSTEM_STATUS": lambda s: 0,  # OK
    "SEATBELT_DRIVER": lambda s: 1,
    "SEATBELT_PASSENGER": lambda s: int(s.passenger_present),
    # Occupant classification switches the passenger airbag off for an
    # empty seat.
    "PASSENGER_AIRBAG_DISABLED": lambda s: int(not s.passenger_present),
    # BODY_STATUS
    "DOOR_OPEN_FL": lambda s: 0,
    "DOOR_OPEN_FR": lambda s: 0,
    "DOOR_OPEN_RL": lambda s: 0,
    "DOOR_OPEN_RR": lambda s: 0,
    "VEHICLE_LOCKED": lambda s: int(s.locked),
    "AMBIENT_LIGHT": lambda s: round(s.ambient_light_lux),
    "HEADLIGHTS_ON": lambda s: int(s.ambient_light_lux < HEADLIGHT_LUX or s.raining),
    "WIPER_STATUS": wiper_setting,
    "INTERIOR_TEMP": lambda s: round(s.interior_temp_c, 1),
}


class VehicleState:
    """Thread-safe, continuously-ticking driving state.

    A single background loop advances the state; readers only ever call
    `snapshot()`, never mutate it directly.
    """

    def __init__(self, tick_s: float = 0.2):
        self._tick_s = tick_s
        self._lock = threading.Lock()
        self._state = DrivingState(passenger_present=random.random() < 0.5)
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="VehicleStateTicker")
        self._thread.start()

    def _run(self) -> None:
        while True:
            time.sleep(self._tick_s)
            with self._lock:
                self._state = tick(self._state, self._tick_s)

    def snapshot(self) -> DrivingState:
        with self._lock:
            return replace(self._state)
