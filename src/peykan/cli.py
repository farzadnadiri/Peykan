import json
import os
import threading
import time as _time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, List, Optional

import can
import typer
from rich.console import Console
from rich.table import Table

from . import j1939, logs, uds
from .bus import make_bus, read_frames, shutdown_bus
from .config import configure_logging, get_settings
from .dbc import decode_frame, load_dbc, signal_int
from .diagnostics import (
    REQUEST_MESSAGE,
    RESPONSE_MESSAGES,
    ecu_name_from_response_message,
    response_code_name,
)
from .obd import build_request, decode_response, parse_response, wait_for_response
from .parsing import parse_int
from .safety import OBD_WRITE_SERVICES, UDS_WRITE_SERVICES, TransmitBlocked, TransmitGuard
from .server.mcp_server import main as run_server
from .simulator.faults import FAULT_ACK_ID, PRESETS, build_control_frame
from .simulator.runner import run_simulator

app = typer.Typer(help="Peykan: simulate, inspect and serve CAN data over MCP.")
console = Console()


@app.callback()
def _main_callback() -> None:
    configure_logging()


def legacy_main() -> None:
    """Entry point for the deprecated `mcp-can` command."""
    import sys

    print(
        "Note: the mcp-can command is deprecated; this project is now called "
        "Peykan, use `peykan` instead.",
        file=sys.stderr,
    )
    app()


@contextmanager
def _transmit_bus(purpose: str, write: bool = False) -> Iterator[can.BusABC]:
    """A bus whose sends go through the transmit policy (see safety.py);
    a refusal ends the command with exit code 2 and the policy's reason."""
    settings = get_settings()
    raw = make_bus(settings.can_interface, settings.can_channel)
    try:
        yield TransmitGuard(settings).wrap(raw, purpose, write)
    except TransmitBlocked as e:
        console.print(f"[red]Blocked by the transmit policy: {e}[/red]")
        raise typer.Exit(code=2)
    finally:
        shutdown_bus(raw)


def _log_path(file: str) -> Path:
    return logs.SAMPLE_LOG if file == "sample" else Path(file)


@app.command()
def server(
    host: Optional[str] = typer.Option(
        None,
        help="Interface to listen on (default 127.0.0.1; 0.0.0.0 exposes it to the network)",
    ),
    port: Optional[int] = typer.Option(None, help="MCP server port (default from env)"),
    transport: Optional[str] = typer.Option(
        None, help="MCP transport: sse | streamable-http | stdio (default from env)"
    ),
) -> None:
    if host is not None:
        os.environ["PEYKAN_MCP_HOST"] = host
    if port is not None:
        os.environ["PEYKAN_MCP_PORT"] = str(port)
    if transport is not None:
        os.environ["PEYKAN_MCP_TRANSPORT"] = transport
    run_server()


@app.command()
def simulate() -> None:
    """Run the ECU simulator using the configured DBC."""
    run_simulator()


@app.command()
def frames(seconds: float = typer.Option(1.0, help="Duration to listen on CAN bus")) -> None:
    """Capture raw CAN frames for a period and print JSON."""
    settings = get_settings()
    bus = make_bus(settings.can_interface, settings.can_channel)
    try:
        frames_list = read_frames(bus, seconds)
        out = [
            {
                "timestamp": f.timestamp,
                "arbitration_id": hex(f.arbitration_id),
                "data": list(f.data),
            }
            for f in frames_list
        ]
        typer.echo(json.dumps(out, indent=2))
    finally:
        shutdown_bus(bus)


@app.command()
def decode(
    id: str,
    data: str,
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON instead of a table"),
) -> None:
    """Decode a CAN frame given an ID and data bytes.

    id: CAN ID in hex (e.g. 0x100) or decimal.
    data: comma-separated bytes (e.g. 01,02,03,04) or space-separated hex (e.g. 01 02 03 04)
    """
    settings = get_settings()
    db = load_dbc(settings.dbc_path)
    arb_id = parse_int(id)
    bytes_list: List[int] = []
    if "," in data:
        bytes_list = [
            int(x.strip(), 16 if x.strip().startswith("0x") else 10)
            for x in data.split(",")
            if x.strip()
        ]
    else:
        parts = [p for p in data.replace(",", " ").split(" ") if p]
        bytes_list = [
            int(x.strip(), 16 if all(c in "0123456789abcdefABCDEF" for c in x) else 10)
            for x in parts
        ]
    decoded = decode_frame(db, arb_id, bytes(bytes_list))
    if json_output:
        typer.echo(json.dumps(decoded, indent=2))
        return
    message = db.get_message_by_frame_id(arb_id)
    unit_by_name = {sig.name: sig.unit for sig in message.signals}
    table = Table(title=f"{message.name}  (id={id})")
    table.add_column("Signal")
    table.add_column("Value")
    table.add_column("Unit")
    for name, value in decoded.items():
        table.add_row(name, str(value), unit_by_name.get(name) or "-")
    console.print(table)


