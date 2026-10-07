import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Union

import can
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from .. import __version__, j1939, logs, uds
from ..bus import make_bus, shutdown_bus
from ..config import Settings, configure_logging, get_settings
from ..dbc import decode_frame, load_dbc, signal_int
from ..diagnostics import (
    REQUEST_MESSAGE,
    RESPONSE_MESSAGES,
    ecu_name_from_response_message,
    response_code_name,
)
from ..diagnostics import (
    is_write_service as is_diag_write_service,
)
from ..obd import build_request, decode_response, parse_response, wait_for_response
from ..parsing import IntLike, parse_int
from ..safety import OBD_WRITE_SERVICES, TransmitBlocked, TransmitGuard
from ..simulator.faults import FAULT_ACK_ID, PRESETS, build_control_frame
from .live_state import DEFAULT_HISTORY_WINDOW_S, LiveState
from .prompts import SERVER_INSTRUCTIONS, register_prompts
from .schemas import (
    DecodeResult,
    DiagnosticEcuResponse,
    DiagnosticResult,
    FaultScenarioResult,
    FrameOut,
    J1939DecodeResult,
    J1939Dtc,
    J1939DtcResult,
    J1939PgnCatalog,
    J1939PgnInfo,
    J1939RequestResult,
    LogAnalysisResult,
    LogListResult,
    LogSignalResult,
    ObdResponse,
    ReplayStatusResult,
    SignalSample,
    SignalState,
    TransmitLogResult,
    UdsClearResult,
    UdsDataResult,
    UdsDtcResult,
    VehicleSnapshot,
    VinResult,
)

logger = logging.getLogger(__name__)

_DASHBOARD_HTML = (Path(__file__).parent / "templates" / "dashboard.html").read_text(
    encoding="utf-8"
)


