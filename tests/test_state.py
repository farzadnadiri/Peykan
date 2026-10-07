import random

from mcp_can.config import DEFAULT_DBC_PATH
from mcp_can.dbc import load_dbc
from mcp_can.simulator.profiles import DEFAULT_PROFILE
from mcp_can.simulator.state import (
    AUTO_LOCK_KPH,
    CABIN_SETPOINT_C,
    CORRELATED_SIGNALS,
    ENGINE_TEMP_MAX_C,
    IDLE_RPM,
    WIPER_OFF,
    DrivingState,
    tick,
    wiper_setting,
)


def _db():
    return load_dbc(DEFAULT_DBC_PATH)


def test_fuel_never_increases():
    random.seed(0)
    state = DrivingState(fuel_pct=50.0)
    for _ in range(200):
        next_state = tick(state, dt_s=0.2)
        assert next_state.fuel_pct <= state.fuel_pct
        state = next_state


def test_values_stay_within_valid_ranges():
    random.seed(1)
    state = DrivingState()
    for _ in range(500):
        state = tick(state, dt_s=0.2)
        assert 0.0 <= state.throttle_pct <= 100.0
        assert 0.0 <= state.rpm <= 16383.0
        assert 0.0 <= state.speed_kph <= 300.0
        assert -40.0 <= state.engine_temp_c <= ENGINE_TEMP_MAX_C
        assert 0.0 <= state.fuel_pct <= 100.0


def test_rpm_and_speed_rise_toward_target_under_sustained_throttle():
    random.seed(2)
    state = DrivingState(throttle_pct=100.0, rpm=IDLE_RPM, speed_kph=0.0)
    # Hold throttle steady (bypass tick()'s own throttle wander) and let
    # rpm/speed chase their targets for a few seconds of simulated time.
    for _ in range(100):
        state = tick(state, dt_s=0.1)
        state.throttle_pct = 100.0
    assert state.rpm > IDLE_RPM + 1000
    assert state.speed_kph > 20.0


def test_engine_warms_up_toward_operating_temperature():
    random.seed(3)
    state = DrivingState(engine_temp_c=20.0)
    for _ in range(500):
        state = tick(state, dt_s=0.2)
    assert state.engine_temp_c > 70.0


def test_correlated_signals_produce_dbc_valid_values():
    # Ranges per vehicle.dbc, capped to what's actually bit-encodable (not
    # just the declared min/max -- see ENGINE_TEMP_MAX_C):
    # ENGINE_SPEED [0,16383], ENGINE_TEMP [-40,87.5],
    # THROTTLE_POSITION/ENGINE_LOAD/FUEL_LEVEL [0,100], WHEEL_SPEED_* [0,300].
    random.seed(4)
    ranges = {
        "ENGINE_SPEED": (0, 16383),
        "ENGINE_TEMP": (-40, ENGINE_TEMP_MAX_C),
        "THROTTLE_POSITION": (0, 100),
        "ENGINE_LOAD": (0, 100),
        "FUEL_LEVEL": (0, 100),
        "WHEEL_SPEED_FL": (0, 300),
        "WHEEL_SPEED_FR": (0, 300),
        "WHEEL_SPEED_RL": (0, 300),
        "WHEEL_SPEED_RR": (0, 300),
        "CRASH_DETECTED": (0, 1),
        "SYSTEM_STATUS": (0, 3),
        "SEATBELT_DRIVER": (0, 1),
        "SEATBELT_PASSENGER": (0, 1),
        "PASSENGER_AIRBAG_DISABLED": (0, 1),
        "DOOR_OPEN_FL": (0, 1),
        "DOOR_OPEN_FR": (0, 1),
        "DOOR_OPEN_RL": (0, 1),
        "DOOR_OPEN_RR": (0, 1),
        "VEHICLE_LOCKED": (0, 1),
        "AMBIENT_LIGHT": (0, 255),
        "HEADLIGHTS_ON": (0, 1),
        "WIPER_STATUS": (0, 5),
        "INTERIOR_TEMP": (-40, 87.5),
    }
    assert set(CORRELATED_SIGNALS) == set(ranges)
    state = DrivingState(throttle_pct=100.0, rpm=16000.0, speed_kph=295.0, fuel_pct=1.0)
    for name, fn in CORRELATED_SIGNALS.items():
        lo, hi = ranges[name]
        for _ in range(50):
            value = fn(state)
            assert lo <= value <= hi, f"{name}={value} outside [{lo},{hi}]"