@app.command()
def monitor(
    signal: str,
    seconds: float = typer.Option(2.0, help="Duration to listen"),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON instead of live output"),
) -> None:
    """Monitor a specific signal and print timestamped values."""
    settings = get_settings()
    db = load_dbc(settings.dbc_path)
    bus = make_bus(settings.can_interface, settings.can_channel)
    end = _time.time() + seconds
    out: List[dict] = []
    try:
        if not json_output:
            console.print(f"[bold cyan]Monitoring {signal} for {seconds}s...[/bold cyan]")
        while _time.time() < end:
            msg = bus.recv(timeout=0.1)
            if msg:
                try:
                    decoded = decode_frame(db, msg.arbitration_id, msg.data)
                    if signal in decoded:
                        sample = {"timestamp": msg.timestamp, "value": decoded[signal]}
                        out.append(sample)
                        if not json_output:
                            console.print(
                                f"  [dim]{sample['timestamp']:.3f}[/dim]  "
                                f"{signal} = [green]{sample['value']}[/green]"
                            )
                except Exception:
                    pass
        if json_output:
            typer.echo(json.dumps(out, indent=2))
        elif out:
            values = [s["value"] for s in out if isinstance(s["value"], (int, float))]
            summary = f"[bold]{len(out)} sample(s)[/bold]"
            if values:
                avg = sum(values) / len(values)
                summary += f", min={min(values)} max={max(values)} avg={avg:.2f}"
            console.print(summary)
        else:
            console.print("[yellow]No samples observed.[/yellow]")
    finally:
        shutdown_bus(bus)


@app.command()
def snapshot(
    seconds: float = typer.Option(1.0, help="How long to listen before reporting"),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON instead of a table"),
) -> None:
    """Listen briefly and report the latest value seen for every signal.

    CLI counterpart to the `get_vehicle_snapshot` MCP tool -- since this runs
    as its own process rather than inside the server, it builds its own
    short-lived snapshot instead of reading the server's history buffer.
    """
    settings = get_settings()
    db = load_dbc(settings.dbc_path)
    bus = make_bus(settings.can_interface, settings.can_channel)
    latest: dict = {}
    end = _time.time() + seconds
    try:
        while _time.time() < end:
            msg = bus.recv(timeout=0.1)
            if msg:
                try:
                    decoded = decode_frame(db, msg.arbitration_id, msg.data)
                    message = db.get_message_by_frame_id(msg.arbitration_id)
                except Exception:
                    continue
                unit_by_name = {sig.name: sig.unit for sig in message.signals}
                for name, value in decoded.items():
                    latest[name] = {
                        "value": value,
                        "unit": unit_by_name.get(name) or "",
                        "message": message.name,
                    }
        if json_output:
            typer.echo(json.dumps(latest, indent=2, default=str))
            return
        if not latest:
            console.print("[yellow]No signals observed.[/yellow]")
            return
        table = Table(title=f"Vehicle Snapshot (last {seconds}s)")
        table.add_column("Signal")
        table.add_column("Value")
        table.add_column("Unit")
        table.add_column("Message")
        for name in sorted(latest):
            info = latest[name]
            table.add_row(name, str(info["value"]), info["unit"] or "-", info["message"])
        console.print(table)
    finally:
        shutdown_bus(bus)