# Hosts that only accept connections from this machine. Binding to anything
# else exposes unauthenticated tools (some of which transmit on the bus) to
# the network, so that has to be an explicit choice (PEYKAN_MCP_HOST).
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def create_app() -> MCPServer:
    """Create an MCP server exposing CAN tools, prompts and DBC metadata."""
    settings = get_settings()
    mcp = MCPServer(
        "Peykan", version=__version__, instructions=SERVER_INSTRUCTIONS
    )
    register_prompts(mcp)
    db = load_dbc(settings.dbc_path)
    live_state = LiveState(
        db,
        settings.can_interface,
        settings.can_channel,
        history_window_s=max(settings.max_duration_s, DEFAULT_HISTORY_WINDOW_S),
    )
    live_state.start()
    # Every frame a tool transmits goes through this (see safety.py).
    guard = TransmitGuard(settings)
    replay: Dict[str, Optional[logs.LogReplayer]] = {"current": None}

    def _capped_duration(duration_s: float) -> float:
        if duration_s > settings.max_duration_s:
            logger.warning(
                "duration_s=%.1f exceeds max_duration_s=%.1f; capping.",
                duration_s,
                settings.max_duration_s,
            )
            return settings.max_duration_s
        return duration_s

    @mcp.tool()
    def read_can_frames(duration_s: float = 1.0) -> List[FrameOut]:
        """Return every raw frame seen in the last `duration_s` seconds.

        Served from the server's continuously-running frame history buffer,
        so it returns immediately (no waiting) and won't miss frames sent
        between calls -- unlike opening a fresh bus listener per call."""
        duration_s = _capped_duration(duration_s)
        since = time.time() - duration_s
        return [
            FrameOut(
                timestamp=f["timestamp"],
                arbitration_id=hex(f["arbitration_id"]),
                data=f["data"],
            )
            for f in live_state.frames_since(since)
        ]

    @mcp.tool()
    def decode_can_frame(arbitration_id: IntLike, data: List[int]) -> DecodeResult:
        """Decode a single CAN frame's bytes into named signals using the loaded DBC.
        `arbitration_id` may be an integer or a hex string like "0x100"."""
        try:
            decoded = decode_frame(db, parse_int(arbitration_id), bytes(data))
            return DecodeResult(status="success", signals=decoded)
        except Exception as e:
            return DecodeResult(status="error", message=str(e))

    @mcp.tool()
    def filter_frames(
        arbitration_id: Optional[IntLike] = None,
        signal_name: Optional[str] = None,
        duration_s: float = 1.0,
    ) -> List[FrameOut]:
        """Frames from the last `duration_s` seconds matching the given
        arbitration_id (integer or hex string like "0x100") and/or containing
        signal_name once decoded. Served from the frame history buffer -- see
        `read_can_frames`."""
        duration_s = _capped_duration(duration_s)
        wanted_id = parse_int(arbitration_id) if arbitration_id is not None else None
        since = time.time() - duration_s
        results: List[FrameOut] = []
        for f in live_state.frames_since(since):
            if wanted_id is not None and f["arbitration_id"] != wanted_id:
                continue
            if signal_name:
                try:
                    decoded = decode_frame(db, f["arbitration_id"], bytes(f["data"]))
                except Exception:
                    continue
                if signal_name not in decoded:
                    continue
                results.append(
                    FrameOut(
                        timestamp=f["timestamp"],
                        arbitration_id=hex(f["arbitration_id"]),
                        data=f["data"],
                        signal_name=signal_name,
                        signal_value=decoded[signal_name],
                    )
                )
            else:
                results.append(
                    FrameOut(
                        timestamp=f["timestamp"],
                        arbitration_id=hex(f["arbitration_id"]),
                        data=f["data"],
                    )
                )
        return results

    @mcp.tool()
    def monitor_signal(signal_name: str, duration_s: float = 2.0) -> List[SignalSample]:
        """Timestamped samples of one decoded signal over the last `duration_s`
        seconds. Served from the frame history buffer -- see `read_can_frames`."""
        duration_s = _capped_duration(duration_s)
        since = time.time() - duration_s
        results: List[SignalSample] = []
        for f in live_state.frames_since(since):
            try:
                decoded = decode_frame(db, f["arbitration_id"], bytes(f["data"]))
            except Exception:
                continue
            if signal_name in decoded:
                results.append(SignalSample(timestamp=f["timestamp"], value=decoded[signal_name]))
        return results

    @mcp.tool()
    def get_vehicle_snapshot() -> VehicleSnapshot:
        """The last known value of every signal seen so far (one entry per
        signal, not per frame) -- a single-call overview of current vehicle
        state instead of decoding a stream of raw frames yourself. `age_s`
        is how long ago that value was last updated; a large `age_s` means
        that ECU/message hasn't been seen recently."""
        raw = live_state.snapshot()
        now = time.time()
        signals = {
            name: SignalState(
                value=info["value"],
                unit=info["unit"],
                message=info["message"],
                age_s=round(now - info["timestamp"], 2),
            )
            for name, info in raw["signals"].items()
        }
        return VehicleSnapshot(
            signals=signals, frame_count=raw["frame_count"], uptime_s=raw["uptime_s"]
        )

    @mcp.tool()
    def send_obd_request(
        service: IntLike,
        pid: Optional[IntLike] = None,
        timeout_s: float = 2.0,
    ) -> ObdResponse:
        """Send a standard OBD-II (SAE J1979) request (e.g. service=1, pid="0x0D"
        for vehicle speed) and return the first ECU response, decoded where the
        PID is one the simulator implements. `service` and `pid` may be integers
        or hex strings."""
        timeout_s = _capped_duration(timeout_s)
        raw_bus = make_bus(settings.can_interface, settings.can_channel)
        try:
            service_id = parse_int(service)
            bus = guard.wrap(
                raw_bus, "send_obd_request", write=service_id in OBD_WRITE_SERVICES
            )
            arb_id, data = build_request(service_id, parse_int(pid) if pid is not None else None)
            bus.send(can.Message(arbitration_id=arb_id, data=data, is_extended_id=False))
            # The bus also carries periodic ECU traffic: wait for the frame
            # that actually answers this request, not just the next one.
            msg = wait_for_response(bus, service_id, timeout_s)
            if not msg:
                return ObdResponse(status="timeout", message="No OBD-II response within timeout_s")
            response_service, resp_pid, value_bytes = parse_response(msg.data)
            return ObdResponse(
                status="success",
                arbitration_id=hex(msg.arbitration_id),
                response_service=response_service,
                pid=resp_pid,
                decoded=decode_response(response_service, resp_pid, value_bytes),
                raw_data=list(msg.data),
            )
        except TransmitBlocked as e:
            return ObdResponse(status="blocked", message=str(e))
        except Exception as e:
            return ObdResponse(status="error", message=str(e))
        finally:
            shutdown_bus(raw_bus)

    @mcp.tool()
    def send_diagnostic_request(
        service_id: IntLike,
        parameter_id: IntLike = 0,
        data_field: IntLike = 0,
        timeout_s: float = 2.0,
    ) -> DiagnosticResult:
        """Send a UDS-style diagnostic request (see vehicle.dbc's DIAGNOSTIC_REQUEST
        SERVICE_ID choices, e.g. "0x22"=READ_DATA_BY_ID, "0x11"=RESET_ECU) and
        collect responses from every ECU that answers within timeout_s. The ID
        arguments may be integers or hex strings."""
        timeout_s = _capped_duration(timeout_s)
        request_msg = db.get_message_by_name(REQUEST_MESSAGE)
        response_frame_ids = {
            db.get_message_by_name(name).frame_id: name for name in RESPONSE_MESSAGES
        }
        raw_bus = make_bus(settings.can_interface, settings.can_channel)
        try:
            bus = guard.wrap(
                raw_bus,
                "send_diagnostic_request",
                write=is_diag_write_service(parse_int(service_id)),
            )
            payload = request_msg.encode(
                {
                    "SERVICE_ID": parse_int(service_id),
                    "PARAMETER_ID": parse_int(parameter_id),
                    "DATA_FIELD": parse_int(data_field),
                }
            )
            bus.send(
                can.Message(
                    arbitration_id=request_msg.frame_id, data=payload, is_extended_id=False
                )
            )
            responses: List[DiagnosticEcuResponse] = []
            end = time.time() + timeout_s
            while time.time() < end:
                msg = bus.recv(timeout=0.1)
                if msg and msg.arbitration_id in response_frame_ids:
                    decoded = decode_frame(db, msg.arbitration_id, msg.data)
                    responses.append(
                        DiagnosticEcuResponse(
                            ecu=ecu_name_from_response_message(
                                response_frame_ids[msg.arbitration_id]
                            ),
                            service_id=signal_int(decoded.get("SERVICE_ID", 0)),
                            parameter_id=signal_int(decoded.get("PARAMETER_ID", 0)),
                            response_code=response_code_name(
                                signal_int(decoded.get("RESPONSE_CODE", 0))
                            ),
                            data_field=signal_int(decoded.get("DATA_FIELD", 0)),
                        )
                    )
            if not responses:
                return DiagnosticResult(
                    status="timeout", message="No ECU responded within timeout_s"
                )
            return DiagnosticResult(status="success", responses=responses)
        except TransmitBlocked as e:
            return DiagnosticResult(status="blocked", message=str(e))
        except Exception as e:
            return DiagnosticResult(status="error", message=str(e))
        finally:
            shutdown_bus(raw_bus)

    @mcp.tool()
    def activate_fault_scenario(
        preset: Optional[str] = None,
        timeout_s: float = 2.0,
    ) -> FaultScenarioResult:
        """Activate a fault-injection scenario in the simulator, or clear the
        active one by passing preset=None. Presets: "overheat", "abs_fault",
        "low_fuel", "crash", "door_ajar", "misfire", "battery_low" (each
        result includes a description). Where a preset sets DTCs they show up
        in send_obd_request service=3, uds_read_dtcs and (for engine faults)
        read_j1939_dtcs. Only affects a simulator sharing this process's
        virtual bus (`peykan demo`)."""
        timeout_s = _capped_duration(timeout_s)
        if preset is not None and preset not in PRESETS:
            return FaultScenarioResult(
                status="error",
                message=f"Unknown preset {preset!r}. Choices: {', '.join(PRESETS)}",
            )
        raw_bus = make_bus(settings.can_interface, settings.can_channel)
        try:
            bus = guard.wrap(raw_bus, "activate_fault_scenario", write=True)
            arb_id, data = build_control_frame(preset)
            bus.send(can.Message(arbitration_id=arb_id, data=data, is_extended_id=False))
            end = time.time() + timeout_s
            while time.time() < end:
                msg = bus.recv(timeout=0.1)
                if msg and msg.arbitration_id == FAULT_ACK_ID:
                    active = PRESETS.get(preset) if preset else None
                    return FaultScenarioResult(
                        status="success",
                        active_preset=preset,
                        description=active.description if active else None,
                        dtcs=list(active.dtcs) if active else [],
                    )
            return FaultScenarioResult(
                status="timeout", message="No ack from simulator within timeout_s"
            )
        except TransmitBlocked as e:
            return FaultScenarioResult(status="blocked", message=str(e))
        except Exception as e:
            return FaultScenarioResult(status="error", message=str(e))
        finally:
            shutdown_bus(raw_bus)

    def _decode_j1939(arbitration_id: int, data: bytes) -> J1939DecodeResult:
        parsed = j1939.parse_can_id(arbitration_id)
        definition = j1939.PGN_CATALOG.get(parsed.pgn)
        return J1939DecodeResult(
            status="success",
            priority=parsed.priority,
            pgn=parsed.pgn,
            pgn_hex=f"0x{parsed.pgn:04X}",
            pgn_name=definition.name if definition else None,
            source_address=parsed.source_address,
            destination_address=parsed.destination_address,
            is_broadcast=parsed.is_broadcast,
            signals=j1939.decode_pgn(parsed.pgn, data) or None,
        )

    @mcp.tool()
    def decode_j1939_frame(arbitration_id: IntLike, data: List[int]) -> J1939DecodeResult:
        """Decode a 29-bit J1939 frame: split the extended ID (integer or hex
        string like "0x0CF00400") into priority / PGN / source + destination
        address, then decode known SPNs from the payload (see `list_j1939_pgns`
        for the supported catalog). DM1 frames return their active DTC list."""
        try:
            return _decode_j1939(parse_int(arbitration_id), bytes(data))
        except Exception as e:
            return J1939DecodeResult(status="error", message=str(e))

    @mcp.tool()
    def list_j1939_pgns() -> J1939PgnCatalog:
        """The catalog of J1939 Parameter Group Numbers this server can decode
        and (for `request_j1939_pgn`) request from the simulator, each with
        its Suspect Parameter Numbers, bit layout, scaling and units."""
        pgns = [
            J1939PgnInfo(**j1939.describe_pgn(pgn))
            for pgn, definition in j1939.PGN_CATALOG.items()
            if definition.spns
        ]
        return J1939PgnCatalog(pgns=pgns)

    @mcp.tool()
    def request_j1939_pgn(pgn: IntLike, timeout_s: float = 2.0) -> J1939RequestResult:
        """Send a J1939 Request PGN (0xEA00) asking every ECU to transmit
        `pgn` and return the decoded responses seen within timeout_s. Pass
        `pgn` as an acronym ("EEC1" for engine speed, "ET1" for coolant
        temperature), a hex string ("0xF004") or an integer; see
        `list_j1939_pgns`. Needs a simulator on this process's bus
        (`peykan demo`)."""
        timeout_s = _capped_duration(timeout_s)
        raw_bus = make_bus(settings.can_interface, settings.can_channel)
        try:
            bus = guard.wrap(raw_bus, "request_j1939_pgn")
            requested = j1939.resolve_pgn(pgn)
            can_id, data = j1939.build_request_pgn(requested)
            bus.send(can.Message(arbitration_id=can_id, data=data, is_extended_id=True))
            responses: List[J1939DecodeResult] = []
            end = time.time() + timeout_s
            while time.time() < end:
                msg = bus.recv(timeout=0.1)
                if not msg or not msg.is_extended_id:
                    continue
                if j1939.parse_can_id(msg.arbitration_id).pgn == requested:
                    responses.append(_decode_j1939(msg.arbitration_id, bytes(msg.data)))
            if not responses:
                return J1939RequestResult(
                    status="timeout",
                    requested_pgn=requested,
                    requested_pgn_hex=f"0x{requested:04X}",
                    message=(
                        f"No ECU transmitted PGN 0x{requested:04X} within timeout_s. "
                        f"Known PGNs: {j1939.known_pgns_summary()}"
                    ),
                )
            return J1939RequestResult(
                status="success",
                requested_pgn=requested,
                requested_pgn_hex=f"0x{requested:04X}",
                responses=responses,
            )
        except TransmitBlocked as e:
            return J1939RequestResult(status="blocked", message=str(e))
        except Exception as e:
            return J1939RequestResult(status="error", message=str(e))
        finally:
            shutdown_bus(raw_bus)

    @mcp.tool()
    def read_j1939_dtcs(duration_s: float = 3.0) -> J1939DtcResult:
        """Read the most recent J1939 DM1 (active diagnostic trouble codes)
        from every ECU that broadcast one in the last `duration_s` seconds:
        lamp status plus every active SPN/FMI, each tagged with the source
        address of the ECU reporting it. Served from the frame history buffer -- see
        `read_can_frames`. An empty `dtcs` list means no active faults."""
        duration_s = _capped_duration(duration_s)
        # latest_dm1 also reassembles BAM: a DM1 with 2+ DTCs is longer than
        # one frame and arrives as a TP.CM announcement + TP.DT packets.
        by_source = j1939.latest_dm1_by_source(
            live_state.frames_since(time.time() - duration_s)
        )
        if not by_source:
            return J1939DtcResult(
                status="timeout", message="No DM1 broadcast seen within duration_s"
            )
        merged = j1939.merge_dm1s(by_source)
        return J1939DtcResult(
            status="success",
            source_address=next(iter(by_source)) if len(by_source) == 1 else None,
            lamps=merged["lamps"],
            dtcs=[J1939Dtc(**d.as_dict(), source_address=sa) for sa, d in merged["dtcs"]],
            ecus=[
                {"source_address": sa, "lamps": dm1["lamps"], "active_dtcs": len(dm1["dtcs"])}
                for sa, dm1 in sorted(by_source.items())
            ],
        )

    # ---------------------------------------------------------- ISO-TP / UDS
    def _uds_failure(result_type: Any, exc: Exception) -> Any:
        if isinstance(exc, TransmitBlocked):
            return result_type(status="blocked", message=str(exc))
        if isinstance(exc, uds.NegativeResponseException):
            return result_type(status="negative_response", message=uds.uds_error(exc))
        if isinstance(exc, (uds.TimeoutException, TimeoutError)):
            return result_type(status="timeout", message=uds.uds_error(exc))
        return result_type(status="error", message=str(exc))

    @mcp.tool()
    def read_vin(timeout_s: float = 2.0) -> VinResult:
        """Read the Vehicle Identification Number via OBD-II Mode 09 PID 02
        (a multi-frame ISO-TP response), with a check-digit validity flag."""
        try:
            result = uds.read_vin_obd(settings, guard, _capped_duration(timeout_s))
            return VinResult(status="success", **result)
        except Exception as e:
            return _uds_failure(VinResult, e)

    @mcp.tool()
    def uds_read_data(
        dids: Union[List[IntLike], IntLike],
        request_id: IntLike = "0x7E0",
        response_id: IntLike = "0x7E8",
        timeout_s: float = 2.0,
    ) -> UdsDataResult:
        """UDS ReadDataByIdentifier (0x22) over ISO-TP. `dids` are data
        identifiers as hex strings, e.g. ["0xF190"] (VIN), "0xF18C" (ECU
        serial), "0xF195" (software version), "0xF40C" (engine RPM),
        "0xF40D" (speed), "0xF405" (coolant), "0xF442" (voltage). Defaults
        address the engine ECU (request 0x7E0, response 0x7E8)."""
        try:
            with uds.uds_client(
                settings, guard, "uds_read_data", False,
                parse_int(request_id), parse_int(response_id), _capped_duration(timeout_s),
            ) as client:
                did_list = dids if isinstance(dids, list) else [dids]
                values = uds.read_dids(client, [parse_int(d) for d in did_list])
            return UdsDataResult(status="success", values=values)
        except Exception as e:
            return _uds_failure(UdsDataResult, e)

    @mcp.tool()
    def uds_read_dtcs(
        status_mask: IntLike = "0xFF",
        request_id: IntLike = "0x7E0",
        response_id: IntLike = "0x7E8",
        timeout_s: float = 2.0,
    ) -> UdsDtcResult:
        """UDS ReadDTCInformation (0x19, reportDTCByStatusMask): stored DTCs
        with their status bits. "test_failed" means the fault is present now;
        "confirmed" alone means it happened earlier and is stored in memory
        until cleared."""
        try:
            with uds.uds_client(
                settings, guard, "uds_read_dtcs", False,
                parse_int(request_id), parse_int(response_id), _capped_duration(timeout_s),
            ) as client:
                dtcs = uds.read_dtcs(client, parse_int(status_mask))
            return UdsDtcResult(status="success", dtcs=dtcs)
        except Exception as e:
            return _uds_failure(UdsDtcResult, e)

    @mcp.tool()
    def uds_clear_dtcs(
        request_id: IntLike = "0x7E0",
        response_id: IntLike = "0x7E8",
        timeout_s: float = 2.0,
    ) -> UdsClearResult:
        """UDS ClearDiagnosticInformation (0x14) for all DTC groups. Changes
        ECU state: only call it when the user asks to clear codes. Faults
        that are still present are detected again immediately."""
        try:
            with uds.uds_client(
                settings, guard, "uds_clear_dtcs", True,
                parse_int(request_id), parse_int(response_id), _capped_duration(timeout_s),
            ) as client:
                client.clear_dtc(0xFFFFFF)
            return UdsClearResult(status="success")
        except Exception as e:
            return _uds_failure(UdsClearResult, e)

    @mcp.tool()
    def get_transmit_log(limit: int = 20) -> TransmitLogResult:
        """The transmit policy (is sending allowed on this interface, are
        state-changing services allowed, ID allowlist) and the most recent
        frames the tools sent or were blocked from sending."""
        return TransmitLogResult(policy=guard.policy(), records=guard.records(max(1, limit)))

    # ---------------------------------------------------------- recorded logs
    @mcp.tool()
    def list_can_logs() -> LogListResult:
        """CAN log files available to analyze (in PEYKAN_LOG_DIR), plus the
        bundled "sample" drive recording."""
        return LogListResult(
            log_dir=str(Path(settings.log_dir).resolve()), files=logs.list_logs(settings.log_dir)
        )

    @mcp.tool()
    def analyze_can_log(path: str) -> LogAnalysisResult:
        """Summarise a recorded CAN log (.asc, .blf, .trc, candump .log, .csv):
        duration, every arbitration ID with its rate, decoded signal ranges
        (min/max/mean/first/last), trouble codes seen (OBD-II and J1939 DM1)
        and timing gaps. `path` is relative to PEYKAN_LOG_DIR, or "sample"."""
        try:
            log = logs.load_log(logs.resolve_log_path(path, settings.log_dir))
            return LogAnalysisResult(status="success", analysis=logs.analyze_log(log, db))
        except Exception as e:
            return LogAnalysisResult(status="error", message=str(e))

    @mcp.tool()
    def get_log_signal(
        path: str, signal_name: str, max_points: int = 100, message: Optional[str] = None
    ) -> LogSignalResult:
        """One decoded signal's values over a recorded log, as (time, value)
        points downsampled to at most `max_points`, plus min/max/mean. Use
        analyze_can_log first to see which signals the log contains; when a
        name occurs in several messages, `message` (e.g. "J1939:EEC1")
        picks one."""
        try:
            log = logs.load_log(logs.resolve_log_path(path, settings.log_dir))
            points = max(2, min(max_points, 1000))
            series = logs.signal_series(log, db, signal_name, points, message)
            return LogSignalResult(status="success", series=series)
        except Exception as e:
            return LogSignalResult(status="error", message=str(e))

    @mcp.tool()
    def replay_can_log(path: str, speed: float = 1.0, loop: bool = False) -> ReplayStatusResult:
        """Replay a recorded log onto the bus with its original timing
        (`speed` 2.0 = twice as fast), so the live tools and the dashboard
        work on it. Transmits every frame in the log, so it counts as a
        state-changing action under the transmit policy."""
        try:
            log = logs.load_log(logs.resolve_log_path(path, settings.log_dir))
            first_id = log.frames[0]["arbitration_id"] if log.frames else 0
            guard.check(first_id, "replay_can_log", write=True)
            current = replay["current"]
            if current is not None:
                current.stop()
            bus = guard.wrap(
                make_bus(settings.can_interface, settings.can_channel), "replay_can_log", True
            )
            replayer = logs.LogReplayer(log, bus, speed=speed, loop=loop)
            replayer.start()
            replay["current"] = replayer
            return ReplayStatusResult(status="success", replay=replayer.status())
        except TransmitBlocked as e:
            return ReplayStatusResult(status="blocked", message=str(e))
        except Exception as e:
            return ReplayStatusResult(status="error", message=str(e))

    @mcp.tool()
    def stop_log_replay() -> ReplayStatusResult:
        """Stop the log replay started by replay_can_log (and report how far it got)."""
        current = replay["current"]
        if current is None:
            return ReplayStatusResult(status="success", message="No replay running")
        current.stop()
        current.join(timeout=2.0)
        return ReplayStatusResult(status="success", replay=current.status())

    @mcp.resource("file://vehicle.dbc")
    def dbc_info() -> Dict[str, Any]:
        info: Dict[str, Any] = {}
        try:
            info['status'] = 'success'
            info['version'] = db.version if db.version else 'N/A'
            info['nodes'] = [node.name for node in db.nodes]
            messages_info = []
            for msg in db.messages:
                message_details: Dict[str, Any] = {
                    'name': msg.name,
                    'id': msg.frame_id,
                    'id_hex': hex(msg.frame_id),
                    'length': msg.length,
                    'cycle_time_ms': msg.cycle_time,
                    'senders': msg.senders,
                    'signals': []
                }
                for sig in msg.signals:
                    signal_details = {
                        'name': sig.name,
                        'start_bit': sig.start,
                        'length_bits': sig.length,
                        'scale': sig.scale,
                        'offset': sig.offset,
                        'minimum': sig.minimum,
                        'maximum': sig.maximum,
                        'unit': sig.unit,
                        'choices': sig.choices,
                        'is_signed': sig.is_signed,
                        'is_float': sig.is_float,
                        'byte_order': sig.byte_order,
                        'receivers': sig.receivers,
                    }
                    message_details['signals'].append(signal_details)
                messages_info.append(message_details)
            info['messages'] = messages_info
        except FileNotFoundError:
            info['status'] = 'error'
            info['message'] = "DBC file not found"
        except Exception as e:
            info['status'] = 'error'
            info['message'] = f"An unexpected error occurred: {e}"
        return info

    # Health/compat endpoints for clients that probe OAuth discovery.
    @mcp.custom_route("/.well-known/oauth-authorization-server/sse", methods=["GET", "OPTIONS"])
    async def _auth_discovery(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "auth": False})

    @mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET", "OPTIONS"])
    async def _protected_discovery(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "auth": False})

    @mcp.custom_route("/healthz", methods=["GET"])
    async def _healthz(_: Request) -> JSONResponse:
        try:
            has_messages = len(db.messages) > 0
        except Exception:
            has_messages = False
        healthy = has_messages
        return JSONResponse(
            {"status": "ok" if healthy else "error", "dbc_loaded": has_messages},
            status_code=200 if healthy else 503,
        )

    # Read-only live dashboard: a static page (below) polling this SSE stream.
    # Only shows data when a simulator shares this process's virtual bus
    # (i.e. `peykan demo`) -- see live_state.py.
    # Someone opening http://localhost:<port>/ in a browser wants the
    # dashboard, not a bare 404.
    @mcp.custom_route("/", methods=["GET"])
    async def _root(_: Request) -> RedirectResponse:
        return RedirectResponse("/dashboard")

    @mcp.custom_route("/dashboard", methods=["GET"])
    async def _dashboard(_: Request) -> HTMLResponse:
        return HTMLResponse(_DASHBOARD_HTML)

    @mcp.custom_route("/dashboard/stream", methods=["GET"])
    async def _dashboard_stream(_: Request) -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            while True:
                yield f"data: {json.dumps(live_state.snapshot())}\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(events(), media_type="text/event-stream")

    return mcp