def test_correlated_signals_are_actually_encodable():
    # Regression test: CORRELATED_SIGNALS values previously passed the
    # DBC-declared min/max check above while still exceeding what the
    # signal's bit width can encode (ENGINE_TEMP's 8 bits cap out at 87.5,
    # below the declared 127.5 max), which made cantools' Message.encode
    # raise. Drive the underlying signals to their extremes and confirm
    # encoding every ENGINE_STATUS/ABS_STATUS message actually succeeds.
    db = _db()
    engine_msg = db.get_message_by_name("ENGINE_STATUS")
    abs_msg = db.get_message_by_name("ABS_STATUS")
    random.seed(5)
    extreme_states = [
        DrivingState(throttle_pct=100.0, rpm=16000.0, speed_kph=295.0, engine_temp_c=87.5),
        DrivingState(throttle_pct=0.0, rpm=0.0, speed_kph=0.0, engine_temp_c=-40.0),
    ]
    for state in extreme_states:
        for _ in range(50):
            engine_msg.encode(
                {
                    "ENGINE_SPEED": CORRELATED_SIGNALS["ENGINE_SPEED"](state),
                    "ENGINE_TEMP": CORRELATED_SIGNALS["ENGINE_TEMP"](state),
                    "THROTTLE_POSITION": CORRELATED_SIGNALS["THROTTLE_POSITION"](state),
                    "ENGINE_LOAD": CORRELATED_SIGNALS["ENGINE_LOAD"](state),
                    "FUEL_LEVEL": CORRELATED_SIGNALS["FUEL_LEVEL"](state),
                }
            )
            abs_msg.encode(
                {
                    "WHEEL_SPEED_FL": CORRELATED_SIGNALS["WHEEL_SPEED_FL"](state),
                    "WHEEL_SPEED_FR": CORRELATED_SIGNALS["WHEEL_SPEED_FR"](state),
                    "WHEEL_SPEED_RL": CORRELATED_SIGNALS["WHEEL_SPEED_RL"](state),
                    "WHEEL_SPEED_RR": CORRELATED_SIGNALS["WHEEL_SPEED_RR"](state),
                }
            )


def test_every_broadcast_signal_is_modeled():
    # Anything left out falls back to SimThread's independent random draws,
    # which is how a 60 km/h car used to report CRASH_DETECTED and open doors.
    db = _db()
    for msg_name, _period in DEFAULT_PROFILE:
        for sig in db.get_message_by_name(msg_name).signals:
            assert sig.name in CORRELATED_SIGNALS, f"{msg_name}.{sig.name} is unmodeled"


def test_broadcast_messages_encode_from_state():
    db = _db()
    random.seed(6)
    state = DrivingState(passenger_present=True)
    for _ in range(300):
        state = tick(state, dt_s=0.2)
        for msg_name, _period in DEFAULT_PROFILE:
            msg = db.get_message_by_name(msg_name)
            msg.encode({sig.name: CORRELATED_SIGNALS[sig.name](state) for sig in msg.signals})


def test_normal_drive_reports_no_crash_open_doors_or_faults():
    random.seed(7)
    state = DrivingState()
    for _ in range(3000):  # ten minutes of simulated driving
        state = tick(state, dt_s=0.2)
        assert CORRELATED_SIGNALS["CRASH_DETECTED"](state) == 0
        assert CORRELATED_SIGNALS["SYSTEM_STATUS"](state) == 0
        assert CORRELATED_SIGNALS["SEATBELT_DRIVER"](state) == 1
        for door in ("FL", "FR", "RL", "RR"):
            assert CORRELATED_SIGNALS[f"DOOR_OPEN_{door}"](state) == 0
        assert 10.0 <= state.interior_temp_c <= 30.0
        if not state.raining:
            assert wiper_setting(state) == WIPER_OFF


def test_passenger_seatbelt_and_airbag_follow_occupancy():
    for present in (True, False):
        state = DrivingState(passenger_present=present)
        assert CORRELATED_SIGNALS["SEATBELT_PASSENGER"](state) == int(present)
        assert CORRELATED_SIGNALS["PASSENGER_AIRBAG_DISABLED"](state) == int(not present)


def test_doors_auto_lock_once_moving_and_stay_locked():
    random.seed(8)
    state = DrivingState(speed_kph=AUTO_LOCK_KPH + 5)
    state = tick(state, dt_s=0.2)
    assert state.locked
    state.speed_kph = 0.0
    state = tick(state, dt_s=0.2)
    assert state.locked


def test_cabin_settles_toward_climate_setpoint():
    random.seed(9)
    state = DrivingState(interior_temp_c=35.0)
    for _ in range(3000):
        state = tick(state, dt_s=0.2)
    assert abs(state.interior_temp_c - CABIN_SETPOINT_C) < 1.0


def test_headlights_and_wipers_follow_conditions():
    dark = DrivingState(ambient_light_lux=20.0)
    bright = DrivingState(ambient_light_lux=200.0)
    rainy_fast = DrivingState(ambient_light_lux=200.0, raining=True, speed_kph=110.0)
    assert CORRELATED_SIGNALS["HEADLIGHTS_ON"](dark) == 1
    assert CORRELATED_SIGNALS["HEADLIGHTS_ON"](bright) == 0
    assert CORRELATED_SIGNALS["HEADLIGHTS_ON"](rainy_fast) == 1
    assert wiper_setting(bright) == WIPER_OFF
    assert wiper_setting(rainy_fast) > wiper_setting(DrivingState(raining=True, speed_kph=10.0))
