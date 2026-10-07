import time

import pytest

from peykan import uds
from peykan.bus import make_bus
from peykan.config import Settings
from peykan.safety import TransmitBlocked, TransmitGuard
from peykan.simulator.faults import FaultState
from peykan.simulator.state import DrivingState, VehicleState
from peykan.simulator.uds_ecu import SIM_VIN, UdsEcu, UdsEcuThread


def _ecu(preset=None):
    faults = FaultState()
    faults.activate(preset)
    return UdsEcu(fault_state=faults), faults


# --- protocol helpers ----------------------------------------------------------
def test_dtc_conversion_round_trip():
    value = uds.dtc_to_uds("P0300", failure_type=0x1F)
    assert value == 0x03001F
    assert uds.uds_to_dtc(value) == {"code": "P0300", "failure_type": 0x1F, "uds_id": "0x03001F"}
    assert uds.uds_to_dtc(uds.dtc_to_uds("B0001"))["code"] == "B0001"


def test_vin_check_digit():
    assert uds.vin_check_digit_valid(SIM_VIN) is True
    assert uds.vin_check_digit_valid(SIM_VIN[:8] + "7" + SIM_VIN[9:]) is False
    assert uds.vin_check_digit_valid("TOO-SHORT") is None


def test_status_bits():
    assert uds.describe_status(0x0B) == [
        "test_failed",
        "test_failed_this_operation_cycle",
        "confirmed",
    ]


# --- simulated ECU, protocol level ------------------------------------------------
def test_ecu_identification_dids():
    ecu, _ = _ecu()
    response = ecu.handle(bytes([0x22, 0xF1, 0x90]))
    assert response[:3] == bytes([0x62, 0xF1, 0x90])
    assert response[3:].decode() == SIM_VIN


def test_ecu_live_dids_follow_vehicle_state():
    class _Fixed:
        def snapshot(self):
            return DrivingState(rpm=2400.0, battery_v=13.9)

    ecu = UdsEcu(vehicle_state=_Fixed(), fault_state=FaultState())
    response = ecu.handle(bytes([0x22, 0xF4, 0x0C]))
    assert uds.decode_did(0xF40C, response[3:])["value"] == 2400.0


def test_ecu_negative_responses():
    ecu, _ = _ecu()
    assert ecu.handle(bytes([0x22, 0x12, 0x34])) == bytes([0x7F, 0x22, 0x31])  # unknown DID
    assert ecu.handle(bytes([0x2E, 0xF1, 0x90])) == bytes([0x7F, 0x2E, 0x11])  # unsupported service
    assert ecu.handle(bytes([0x22, 0xF1])) == bytes([0x7F, 0x22, 0x13])  # bad length
    assert ecu.handle(bytes([0x10, 0x02])) == bytes([0x7F, 0x10, 0x12])  # programming session


def test_tester_present_can_suppress_its_response():
    ecu, _ = _ecu()
    assert ecu.handle(bytes([0x3E, 0x00])) == bytes([0x7E, 0x00])
    assert ecu.handle(bytes([0x3E, 0x80])) is None


def test_dtc_memory_keeps_history_until_cleared():
    ecu, faults = _ecu("overheat")
    active = ecu.handle(bytes([0x19, 0x02, 0xFF]))
    assert active[3:] == uds.dtc_to_uds("P0217").to_bytes(3, "big") + bytes([0x0B])

    faults.activate(None)
    stored = ecu.handle(bytes([0x19, 0x02, 0xFF]))
    assert stored[-1] == 0x08  # confirmed only: no longer failing
    assert ecu.handle(bytes([0x19, 0x02, 0x01])) == bytes([0x59, 0x02, 0x0B])  # mask: failing now

    assert ecu.handle(bytes([0x14, 0xFF, 0xFF, 0xFF])) == bytes([0x54])
    assert ecu.handle(bytes([0x19, 0x01, 0xFF]))[-2:] == bytes([0, 0])  # count 0


def test_clearing_does_not_hide_a_fault_that_is_still_present():
    ecu, _ = _ecu("misfire")
    ecu.handle(bytes([0x14, 0xFF, 0xFF, 0xFF]))
    assert ecu.dtc_memory() == {"P0300": 0x0B}


def test_obd_mode09_vin_is_multi_frame_length():
    ecu, _ = _ecu()
    response = ecu.handle(bytes([0x09, 0x02]))
    assert response[:3] == bytes([0x49, 0x02, 0x01]) and len(response) == 20  # > 7 bytes


# --- end to end over a virtual bus, real ISO-TP segmentation ----------------------
@pytest.fixture(scope="module")
def ecu_bus():
    channel = "uds_e2e"
    faults = FaultState()
    vehicle = VehicleState()
    vehicle.start()
    UdsEcuThread(
        make_bus("virtual", channel), make_bus("virtual", channel), UdsEcu(vehicle, faults)
    ).start()
    time.sleep(0.3)
    return Settings(can_channel=channel), faults


def test_read_vin_over_isotp(ecu_bus):
    settings, _ = ecu_bus
    result = uds.read_vin_obd(settings, TransmitGuard(settings))
    assert result == {"vin": SIM_VIN, "check_digit_valid": True}


def test_uds_client_reads_dids_and_dtcs(ecu_bus):
    settings, faults = ecu_bus
    guard = TransmitGuard(settings)
    faults.activate("battery_low")
    try:
        with uds.uds_client(settings, guard, "test", False) as client:
            values = uds.read_dids(client, [0xF190, 0xF195])
            dtcs = uds.read_dtcs(client)
    finally:
        faults.activate(None)
    assert values[0]["value"] == SIM_VIN
    assert [d["code"] for d in dtcs] == ["P0562"]
    assert "test_failed" in dtcs[0]["status_bits"]
    # Flow-control and consecutive frames went through the guard too.
    assert any(r["arbitration_id"] == "0x7e0" for r in guard.records(100))


def test_uds_client_refused_by_policy_before_opening_the_bus():
    settings = Settings(can_interface="pcan", can_channel="never_opened")
    with pytest.raises(TransmitBlocked):
        with uds.uds_client(settings, TransmitGuard(settings), "test", False):
            pass


def test_ecu_records_a_fault_nobody_queried_while_it_was_active(ecu_bus):
    settings, faults = ecu_bus
    faults.activate("crash")
    time.sleep(0.6)  # the ECU's own loop notices it
    faults.activate(None)
    with uds.uds_client(settings, TransmitGuard(settings), "test", False) as client:
        stored = {d["code"]: d["status_bits"] for d in uds.read_dtcs(client)}
    assert stored["B0001"] == ["confirmed"]
