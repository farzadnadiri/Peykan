import json
import time

from typer.testing import CliRunner

from peykan import cli as cli_module


class FakeMsg:
    def __init__(self, arbitration_id: int, data: bytes, timestamp: float | None = None):
        self.arbitration_id = arbitration_id
        self.data = data
        self.timestamp = time.time() if timestamp is None else timestamp


class FakeBus:
    def __init__(self, messages: list[FakeMsg]):
        self._messages = messages
        self.sent = []

    def recv(self, timeout: float | None = None):
        if self._messages:
            return self._messages.pop(0)
        return None

    def send(self, msg):
        self.sent.append(msg)


runner = CliRunner()


def test_cli_frames_minimal(monkeypatch):
    # Arrange: fake bus returns a single frame
    fake = FakeBus([FakeMsg(0x100, bytes([1, 2, 3, 4, 5, 6, 7, 8]))])
    monkeypatch.setattr(cli_module, "make_bus", lambda *a, **k: fake)

    # Act
    result = runner.invoke(cli_module.app, ["frames", "--seconds", "0.02"])  # short capture

    # Assert
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert isinstance(out, list)
    assert len(out) >= 1
    first = out[0]
    assert first["arbitration_id"] == hex(0x100)
    assert first["data"] == [1, 2, 3, 4, 5, 6, 7, 8]


def test_cli_obd_request_basic(monkeypatch):
    # Arrange: fake bus will return a single-frame OBD-II positive response
    # Example response to 0x01 0x0D (Vehicle speed): 0x41 0x0D <A>
    response = FakeMsg(0x7E8, bytes([3, 0x41, 0x0D, 50, 0, 0, 0, 0]))
    fake = FakeBus([response])
    monkeypatch.setattr(cli_module, "make_bus", lambda *a, **k: fake)

    # Act
    result = runner.invoke(cli_module.app, ["obd-request", "--service", "0x01", "--pid", "0x0D"])

    # Assert
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["arbitration_id"] == hex(0x7E8)
    assert out["data"][0] == 3  # length
    assert out["data"][1] == 0x41 and out["data"][2] == 0x0D