@app.command("dbc-info")
def dbc_info_cmd(
    message: Optional[str] = typer.Argument(None, help="Show only this message's signals"),
) -> None:
    """Pretty-print the loaded DBC's messages and signals."""
    settings = get_settings()
    db = load_dbc(settings.dbc_path)
    messages = [m for m in db.messages if message is None or m.name == message]
    if not messages:
        console.print(f"[red]No message named '{message}' found in {settings.dbc_path}[/red]")
        raise typer.Exit(code=1)
    for msg in messages:
        table = Table(title=f"{msg.name}  (id=0x{msg.frame_id:03x}, {msg.length} bytes)")
        table.add_column("Signal")
        table.add_column("Bits")
        table.add_column("Scale")
        table.add_column("Offset")
        table.add_column("Range")
        table.add_column("Unit")
        for sig in msg.signals:
            rng = f"{sig.minimum}..{sig.maximum}" if sig.minimum is not None else "-"
            table.add_row(
                sig.name,
                f"{sig.start}:{sig.length}",
                str(sig.scale),
                str(sig.offset),
                rng,
                sig.unit or "-",
            )
        console.print(table)


@app.command("obd-request")
def obd_request(
    service: str = typer.Option(..., "--service", "-s", help="Service ID (hex like 0x01)"),
    pid: Optional[str] = typer.Option(None, "--pid", "-p", help="PID hex like 0x0D"),
    timeout: float = 1.0,
) -> None:
    """Send a basic OBD-II (SAE J1979) request and print the first response as JSON."""
    svc = parse_int(service)
    parsed_pid: Optional[int] = parse_int(pid) if pid is not None else None
    with _transmit_bus("obd-request", write=svc in OBD_WRITE_SERVICES) as bus:
        arb_id, data = build_request(svc, parsed_pid)
        bus.send(can.Message(arbitration_id=arb_id, data=data, is_extended_id=False))
        msg = wait_for_response(bus, svc, timeout)
        if not msg:
            typer.echo(json.dumps({"status": "timeout"}))
            raise typer.Exit(code=1)
        response_service, resp_pid, value_bytes = parse_response(msg.data)
        out = {
            "arbitration_id": hex(msg.arbitration_id),
            "data": [int(b) for b in msg.data],
            "decoded": decode_response(response_service, resp_pid, value_bytes),
        }
        typer.echo(json.dumps(out, indent=2))


@app.command("diag-request")
def diag_request(
    service_id: str = typer.Option(
        ..., "--service-id", "-s", help="Diagnostic service ID (hex like 0x22)"
    ),
    parameter_id: str = typer.Option(
        "0x00", "--parameter-id", "-p", help="Parameter ID (hex like 0x01)"
    ),
    data_field: str = typer.Option("0x00", "--data-field", help="Request data field (hex/int)"),
    timeout: float = 2.0,
) -> None:
    """Send a UDS-style diagnostic request (see vehicle.dbc DIAGNOSTIC_REQUEST) and
    print every ECU's response as JSON."""
    settings = get_settings()
    db = load_dbc(settings.dbc_path)
    write = parse_int(service_id) in UDS_WRITE_SERVICES
    with _transmit_bus("diag-request", write=write) as bus:
        request_msg = db.get_message_by_name(REQUEST_MESSAGE)
        response_frame_ids = {
            db.get_message_by_name(name).frame_id: name for name in RESPONSE_MESSAGES
        }
        payload = request_msg.encode(
            {
                "SERVICE_ID": parse_int(service_id),
                "PARAMETER_ID": parse_int(parameter_id),
                "DATA_FIELD": parse_int(data_field),
            }
        )
        bus.send(
            can.Message(arbitration_id=request_msg.frame_id, data=payload, is_extended_id=False)
        )
        responses = []
        end = _time.time() + timeout
        while _time.time() < end:
            msg = bus.recv(timeout=0.1)
            if msg and msg.arbitration_id in response_frame_ids:
                decoded = decode_frame(db, msg.arbitration_id, msg.data)
                ecu = ecu_name_from_response_message(response_frame_ids[msg.arbitration_id])
                responses.append(
                    {
                        "ecu": ecu,
                        "service_id": signal_int(decoded.get("SERVICE_ID", 0)),
                        "parameter_id": signal_int(decoded.get("PARAMETER_ID", 0)),
                        "response_code": response_code_name(
                            signal_int(decoded.get("RESPONSE_CODE", 0))
                        ),
                        "data_field": signal_int(decoded.get("DATA_FIELD", 0)),
                    }
                )
        if not responses:
            typer.echo(json.dumps({"status": "timeout"}))
            raise typer.Exit(code=1)
        typer.echo(json.dumps({"status": "success", "responses": responses}, indent=2))


