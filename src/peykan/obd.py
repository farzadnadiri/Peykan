from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple, Union

OBD_BROADCAST_ID = 0x7DF
OBD_RESPONSE_BASE_ID = 0x7E8  # first ECU response ID

_DTC_CATEGORIES = "PCBU"  # Powertrain / Chassis / Body / Network, per SAE J2012


def encode_dtc(code: str) -> Tuple[int, int]:
    """Encode a J2012-style DTC string (e.g. "P0217") into its 2-byte wire form."""
    category = _DTC_CATEGORIES.index(code[0].upper())
    d1, d2, d3, d4 = (int(c, 16) for c in code[1:5])
    byte_a = (category << 6) | (d1 << 4) | d2
    byte_b = (d3 << 4) | d4
    return byte_a, byte_b


def decode_dtc(byte_a: int, byte_b: int) -> str:
    """Inverse of `encode_dtc`."""
    category = _DTC_CATEGORIES[(byte_a >> 6) & 0x3]
    d1, d2 = (byte_a >> 4) & 0x3, byte_a & 0xF
    d3, d4 = (byte_b >> 4) & 0xF, byte_b & 0xF
    return f"{category}{d1:01X}{d2:01X}{d3:01X}{d4:01X}"


def decode_dtcs(value_bytes: List[int]) -> List[str]:
    """Decode a Mode 03 response's DTC bytes (pairs of bytes, one per code)."""
    return [
        decode_dtc(value_bytes[i], value_bytes[i + 1])
        for i in range(0, len(value_bytes) - 1, 2)
    ]


def _single_frame(payload: List[int]) -> List[int]:
    """Build a single-frame ISO-TP message: [len] + payload, padded to 8 bytes."""
    length = len(payload)
    data = [length & 0xFF] + payload
    while len(data) < 8:
        data.append(0x00)
    return data[:8]


def build_request(service: int, pid: Optional[int] = None) -> Tuple[int, bytes]:
    payload: List[int] = [service]
    if pid is not None:
        payload.append(pid)
    data = _single_frame(payload)
    return (OBD_BROADCAST_ID, bytes(data))


# Mode 01 PIDs `simulate_response` implements.
SUPPORTED_MODE01_PIDS = [0x05, 0x0C, 0x0D, 0x2F, 0x42, 0x51]


def _supported_mask(base: int, pids: List[int]) -> Tuple[int, int, int, int]:
    """4-byte bitmap answering "supported PIDs" request `base` (0x00, 0x20,
    0x40, ...): bit 31 is PID base+1, bit 0 is base+0x20, which also flags
    that the next range has supported PIDs of its own."""
    mask = 0
    for pid in pids:
        if base < pid <= base + 0x20:
            mask |= 1 << (32 - (pid - base))
    if any(pid > base + 0x20 for pid in pids):
        mask |= 1
    return (mask >> 24 & 0xFF, mask >> 16 & 0xFF, mask >> 8 & 0xFF, mask & 0xFF)


def simulate_response(
    service: int,
    pid: Optional[int],
    dtcs: Optional[List[str]] = None,
    state: Any = None,
) -> Optional[List[int]]:
    """Return payload bytes (without length) for a given OBD-II request.

    A small subset, as single-frame responses. `dtcs` is the currently
    active fault codes (see `simulator/faults.py`); a single-frame response
    fits at most 3 (7 payload bytes: 1 for the service id + 2 per code).
    `state` is the simulator's `DrivingState` (fault effects applied); when
    given, Mode 01 values are live rather than fixed demo values. The VIN
    (Mode 09 PID 02) is multi-frame and answered by the ISO-TP ECU in
    `simulator/uds_ecu.py` instead.
    """
    if service == 0x01:
        if pid in (0x00, 0x20, 0x40):
            return [0x41, pid, *_supported_mask(pid, SUPPORTED_MODE01_PIDS)]
        if pid == 0x05:  # Coolant temp = A-40
            temp_c = state.engine_temp_c if state is not None else 90
            return [0x41, 0x05, _byte(round(temp_c) + 40)]
        if pid == 0x0C:  # Engine RPM = (256A+B)/4
            raw = int(round((state.rpm if state is not None else 800) * 4))
            raw = max(0, min(raw, 0xFFFF))
            return [0x41, 0x0C, raw >> 8, raw & 0xFF]
        if pid == 0x0D:  # Speed km/h
            return [0x41, 0x0D, _byte(round(state.speed_kph if state is not None else 50))]
        if pid == 0x2F:  # Fuel tank level input % = 100/255 * A
            level_pct = state.fuel_pct if state is not None else 50
            return [0x41, 0x2F, _byte(round(level_pct * 255 / 100))]
        if pid == 0x42:  # Control module voltage = (256A+B)/1000 V
            raw = int(round((state.battery_v if state is not None else 14.2) * 1000))
            return [0x41, 0x42, (raw >> 8) & 0xFF, raw & 0xFF]
        if pid == 0x51:  # Fuel type (1 = gasoline)
            return [0x41, 0x51, 0x01]
    if service == 0x03:  # DTCs
        payload = [0x43]
        for code in (dtcs or [])[:3]:
            payload.extend(encode_dtc(code))
        return payload
    if service == 0x09:
        if pid == 0x00:
            return [0x49, 0x00, *_supported_mask(0x00, [0x02, 0x0A])]
        if pid == 0x0A:  # ECU name (ASCII), simple short name in single frame
            name = b"MCP-ECU"
            return [0x49, 0x0A] + list(name[:5])  # truncate to fit single-frame demo
    return None


