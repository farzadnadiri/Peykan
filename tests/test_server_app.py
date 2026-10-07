import asyncio
import os
import time

import can
from starlette.testclient import TestClient

from peykan.config import DEFAULT_DBC_PATH, get_settings
from peykan.server.mcp_server import build_http_app, create_app


def _make_app():
    os.environ["PEYKAN_DBC_PATH"] = DEFAULT_DBC_PATH
    return create_app()


def _client(app):
    """The app `main()` actually serves (MCP transport + routes + CORS)."""
    return TestClient(build_http_app(app, get_settings()))


def _call(app, name, args):
    """A tool's structured output: the result model's fields, or
    {"result": [...]} for tools that return a list."""
    result = asyncio.run(app.call_tool(name, args))
    assert not result.is_error, result.content
    return result.structured_content


def test_create_app_returns_server():
    # Ensure DBC path is resolvable during CI/test runs
    app = _make_app()
    # Avoid running the server; just ensure creation works
    assert hasattr(app, "tool") and hasattr(app, "run")


def test_expected_tools_are_registered():
    app = _make_app()
    tools = asyncio.run(app.list_tools())
    names = {t.name for t in tools}
    assert {
        "read_can_frames",
        "decode_can_frame",
        "filter_frames",
        "monitor_signal",
        "send_obd_request",
        "send_diagnostic_request",
        "get_vehicle_snapshot",
        "activate_fault_scenario",
        "decode_j1939_frame",
        "list_j1939_pgns",
        "request_j1939_pgn",
        "read_j1939_dtcs",
    }.issubset(names)


def test_decode_j1939_frame_tool():
    from peykan import j1939

    app = _make_app()
    can_id = j1939.build_can_id(j1939.PGN_EEC1, source_address=0, priority=3)
    data = list(j1939.encode_pgn(j1939.PGN_EEC1, {"ENGINE_SPEED": 1200.0}))
    decoded = _call(app, "decode_j1939_frame", {"arbitration_id": can_id, "data": data})
    assert decoded["pgn_hex"] == "0xF004"
    assert decoded["signals"]["ENGINE_SPEED"] == 1200.0


def test_list_j1939_pgns_tool():
    app = _make_app()
    catalog = _call(app, "list_j1939_pgns", {})
    acronyms = {p["acronym"] for p in catalog["pgns"]}
    assert {"EEC1", "ET1", "CCVS1"}.issubset(acronyms)


def test_healthz_and_dashboard_routes():
    app = _make_app()
    client = _client(app)

    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["dbc_loaded"] is True

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "text/html" in dashboard.headers["content-type"]
    assert "Peykan Live Dashboard" in dashboard.text


def test_cors_defaults_to_wildcard_without_credentials():
    app = _make_app()
    client = _client(app)
    resp = client.get("/healthz", headers={"Origin": "http://example.com"})
    assert resp.headers.get("access-control-allow-origin") == "*"
    # Wildcard origin + allow_credentials is a combination browsers reject
    # outright, so the middleware shouldn't be asked to send it at all.
    assert "access-control-allow-credentials" not in resp.headers


def test_cors_allows_credentials_once_origins_are_narrowed():
    os.environ["PEYKAN_CORS_ALLOW_ORIGINS"] = '["http://example.com"]'
    try:
        app = _make_app()
        client = _client(app)
        resp = client.get("/healthz", headers={"Origin": "http://example.com"})
        assert resp.headers.get("access-control-allow-origin") == "http://example.com"
        assert resp.headers.get("access-control-allow-credentials") == "true"
    finally:
        del os.environ["PEYKAN_CORS_ALLOW_ORIGINS"]


def test_read_can_frames_served_from_history_buffer():
    # create_app() starts LiveState's listener on the same virtual channel;
    # a frame sent from an independent bus instance should still show up in
    # read_can_frames -- proving the tool reads the shared history buffer
    # rather than racing its own (now-removed) fresh bus connection.
    app = _make_app()
    time.sleep(0.2)  # let the listener thread come up

    sender = can.ThreadSafeBus(interface="virtual", channel="bus0")
    try:
        sender.send(
            can.Message(
                arbitration_id=0x100,
                data=[1, 2, 3, 4, 5, 6, 7, 8],
                is_extended_id=False,
            )
        )
        time.sleep(0.3)  # let the listener pick it up
    finally:
        sender.shutdown()

    frames = _call(app, "read_can_frames", {"duration_s": 5.0})["result"]
    assert any(
        f["arbitration_id"] == "0x100" and f["data"] == [1, 2, 3, 4, 5, 6, 7, 8]
        for f in frames
    )

    # arbitration_id 0x100 is ENGINE_STATUS in vehicle.dbc, so the same
    # frame should also have updated get_vehicle_snapshot's signal state.
    snapshot = _call(app, "get_vehicle_snapshot", {})
    assert "ENGINE_SPEED" in snapshot["signals"]
    assert snapshot["signals"]["ENGINE_SPEED"]["message"] == "ENGINE_STATUS"
    assert snapshot["frame_count"] >= 1