@app.command("fault")
def fault_scenario(
    preset: str = typer.Argument(
        ..., help="Scenario name to activate, 'clear' to deactivate, or 'list'"
    ),
    timeout: float = 1.0,
) -> None:
    """Activate (or clear) a fault-injection scenario in a running simulator.

    Requires `peykan simulate`/`demo` to already be running: this sends a
    control frame over the bus and waits for the simulator to ack it, the
    same round-trip pattern as `obd-request`/`diag-request`.
    """
    if preset == "list":
        table = Table(title="Fault scenarios")
        table.add_column("Name")
        table.add_column("Description")
        table.add_column("DTCs")
        for p in PRESETS.values():
            table.add_row(p.name, p.description, ", ".join(p.dtcs) or "-")
        console.print(table)
        return
    target: Optional[str] = None if preset == "clear" else preset
    if target is not None and target not in PRESETS:
        console.print(
            f"[red]Unknown scenario '{preset}'. Run 'peykan fault list' to see options.[/red]"
        )
        raise typer.Exit(code=1)
    with _transmit_bus("fault", write=True) as bus:
        arb_id, data = build_control_frame(target)
        bus.send(can.Message(arbitration_id=arb_id, data=data, is_extended_id=False))
        end = _time.time() + timeout
        while _time.time() < end:
            msg = bus.recv(timeout=0.1)
            if msg and msg.arbitration_id == FAULT_ACK_ID:
                console.print(f"[green]Scenario now active: {target or '(cleared)'}[/green]")
                return
        console.print("[yellow]No ack from simulator -- is it running?[/yellow]")
        raise typer.Exit(code=1)


def _parse_data_bytes(data: str) -> List[int]:
    """Parse "01 02 0x03" / "1,2,3" style byte lists (shared by j1939 commands)."""
    parts = [p for p in data.replace(",", " ").split(" ") if p]
    return [
        int(x, 16 if x.lower().startswith("0x") or not x.isdigit() else 10) for x in parts
    ]


@app.command("j1939-decode")
def j1939_decode(
    id: str = typer.Argument(..., help="29-bit extended CAN ID (hex like 0x18F00400)"),
    data: str = typer.Argument(..., help="Payload bytes, space- or comma-separated"),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON instead of a table"),
) -> None:
    """Decompose a J1939 29-bit ID (priority / PGN / addresses) and decode known SPNs."""
    arb_id = parse_int(id)
    payload = bytes(_parse_data_bytes(data))
    parsed = j1939.parse_can_id(arb_id)
    definition = j1939.PGN_CATALOG.get(parsed.pgn)
    signals = j1939.decode_pgn(parsed.pgn, payload)
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "priority": parsed.priority,
                    "pgn": parsed.pgn,
                    "pgn_hex": f"0x{parsed.pgn:04X}",
                    "pgn_name": definition.name if definition else None,
                    "source_address": parsed.source_address,
                    "destination_address": parsed.destination_address,
                    "is_broadcast": parsed.is_broadcast,
                    "signals": signals,
                },
                indent=2,
                default=str,
            )
        )
        return
    header = definition.name if definition else "unknown PGN"
    table = Table(title=f"J1939  {header}  (PGN 0x{parsed.pgn:04X})")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("priority", str(parsed.priority))
    table.add_row("source address", f"0x{parsed.source_address:02X}")
    da = parsed.destination_address
    table.add_row("destination", "broadcast" if da is None else f"0x{da:02X}")
    for name, value in signals.items():
        table.add_row(name, str(value))
    console.print(table)


@app.command("j1939-pgns")
def j1939_pgns() -> None:
    """List the J1939 PGNs/SPNs this project can decode."""
    for pgn, definition in j1939.PGN_CATALOG.items():
        if not definition.spns:
            continue
        table = Table(title=f"{definition.acronym} - {definition.name}  (PGN 0x{pgn:04X})")
        table.add_column("SPN")
        table.add_column("Name")
        table.add_column("Bits")
        table.add_column("Scale")
        table.add_column("Offset")
        table.add_column("Unit")
        for s in definition.spns:
            table.add_row(
                str(s.spn),
                s.name,
                f"{s.start_bit}:{s.length_bits}",
                str(s.scale),
                str(s.offset),
                s.unit or "-",
            )
        console.print(table)


