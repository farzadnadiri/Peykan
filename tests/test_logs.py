import threading
import time

import can
import pytest

from peykan import j1939, logs
from peykan.config import DEFAULT_DBC_PATH
from peykan.dbc import load_dbc
from peykan.obd import build_response_frame, encode_dtc

TWO_DTCS = [j1939.J1939Dtc(spn=1322, fmi=31), j1939.J1939Dtc(spn=651, fmi=7)]
FRAMES_IN_LOG = 40 + 3 + 1  # ENGINE_STATUS + DM1 via BAM + OBD Mode 03 reply


def _write_log(path):
    db = load_dbc(DEFAULT_DBC_PATH)
    engine = db.get_message_by_name("ENGINE_STATUS")
    t = 1000.0
    with can.Logger(str(path)) as log:
        for i in range(40):  # 10 Hz, with a 2 s dropout halfway
            if i == 20:
                t += 2.0
            data = engine.encode(
                {
                    "ENGINE_SPEED": 800 + i * 50,
                    "ENGINE_TEMP": 85,
                    "THROTTLE_POSITION": 10,
                    "ENGINE_LOAD": 10,
                    "FUEL_LEVEL": 50,
                    "BATTERY_VOLTAGE": 14.1,
                }
            )
            log(
                can.Message(
                    timestamp=t, arbitration_id=engine.frame_id, data=data, is_extended_id=False
                )
            )
            t += 0.1
        dm1 = j1939.build_dm1(TWO_DTCS, mil_on=True)
        for can_id, data in j1939.frames_for_pgn(j1939.PGN_DM1, dm1, j1939.SA_ENGINE):
            log(can.Message(timestamp=t, arbitration_id=can_id, data=data, is_extended_id=True))
            t += 0.05
        _, obd = build_response_frame([0x43, *encode_dtc("P0300")])
        log(can.Message(timestamp=t, arbitration_id=0x7E8, data=obd, is_extended_id=False))
    return path


# Vector ASC/BLF, candump .log, PEAK TRC and CSV: written and read back by
# python-can, so this checks the analysis works on each reader's output.
@pytest.fixture(params=["drive.asc", "drive.blf", "drive.log", "drive.trc", "drive.csv"])
def log_file(tmp_path, request):
    return _write_log(tmp_path / request.param)


def test_analyze_log(log_file):
    analysis = logs.analyze_log(logs.load_log(log_file), load_dbc(DEFAULT_DBC_PATH))
    assert analysis["frame_count"] == FRAMES_IN_LOG
    engine = next(i for i in analysis["ids"] if i["arbitration_id"] == "0x100")
    assert engine["name"] == "ENGINE_STATUS" and abs(engine["rate_hz"] - 10) < 0.5
    speed = next(s for s in analysis["signals"] if s["name"] == "ENGINE_SPEED")
    assert (speed["min"], speed["max"], speed["unit"]) == (800, 2750, "rpm")
    assert {(d["spn"], d["fmi"]) for d in analysis["j1939_dtcs"]} == {(1322, 31), (651, 7)}
    assert [d["code"] for d in analysis["obd_dtcs"]] == ["P0300"]
    assert any(g.startswith("0x100") for g in analysis["timing_gaps"])


def test_signal_series_downsamples(log_file):
    db = load_dbc(DEFAULT_DBC_PATH)
    series = logs.signal_series(logs.load_log(log_file), db, "ENGINE_SPEED", 10)
    assert series["samples_total"] == 40
    assert len(series["points"]) <= 11
    assert series["points"][0]["value"] == 800 and series["points"][-1]["value"] == 2750
    with pytest.raises(logs.LogAccessError):
        logs.signal_series(logs.load_log(log_file), load_dbc(DEFAULT_DBC_PATH), "NOPE", 10)


def test_log_paths_are_confined_to_the_log_dir(tmp_path):
    (tmp_path / "logs").mkdir()
    inside = _write_log(tmp_path / "logs" / "a.asc")
    log_dir = str(tmp_path / "logs")
    assert logs.resolve_log_path("a.asc", log_dir) == inside.resolve()
    outside = _write_log(tmp_path / "secret.asc")
    for bad in ("../secret.asc", str(outside)):
        with pytest.raises(logs.LogAccessError, match="outside"):
            logs.resolve_log_path(bad, log_dir)
    (tmp_path / "logs" / "notes.txt").write_text("hi")
    with pytest.raises(logs.LogAccessError, match="unsupported"):
        logs.resolve_log_path("notes.txt", log_dir)
    with pytest.raises(logs.LogAccessError, match="not found"):
        logs.resolve_log_path("missing.asc", log_dir)
    assert logs.resolve_log_path("sample", log_dir) == logs.SAMPLE_LOG


def test_replay_puts_frames_on_the_bus(tmp_path):
    log = logs.load_log(_write_log(tmp_path / "drive.asc"))
    sender = can.Bus(interface="virtual", channel="replay_test")
    listener = can.Bus(interface="virtual", channel="replay_test")
    try:
        player = logs.LogReplayer(log, sender, speed=50.0)
        start = time.time()
        player.start()
        received = []
        while time.time() - start < 3 and len(received) < FRAMES_IN_LOG:
            msg = listener.recv(timeout=0.2)
            if msg is not None:
                received.append(msg)
        player.join(timeout=2)
        assert len(received) == FRAMES_IN_LOG
        assert received[0].timestamp > start - 1  # restamped as live traffic
        assert player.status()["frames_sent"] == FRAMES_IN_LOG
    finally:
        sender.shutdown()
        listener.shutdown()


def test_record_bus_writes_a_readable_log(tmp_path):
    out = tmp_path / "rec.asc"
    recorder_bus = can.Bus(interface="virtual", channel="record_test")
    sender = can.Bus(interface="virtual", channel="record_test")

    def _send():
        time.sleep(0.1)
        for i in range(5):
            sender.send(can.Message(arbitration_id=0x200 + i, data=[i], is_extended_id=False))

    try:
        threading.Thread(target=_send).start()
        assert logs.record_bus(recorder_bus, str(out), 0.6) == 5
    finally:
        recorder_bus.shutdown()
        sender.shutdown()
    assert len(logs.load_log(out).frames) == 5


def test_bundled_sample_log_tells_a_story():
    analysis = logs.analyze_log(logs.load_log(logs.SAMPLE_LOG), load_dbc(DEFAULT_DBC_PATH))
    assert analysis["duration_s"] > 20
    # The recording includes a misfire partway through (two J1939 DTCs, so
    # its DM1 travels via the BAM transport protocol).
    assert {d["spn"] for d in analysis["j1939_dtcs"]} == {1322, 651}