def test_root_redirects_to_dashboard():
    app = _make_app()
    client = _client(app)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/dashboard"


class _FakeMsg:
    def __init__(self, arbitration_id, data):
        self.arbitration_id = arbitration_id
        self.data = bytes(data)
        self.is_extended_id = True


class _FakeBus:
    def __init__(self, messages):
        self._messages = list(messages)
        self.sent = []

    def recv(self, timeout=None):
        return self._messages.pop(0) if self._messages else None

    def send(self, msg):
        self.sent.append(msg)

    def shutdown(self):
        pass


def test_request_j1939_pgn_accepts_acronym(monkeypatch):
    # Small local models get hex->decimal conversion wrong (asking for 39652
    # instead of 0xF004 = 61444), so the tool takes "EEC1" / "0xF004" as-is.
    from peykan import j1939
    from peykan.server import mcp_server

    can_id = j1939.build_can_id(j1939.PGN_EEC1, source_address=0, priority=3)
    data = j1939.encode_pgn(j1939.PGN_EEC1, {"ENGINE_SPEED": 1500.0})
    fake = _FakeBus([_FakeMsg(can_id, data)])
    monkeypatch.setattr(mcp_server, "make_bus", lambda *a, **k: fake)
    monkeypatch.setattr(mcp_server, "shutdown_bus", lambda bus: None)

    app = _make_app()
    out = _call(app, "request_j1939_pgn", {"pgn": "EEC1", "timeout_s": 0.3})
    assert out["status"] == "success", out
    assert out["requested_pgn_hex"] == "0xF004"
    assert out["responses"][0]["signals"]["ENGINE_SPEED"] == 1500.0
    # The Request PGN frame on the bus carries 0xF004, little-endian.
    assert list(fake.sent[0].data[:3]) == [0x04, 0xF0, 0x00]


def test_request_j1939_pgn_timeout_lists_known_pgns(monkeypatch):
    from peykan.server import mcp_server

    monkeypatch.setattr(mcp_server, "make_bus", lambda *a, **k: _FakeBus([]))
    monkeypatch.setattr(mcp_server, "shutdown_bus", lambda bus: None)

    app = _make_app()
    out = _call(app, "request_j1939_pgn", {"pgn": 39652, "timeout_s": 0.2})
    assert out["status"] == "timeout"
    assert "0x9AE4" in out["message"]
    assert "EEC1=0xF004" in out["message"]


def test_obd_and_decode_tools_accept_hex_strings():
    app = _make_app()
    out = _call(
        app, "decode_can_frame", {"arbitration_id": "0x100", "data": [0, 0, 0, 0, 0, 0, 0, 0]}
    )
    assert out["status"] == "success", out
    assert "ENGINE_SPEED" in out["signals"]


def test_prompts_are_registered_and_render():
    app = _make_app()
    names = {p.name for p in asyncio.run(app.list_prompts())}
    assert {"diagnose_vehicle", "explain_dtc", "trip_summary", "fault_drill"} <= names

    result = asyncio.run(app.get_prompt("explain_dtc", {"code": "P0217"}))
    text = result.messages[0].content.text
    assert "P0217" in text and "service=3" in text

    result = asyncio.run(app.get_prompt("fault_drill", {"preset": "abs_fault"}))
    assert 'preset="abs_fault"' in result.messages[0].content.text


def test_server_instructions_point_models_at_the_snapshot_tool():
    app = _make_app()
    assert "get_vehicle_snapshot" in app.instructions


def test_listens_on_loopback_by_default():
    assert get_settings().mcp_host == "127.0.0.1"


def test_dns_rebinding_protection_on_loopback():
    # A web page on another origin resolving its own hostname to 127.0.0.1
    # must not be able to drive the tools; the SDK rejects foreign Host
    # headers with 421 before even looking up the session.
    app = _make_app()
    client = _client(app)
    foreign = client.post(
        "/messages/?session_id=0", headers={"Host": "attacker.example"}, json={}
    )
    assert foreign.status_code == 421
    local = client.post(
        "/messages/?session_id=0", headers={"Host": "localhost:6278"}, json={}
    )
    assert local.status_code != 421