@app.command("j1939-request")
def j1939_request(
    pgn: str = typer.Argument(
        ..., help="PGN to request: acronym (EEC1), hex (0xF004) or decimal"
    ),
    timeout: float = 2.0,
) -> None:
    """Send a J1939 Request PGN (0xEA00) and print every decoded response."""
    requested = j1939.resolve_pgn(pgn)
    with _transmit_bus("j1939-request") as bus:
        can_id, data = j1939.build_request_pgn(requested)
        bus.send(can.Message(arbitration_id=can_id, data=data, is_extended_id=True))
        responses = []
        end = _time.time() + timeout
        while _time.time() < end:
            msg = bus.recv(timeout=0.1)
            if not msg or not getattr(msg, "is_extended_id", False):
                continue
            parsed = j1939.parse_can_id(msg.arbitration_id)
            if parsed.pgn != requested:
                continue
            responses.append(
                {
                    "source_address": parsed.source_address,
                    "signals": j1939.decode_pgn(parsed.pgn, bytes(msg.data)),
                }
            )
        if not responses:
            typer.echo(json.dumps({"status": "timeout"}))
            raise typer.Exit(code=1)
        typer.echo(json.dumps({"status": "success", "responses": responses}, indent=2, default=str))


@app.command("j1939-dtcs")
def j1939_dtcs(seconds: float = typer.Option(3.0, help="How long to listen for a DM1")) -> None:
    """Listen for a J1939 DM1 broadcast and print its active trouble codes."""
    settings = get_settings()
    bus = make_bus(settings.can_interface, settings.can_channel)
    try:
        frames = []
        end = _time.time() + seconds
        while _time.time() < end:
            msg = bus.recv(timeout=0.1)
            if msg and getattr(msg, "is_extended_id", False):
                frames.append({"arbitration_id": msg.arbitration_id, "data": bytes(msg.data)})
        # Handles both single-frame DM1s and multi-packet (BAM) ones.
        by_source = j1939.latest_dm1_by_source(frames)
        if not by_source:
            typer.echo(json.dumps({"status": "timeout"}))
            raise typer.Exit(code=1)
        merged = j1939.merge_dm1s(by_source)
        typer.echo(
            json.dumps(
                {
                    "status": "success",
                    "lamps": merged["lamps"],
                    "dtcs": [{**d.as_dict(), "source_address": sa} for sa, d in merged["dtcs"]],
                },
                indent=2,
            )
        )
    finally:
        shutdown_bus(bus)


def _uds_options_ids(request_id: str, response_id: str) -> tuple:
    return parse_int(request_id), parse_int(response_id)


@app.command("vin")
def vin(timeout: float = 2.0) -> None:
    """Read the VIN via OBD-II Mode 09 PID 02 (multi-frame ISO-TP)."""
    settings = get_settings()
    try:
        result = uds.read_vin_obd(settings, TransmitGuard(settings), timeout)
    except TransmitBlocked as e:
        console.print(f"[red]Blocked by the transmit policy: {e}[/red]")
        raise typer.Exit(code=2)
    except Exception as e:
        typer.echo(json.dumps({"status": "error", "message": uds.uds_error(e)}))
        raise typer.Exit(code=1)
    typer.echo(json.dumps({"status": "success", **result}, indent=2))


def _run_uds(
    purpose: str,
    write: bool,
    request_id: str,
    response_id: str,
    timeout: float,
    action: Callable[[Any], Any],
) -> Any:
    settings = get_settings()
    req, resp = _uds_options_ids(request_id, response_id)
    try:
        guard = TransmitGuard(settings)
        with uds.uds_client(settings, guard, purpose, write, req, resp, timeout) as client:
            return action(client)
    except TransmitBlocked as e:
        console.print(f"[red]Blocked by the transmit policy: {e}[/red]")
        raise typer.Exit(code=2)
    except Exception as e:
        typer.echo(json.dumps({"status": "error", "message": uds.uds_error(e)}))
        raise typer.Exit(code=1)


@app.command("uds-read")
def uds_read(
    dids: List[str] = typer.Argument(..., help="Data identifiers, e.g. 0xF190 0xF40C"),
    request_id: str = typer.Option("0x7E0", help="ECU request (physical) ID"),
    response_id: str = typer.Option("0x7E8", help="ECU response ID"),
    timeout: float = 2.0,
) -> None:
    """UDS ReadDataByIdentifier (0x22) over ISO-TP."""
    values = _run_uds(
        "uds-read", False, request_id, response_id, timeout,
        lambda c: uds.read_dids(c, [parse_int(d) for d in dids]),
    )
    typer.echo(json.dumps({"status": "success", "values": values}, indent=2))


