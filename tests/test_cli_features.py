import json
import time

from typer.testing import CliRunner

from peykan import cli as cli_module
from peykan.bus import make_bus
from peykan.simulator.faults import FaultState
from peykan.simulator.uds_ecu import SIM_VIN, UdsEcu, UdsEcuThread

runner = CliRunner()


class _FakeBus:
    def __init__(self):
        self.sent = []

    def send(self, msg, timeout=None):
        self.sent.append(msg)

    def recv(self, timeout=None):
        return None


def test_cli_refuses_to_transmit_on_real_hardware(monkeypatch):
    fake = _FakeBus()
    monkeypatch.setenv("PEYKAN_CAN_INTERFACE", "pcan")
    monkeypatch.setattr(cli_module, "make_bus", lambda *a, **k: fake)
    result = runner.invoke(cli_module.app, ["obd-request", "--service", "0x01", "--pid", "0x0D"])
    assert result.exit_code == 2
    assert "PEYKAN_ALLOW_TRANSMIT" in result.output
    assert fake.sent == []


def test_cli_log_info_and_signal_on_the_sample():
    result = runner.invoke(cli_module.app, ["log-info", "sample", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["frame_count"] > 1000
    result = runner.invoke(
        cli_module.app, ["log-signal", "sample", "BATTERY_VOLTAGE", "--max-points", "5"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["unit"] == "V"


def test_cli_vin_and_uds_dtcs(monkeypatch):
    channel = "cli_uds"
    monkeypatch.setenv("PEYKAN_CAN_CHANNEL", channel)
    faults = FaultState()
    faults.activate("battery_low")
    UdsEcuThread(
        make_bus("virtual", channel), make_bus("virtual", channel), UdsEcu(fault_state=faults)
    ).start()
    time.sleep(0.3)
    result = runner.invoke(cli_module.app, ["vin"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["vin"] == SIM_VIN
    result = runner.invoke(cli_module.app, ["uds-dtcs"])
    assert result.exit_code == 0, result.output
    assert [d["code"] for d in json.loads(result.stdout)["dtcs"]] == ["P0562"]