def test_streamable_http_app_serves_routes_too():
    os.environ["PEYKAN_MCP_TRANSPORT"] = "streamable-http"
    try:
        app = _make_app()
        client = _client(app)
        assert client.get("/healthz").status_code == 200
    finally:
        del os.environ["PEYKAN_MCP_TRANSPORT"]


class _QuietBus(_FakeBus):
    def __init__(self):
        super().__init__([])


def test_new_tools_are_registered():
    app = _make_app()
    names = {t.name for t in asyncio.run(app.list_tools())}
    assert {
        "read_vin",
        "uds_read_data",
        "uds_read_dtcs",
        "uds_clear_dtcs",
        "get_transmit_log",
        "list_can_logs",
        "analyze_can_log",
        "get_log_signal",
        "replay_can_log",
        "stop_log_replay",
    } <= names


def test_tools_report_blocked_on_real_hardware(monkeypatch):
    from peykan.server import live_state, mcp_server

    monkeypatch.setenv("PEYKAN_CAN_INTERFACE", "pcan")
    monkeypatch.setattr(mcp_server, "make_bus", lambda *a, **k: _QuietBus())
    monkeypatch.setattr(mcp_server, "shutdown_bus", lambda bus: None)
    monkeypatch.setattr(live_state, "make_bus", lambda *a, **k: _QuietBus())
    app = _make_app()
    assert _call(app, "send_obd_request", {"service": 1, "pid": "0x0D"})["status"] == "blocked"
    assert _call(app, "activate_fault_scenario", {"preset": "crash"})["status"] == "blocked"
    assert _call(app, "uds_read_dtcs", {})["status"] == "blocked"
    log = _call(app, "get_transmit_log", {})
    assert log["policy"]["transmit_allowed"] is False
    assert log["records"] and not log["records"][-1]["sent"]


def test_log_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("PEYKAN_LOG_DIR", str(tmp_path))
    app = _make_app()
    files = _call(app, "list_can_logs", {})["files"]
    assert any(f["path"] == "sample" for f in files)
    analysis = _call(app, "analyze_can_log", {"path": "sample"})
    assert analysis["status"] == "success"
    assert {d["spn"] for d in analysis["analysis"]["j1939_dtcs"]} == {1322, 651}
    series = _call(
        app, "get_log_signal", {"path": "sample", "signal_name": "ENGINE_SPEED", "max_points": 5}
    )
    assert series["status"] == "success" and len(series["series"]["points"]) <= 6
    escape = _call(app, "analyze_can_log", {"path": "../../etc/passwd.asc"})
    assert escape["status"] == "error" and "outside" in escape["message"]


def test_replay_tool_starts_and_stops(monkeypatch):
    monkeypatch.setenv("PEYKAN_CAN_CHANNEL", "srv_replay")
    app = _make_app()
    started = _call(app, "replay_can_log", {"path": "sample", "speed": 20.0})
    assert started["status"] == "success" and started["replay"]["running"]
    time.sleep(0.5)
    stopped = _call(app, "stop_log_replay", {})
    assert stopped["replay"]["frames_sent"] > 0 and not stopped["replay"]["running"]


def test_read_vin_and_uds_through_the_server(monkeypatch):
    from peykan.bus import make_bus
    from peykan.simulator.faults import FaultState
    from peykan.simulator.uds_ecu import SIM_VIN, UdsEcu, UdsEcuThread

    channel = "srv_uds"
    monkeypatch.setenv("PEYKAN_CAN_CHANNEL", channel)
    faults = FaultState()
    faults.activate("overheat")
    UdsEcuThread(
        make_bus("virtual", channel), make_bus("virtual", channel), UdsEcu(fault_state=faults)
    ).start()
    time.sleep(0.3)
    app = _make_app()
    assert _call(app, "read_vin", {}) == {
        "status": "success", "vin": SIM_VIN, "check_digit_valid": True, "message": None
    }
    data = _call(app, "uds_read_data", {"dids": ["0xF190", "0xF195"]})
    assert data["values"][0]["value"] == SIM_VIN
    assert [d["code"] for d in _call(app, "uds_read_dtcs", {})["dtcs"]] == ["P0217"]
    unknown = _call(app, "uds_read_data", {"dids": ["0x1234"]})
    assert unknown["status"] == "negative_response" and "requestOutOfRange" in unknown["message"]
    assert _call(app, "uds_clear_dtcs", {})["status"] == "success"