@app.command("uds-dtcs")
def uds_dtcs(
    status_mask: str = typer.Option("0xFF", help="DTC status mask"),
    request_id: str = typer.Option("0x7E0", help="ECU request (physical) ID"),
    response_id: str = typer.Option("0x7E8", help="ECU response ID"),
    timeout: float = 2.0,
) -> None:
    """UDS ReadDTCInformation (0x19 0x02): stored DTCs with status bits."""
    dtcs = _run_uds(
        "uds-dtcs", False, request_id, response_id, timeout,
        lambda c: uds.read_dtcs(c, parse_int(status_mask)),
    )
    typer.echo(json.dumps({"status": "success", "dtcs": dtcs}, indent=2))


@app.command("uds-clear")
def uds_clear(
    request_id: str = typer.Option("0x7E0", help="ECU request (physical) ID"),
    response_id: str = typer.Option("0x7E8", help="ECU response ID"),
    timeout: float = 2.0,
) -> None:
    """UDS ClearDiagnosticInformation (0x14). Changes ECU state."""
    _run_uds("uds-clear", True, request_id, response_id, timeout, lambda c: c.clear_dtc(0xFFFFFF))
    typer.echo(json.dumps({"status": "success"}))


@app.command("log-info")
def log_info(
    file: str = typer.Argument(..., help="Log file (.asc .blf .trc .log .csv), or 'sample'"),
    json_out: bool = typer.Option(False, "--json", help="Print the full analysis as JSON"),
) -> None:
    """Summarise a recorded CAN log: IDs and rates, signal ranges, DTCs, gaps."""
    settings = get_settings()
    analysis = logs.analyze_log(logs.load_log(_log_path(file)), load_dbc(settings.dbc_path))
    if json_out:
        typer.echo(json.dumps(analysis, indent=2, default=str))
        return
    console.print(
        f"[bold]{analysis['file']}[/bold]: {analysis['frame_count']} frames over "
        f"{analysis['duration_s']} s, {analysis['distinct_ids']} IDs, "
        f"{analysis['error_frames']} error frames"
    )
    ids = Table(title="Arbitration IDs")
    for col in ("ID", "Name", "Count", "Rate (Hz)"):
        ids.add_column(col)
    for entry in analysis["ids"][:25]:
        ids.add_row(
            entry["arbitration_id"],
            entry["name"] or "-",
            str(entry["count"]),
            str(entry.get("rate_hz", "-")),
        )
    console.print(ids)
    sigs = Table(title="Signals")
    for col in ("Signal", "Message", "Min", "Max", "Mean", "Last", "Unit"):
        sigs.add_column(col)
    for sig in analysis["signals"]:
        if "min" in sig:
            stats = [str(sig[k]) for k in ("min", "max", "mean", "last")]
            sigs.add_row(sig["name"], sig["message"] or "-", *stats, sig["unit"])
        else:
            values = ", ".join(sig["values"])
            sigs.add_row(sig["name"], sig["message"] or "-", "", "", "", values, "")
    console.print(sigs)
    for code in analysis["obd_dtcs"]:
        console.print(
            f"[yellow]OBD DTC {code['code']} first seen at {code['first_seen_s']} s[/yellow]"
        )
    for dtc in analysis["j1939_dtcs"]:
        console.print(
            f"[yellow]J1939 DTC SPN {dtc['spn']} FMI {dtc['fmi']} ({dtc['fmi_name']}) "
            f"{dtc['first_seen_s']}-{dtc['last_seen_s']} s[/yellow]"
        )
    for gap in analysis["timing_gaps"]:
        console.print(f"[red]Gap: {gap}[/red]")


@app.command("log-signal")
def log_signal(
    file: str = typer.Argument(..., help="Log file, or 'sample'"),
    signal: str = typer.Argument(..., help="Signal name, e.g. ENGINE_SPEED"),
    max_points: int = typer.Option(50, help="Downsample to at most this many points"),
    message: Optional[str] = typer.Option(
        None, help="Source message when the name occurs in several (e.g. J1939:EEC1)"
    ),
) -> None:
    """Print one signal's values over a recorded log as JSON."""
    settings = get_settings()
    log = logs.load_log(_log_path(file))
    series = logs.signal_series(log, load_dbc(settings.dbc_path), signal, max_points, message)
    typer.echo(json.dumps(series, indent=2, default=str))


