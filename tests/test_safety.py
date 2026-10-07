import json

import can
import pytest

from peykan.config import Settings
from peykan.safety import TransmitBlocked, TransmitGuard


class _RecordingBus:
    def __init__(self):
        self.sent = []

    def send(self, msg, timeout=None):
        self.sent.append(msg)

    def recv(self, timeout=None):
        return None


def _msg(arbitration_id=0x7DF):
    return can.Message(arbitration_id=arbitration_id, data=[2, 1, 0x0D], is_extended_id=False)


def test_virtual_bus_allows_reads_and_writes():
    guard = TransmitGuard(Settings(can_interface="virtual"))
    bus = _RecordingBus()
    guard.wrap(bus, "test").send(_msg())
    guard.wrap(bus, "test", write=True).send(_msg())
    assert len(bus.sent) == 2
    assert guard.policy()["transmit_allowed"] and guard.policy()["write_services_allowed"]


def test_real_hardware_is_read_only_by_default():
    guard = TransmitGuard(Settings(can_interface="pcan"))
    bus = _RecordingBus()
    with pytest.raises(TransmitBlocked, match="PEYKAN_ALLOW_TRANSMIT"):
        guard.wrap(bus, "obd").send(_msg())
    assert bus.sent == []
    record = guard.records()[-1]
    assert record["sent"] is False and "disabled" in record["blocked_reason"]


def test_allow_transmit_still_blocks_write_services():
    guard = TransmitGuard(Settings(can_interface="pcan", allow_transmit=True))
    bus = _RecordingBus()
    guard.wrap(bus, "obd").send(_msg())
    with pytest.raises(TransmitBlocked, match="PEYKAN_ALLOW_WRITE_SERVICES"):
        guard.wrap(bus, "clear dtcs", write=True).send(_msg(0x7E0))
    assert len(bus.sent) == 1

    both = TransmitGuard(
        Settings(can_interface="pcan", allow_transmit=True, allow_write_services=True)
    )
    both.wrap(bus, "clear dtcs", write=True).send(_msg(0x7E0))
    assert len(bus.sent) == 2


def test_write_services_never_allowed_without_transmit():
    settings = Settings(can_interface="pcan", allow_write_services=True)
    assert not settings.write_services_allowed


def test_allowlist_restricts_ids():
    guard = TransmitGuard(Settings(transmit_allowlist=["0x7DF", "0x7E0"]))
    bus = _RecordingBus()
    guard.wrap(bus, "ok").send(_msg(0x7DF))
    with pytest.raises(TransmitBlocked, match="0x123"):
        guard.wrap(bus, "nope").send(_msg(0x123))
    assert [m.arbitration_id for m in bus.sent] == [0x7DF]


def test_transmit_log_file_records_sent_and_blocked(tmp_path):
    path = tmp_path / "tx.jsonl"
    guard = TransmitGuard(Settings(transmit_allowlist=["0x7DF"], transmit_log_path=str(path)))
    bus = _RecordingBus()
    guard.wrap(bus, "ok").send(_msg(0x7DF))
    with pytest.raises(TransmitBlocked):
        guard.wrap(bus, "nope").send(_msg(0x100))
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [entry["sent"] for entry in lines] == [True, False]
    assert lines[0]["data"] == "02 01 0d"


def test_guarded_bus_is_a_python_can_bus_and_delegates_recv():
    # can-isotp refuses anything that isn't a BusABC.
    raw = can.Bus(interface="virtual", channel="guard_test")
    peer = can.Bus(interface="virtual", channel="guard_test")
    try:
        guarded = TransmitGuard(Settings()).wrap(raw, "test")
        assert isinstance(guarded, can.BusABC)
        peer.send(_msg(0x321))
        assert guarded.recv(timeout=1.0).arbitration_id == 0x321
    finally:
        raw.shutdown()
        peer.shutdown()


def test_simulator_refuses_to_run_on_real_hardware(monkeypatch):
    from peykan.simulator import runner

    monkeypatch.setenv("PEYKAN_CAN_INTERFACE", "pcan")
    with pytest.raises(runner.SimulatorRefused, match="SIMULATOR_ON_HARDWARE"):
        runner.run_simulator()
