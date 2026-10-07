import random

from peykan.obd import decode_pid_value, simulate_response
from peykan.simulator.faults import PRESETS, FaultState
from peykan.simulator.j1939_runner import dm1_messages
from peykan.simulator.state import CHARGING_VOLTAGE, DrivingState, tick


def _driving():
    return DrivingState(throttle_pct=40.0, rpm=2600.0, speed_kph=80.0, locked=True)


def test_new_presets_exist_with_descriptions():
    for name in ("crash", "door_ajar", "misfire", "battery_low"):
        assert PRESETS[name].description


def test_crash_stops_the_vehicle_and_deploys_airbags():
    faults = FaultState()
    faults.activate("crash")
    state = faults.apply(_driving())
    assert state.speed_kph == 0 and state.rpm == 0 and not state.locked
    assert faults.get_override("CRASH_DETECTED") == 1
    assert faults.dtcs() == ["B0001"]


def test_state_effect_works_on_a_copy():
    original = _driving()
    faults = FaultState()
    faults.activate("crash")
    faults.apply(original)
    assert original.speed_kph == 80.0


def test_misfire_makes_rpm_rough():
    random.seed(1)
    faults = FaultState()
    faults.activate("misfire")
    readings = {round(faults.apply(_driving()).rpm) for _ in range(20)}
    assert len(readings) > 10
    assert max(readings) - min(readings) > 150


def test_battery_low_shows_on_obd_voltage_pid():
    faults = FaultState()
    faults.activate("battery_low")
    state = faults.apply(_driving())
    decoded = decode_pid_value(0x42, simulate_response(0x01, 0x42, state=state)[2:])
    assert decoded["name"] == "control_module_voltage" and decoded["value"] < 12.0
    assert faults.dtcs() == ["P0562"]


def test_battery_charges_with_engine_running():
    random.seed(2)
    state = DrivingState(rpm=2000.0)
    for _ in range(300):
        state = tick(state, dt_s=0.2)
    assert abs(state.battery_v - CHARGING_VOLTAGE) < 0.3


def test_door_ajar_only_touches_one_door():
    faults = FaultState()
    faults.activate("door_ajar")
    assert faults.get_override("DOOR_OPEN_RR") == 1
    assert not faults.has_override("DOOR_OPEN_FL")
    assert faults.apply(_driving()) == _driving()  # no driving-state effect


def test_misfire_dm1_needs_the_transport_protocol():
    faults = FaultState()
    faults.activate("misfire")
    frames = dm1_messages(faults)
    assert len(frames) == 3  # TP.CM BAM + 2 TP.DT packets for 2 DTCs
    faults.activate("battery_low")
    assert len(dm1_messages(faults)) == 1


def test_obd_mode01_values_follow_the_vehicle():
    state = DrivingState(rpm=3000.0, speed_kph=72.4, engine_temp_c=88.0)
    rpm = decode_pid_value(0x0C, simulate_response(0x01, 0x0C, state=state)[2:])
    speed = decode_pid_value(0x0D, simulate_response(0x01, 0x0D, state=state)[2:])
    coolant = decode_pid_value(0x05, simulate_response(0x01, 0x05, state=state)[2:])
    assert (rpm["value"], speed["value"], coolant["value"]) == (3000.0, 72, 88)