@app.command("replay")
def replay(
    file: str = typer.Argument(..., help="Log file, or 'sample'"),
    speed: float = typer.Option(1.0, help="Playback speed factor"),
    loop: bool = typer.Option(False, help="Repeat until Ctrl-C"),
) -> None:
    """Replay a recorded log onto the configured bus (subject to the transmit
    policy: allowed on the virtual bus, needs PEYKAN_ALLOW_TRANSMIT and
    PEYKAN_ALLOW_WRITE_SERVICES on real hardware)."""
    log = logs.load_log(_log_path(file))
    with _transmit_bus("replay", write=True) as bus:
        player = logs.LogReplayer(log, bus, speed=speed, loop=loop)
        player.start()
        try:
            while player.is_alive():
                player.join(timeout=0.5)
        except KeyboardInterrupt:
            player.stop()
        status = player.status()
        if status["error"]:
            console.print(f"[red]Replay stopped: {status['error']}[/red]")
            raise typer.Exit(code=2)
        console.print(f"[green]Replayed {status['frames_sent']} frames[/green]")


@app.command("record")
def record(
    file: str = typer.Argument(..., help="Output log (.asc .blf .log .csv .trc)"),
    seconds: float = typer.Option(30.0, help="How long to record"),
    simulate: bool = typer.Option(False, help="Run the simulator in this process and record it"),
    fault: Optional[str] = typer.Option(None, help="With --simulate: fault preset to inject"),
    fault_at: float = typer.Option(10.0, help="Seconds into the recording to inject --fault"),
) -> None:
    """Record bus traffic to a log file (e.g. a simulated drive with a fault)."""
    settings = get_settings()
    if fault is not None and (not simulate or fault not in PRESETS):
        console.print("[red]--fault needs --simulate and a known preset (peykan fault list)[/red]")
        raise typer.Exit(code=1)
    if simulate:
        threading.Thread(target=run_simulator, daemon=True).start()
        _time.sleep(1.0)  # let the simulator come up before recording starts
    if fault is not None:
        def _inject() -> None:
            _time.sleep(fault_at)
            with _transmit_bus("record --fault", write=True) as bus:
                arb_id, data = build_control_frame(fault)
                bus.send(can.Message(arbitration_id=arb_id, data=data, is_extended_id=False))
        threading.Thread(target=_inject, daemon=True).start()
    bus = make_bus(settings.can_interface, settings.can_channel)
    try:
        count = logs.record_bus(bus, file, seconds)
    finally:
        shutdown_bus(bus)
    console.print(f"[green]Wrote {count} frames to {file}[/green]")


@app.command("demo")
def demo(
    host: Optional[str] = typer.Option(
        None,
        help="Interface to listen on (default 127.0.0.1; 0.0.0.0 exposes it to the network)",
    ),
    port: Optional[int] = typer.Option(None, help="MCP server port (default from env)"),
    transport: Optional[str] = typer.Option(
        None, help="MCP transport: sse | streamable-http | stdio (default from env)"
    ),
    log: Optional[str] = typer.Option(
        None, help="Replay this log file (or 'sample') on a loop instead of simulating"
    ),
) -> None:
    """Run simulator in a background thread and start the MCP server.

    Helps on Windows with virtual backend. With --log, the server sees a
    recorded drive (replayed in a loop) instead of the simulator.
    """
    if log is not None:
        settings = get_settings()
        loaded = logs.load_log(_log_path(log))
        raw = make_bus(settings.can_interface, settings.can_channel)
        bus = TransmitGuard(settings).wrap(raw, "demo --log", write=True)
        logs.LogReplayer(loaded, bus, loop=True).start()
    else:
        sim_thread = threading.Thread(target=run_simulator, daemon=True)
        sim_thread.start()
    if host is not None:
        os.environ["PEYKAN_MCP_HOST"] = host
    if port is not None:
        os.environ["PEYKAN_MCP_PORT"] = str(port)
    if transport is not None:
        os.environ["PEYKAN_MCP_TRANSPORT"] = transport
    run_server()