def _transport_security(settings: Settings) -> Optional[TransportSecuritySettings]:
    """DNS-rebinding protection for loopback binds, as the SDK enables by
    default, but also admitting any origins configured in
    `cors_allow_origins` so narrowing CORS to a real host still works."""
    if settings.mcp_host not in LOOPBACK_HOSTS:
        return None  # an explicit network bind; Host headers will vary
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    if settings.cors_allow_origins != ["*"]:
        origins += settings.cors_allow_origins
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
        allowed_origins=origins,
    )


def build_http_app(mcp: MCPServer, settings: Settings) -> Starlette:
    """The ASGI app `main()` serves: the MCP endpoint for the configured
    transport plus the dashboard/health routes, with CORS.

    CORS lets browser-based MCP hosts (e.g. MCP Inspector) reach the server.
    Origins default to "*" for the zero-friction demo experience; see
    Settings.cors_allow_origins. Credentials are only allowed once that's
    narrowed to specific origins -- wildcard-origin + allow_credentials is a
    combination browsers reject outright.
    """
    security = _transport_security(settings)
    if settings.mcp_transport == "streamable-http":
        app = mcp.streamable_http_app(host=settings.mcp_host, transport_security=security)
    else:
        app = mcp.sse_app(host=settings.mcp_host, transport_security=security)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=settings.cors_allow_origins != ["*"],
    )
    return app


def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    mcp = create_app()
    if settings.mcp_transport == "stdio":
        mcp.run("stdio")
        return

    host, port = settings.mcp_host, settings.mcp_port
    logger.info(
        "Starting Peykan server on %s:%s (transport=%s)", host, port, settings.mcp_transport
    )
    if host not in LOOPBACK_HOSTS:
        logger.warning(
            "Listening on %s: other machines on the network can reach this server, "
            "and its tools have no authentication. Use PEYKAN_MCP_HOST=127.0.0.1 "
            "unless that's intended (e.g. inside Docker).",
            host,
        )
    endpoint = "/mcp" if settings.mcp_transport == "streamable-http" else "/sse"
    logger.info("Dashboard:    http://localhost:%s/dashboard", port)
    logger.info("MCP endpoint: http://localhost:%s%s", port, endpoint)
    uvicorn.run(
        build_http_app(mcp, settings),
        host=host,
        port=port,
        log_level=settings.log_level.lower(),
    )