def _byte(value: float) -> int:
    return int(max(0, min(255, value)))


def parse_request(data: Union[bytes, bytearray]) -> Tuple[int, Optional[int]]:
    """Parse a single-frame request and return (service, pid)."""
    if not data:
        return (0, None)
    length = data[0]
    payload = list(data[1:1 + length])
    if not payload:
        return (0, None)
    service = payload[0]
    pid = payload[1] if len(payload) > 1 else None
    return (service, pid)


def build_response_frame(
    payload: List[int],
    responder_id: int = OBD_RESPONSE_BASE_ID,
) -> Tuple[int, bytes]:
    data = _single_frame(payload)
    return (responder_id, bytes(data))


def is_response_to(msg: Any, service: int) -> bool:
    """Whether a received frame is an ECU's answer to OBD-II `service`:
    from a response ID (0x7E8-0x7EF, 11-bit) and carrying either the
    positive response (service + 0x40) or a negative response (0x7F) for
    it. Anything else on the bus -- periodic ECU traffic, J1939 -- isn't."""
    if getattr(msg, "is_extended_id", False):
        return False
    if not OBD_RESPONSE_BASE_ID <= msg.arbitration_id <= OBD_RESPONSE_BASE_ID + 7:
        return False
    data = bytes(msg.data)
    if len(data) < 2 or not 1 <= data[0] <= 7:  # single-frame PCI
        return False
    if data[1] == service + 0x40:
        return True
    return data[1] == 0x7F and len(data) > 2 and data[2] == service


def wait_for_response(bus: Any, service: int, timeout_s: float) -> Any:
    """First frame answering `service` within `timeout_s`, or None."""
    end = time.monotonic() + timeout_s
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return None
        msg = bus.recv(timeout=min(remaining, 0.1))
        if msg is not None and is_response_to(msg, service):
            return msg


def parse_response(data: Union[bytes, bytearray]) -> Tuple[int, Optional[int], List[int]]:
    """Parse a single-frame OBD-II response.

    Returns (response_service, pid, value_bytes). `response_service` is the
    request service + 0x40 (e.g. 0x41 for a Mode 01 reply); `pid` is present
    for Mode 01/09 replies, None otherwise.
    """
    data = bytes(data)
    if not data:
        return (0, None, [])
    length = data[0]
    payload = list(data[1:1 + length])
    if not payload:
        return (0, None, [])
    response_service = payload[0]
    if response_service in (0x41, 0x49) and len(payload) > 1:
        return (response_service, payload[1], payload[2:])
    return (response_service, None, payload[1:])


def decode_pid_value(pid: Optional[int], value_bytes: List[int]) -> Optional[Dict[str, Any]]:
    """Best-effort human-friendly decode for the PIDs `simulate_response` implements.

    Unknown PIDs (or ones with no bytes) return None rather than guessing.
    """
    if pid is None or not value_bytes:
        return None
    a = value_bytes[0]
    if pid == 0x05:
        return {"name": "engine_coolant_temp", "value": a - 40, "unit": "degC"}
    if pid == 0x0C and len(value_bytes) >= 2:
        return {"name": "engine_rpm", "value": (a * 256 + value_bytes[1]) / 4, "unit": "rpm"}
    if pid == 0x42 and len(value_bytes) >= 2:
        volts = (a * 256 + value_bytes[1]) / 1000
        return {"name": "control_module_voltage", "value": volts, "unit": "V"}
    if pid == 0x0D:
        return {"name": "vehicle_speed", "value": a, "unit": "km/h"}
    if pid == 0x2F:
        return {"name": "fuel_tank_level", "value": round(a * 100 / 255, 1), "unit": "%"}
    if pid == 0x51:
        fuel_types = {1: "gasoline"}
        return {"name": "fuel_type", "value": fuel_types.get(a, f"unknown(0x{a:02x})")}
    return None


def decode_response(
    response_service: int, pid: Optional[int], value_bytes: List[int]
) -> Optional[Dict[str, Any]]:
    """Best-effort decode dispatching on response service: Mode 03 (0x43)
    responses carry DTCs rather than a PID'd value, so `decode_pid_value`
    (which expects a `pid`) doesn't apply to them."""
    if response_service == 0x43:
        return {"dtcs": decode_dtcs(value_bytes)}
    return decode_pid_value(pid, value_bytes)
