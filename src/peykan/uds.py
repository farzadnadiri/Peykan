"""UDS (ISO 14229) over ISO-TP (ISO 15765-2), plus OBD-II Mode 09 VIN.

Classic CAN frames carry 8 bytes; anything longer -- a VIN, a DTC list, most
UDS responses -- is split by ISO-TP into first/consecutive frames with flow
control in between. can-isotp implements that transport and udsoncan the
UDS client on top; this module holds the shared protocol knowledge (IDs,
DID names, DTC conversion) and small client helpers used by both the MCP
tools and the CLI. The simulator side lives in `simulator/uds_ecu.py`.

Every client helper takes a `TransmitGuard` and sends through it, so ISO-TP
flow-control and consecutive frames are covered by the transmit policy too.
"""
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

import can
import isotp
import udsoncan
from udsoncan.client import Client
from udsoncan.connections import PythonIsoTpConnection
from udsoncan.exceptions import NegativeResponseException, TimeoutException

from .bus import make_bus, shutdown_bus
from .config import Settings
from .obd import OBD_BROADCAST_ID, decode_dtc, decode_pid_value, encode_dtc
from .safety import UDS_WRITE_SERVICES, TransmitGuard

# Engine ECU physical addressing (ISO 15765-4 11-bit OBD/UDS convention).
ENGINE_REQUEST_ID = 0x7E0
ENGINE_RESPONSE_ID = 0x7E8

ISOTP_PARAMS: Dict[str, Any] = {
    "tx_padding": 0x00,
    "tx_data_min_length": 8,
    "stmin": 0,
    "blocksize": 0,
    "rx_flowcontrol_timeout": 1000,
    "rx_consecutive_frame_timeout": 1000,
}

# Data identifiers the simulator implements (and that are common on real
# ECUs: the F18x/F19x block is standardised by ISO 14229, F4xx mirrors OBD
# Mode 01 PIDs per SAE J1979-2).
KNOWN_DIDS: Dict[int, str] = {
    0xF187: "spare_part_number",
    0xF18C: "ecu_serial_number",
    0xF190: "vin",
    0xF195: "software_version",
    0xF405: "engine_coolant_temp",
    0xF40C: "engine_rpm",
    0xF40D: "vehicle_speed",
    0xF442: "control_module_voltage",
}

NRC_NAMES: Dict[int, str] = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x78: "requestCorrectlyReceivedResponsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}

# ISO 14229 DTC status byte, bit 0 first.
DTC_STATUS_BITS = [
    "test_failed",
    "test_failed_this_operation_cycle",
    "pending",
    "confirmed",
    "test_not_completed_since_last_clear",
    "test_failed_since_last_clear",
    "test_not_completed_this_operation_cycle",
    "warning_indicator_requested",
]


def dtc_to_uds(code: str, failure_type: int = 0) -> int:
    """'P0300' -> 24-bit UDS DTC (2 SAE J2012 bytes + failure-type byte)."""
    a, b = encode_dtc(code)
    return (a << 16) | (b << 8) | (failure_type & 0xFF)


def uds_to_dtc(value: int) -> Dict[str, Any]:
    return {
        "code": decode_dtc((value >> 16) & 0xFF, (value >> 8) & 0xFF),
        "failure_type": value & 0xFF,
        "uds_id": f"0x{value:06X}",
    }


def describe_status(status: int) -> List[str]:
    return [name for bit, name in enumerate(DTC_STATUS_BITS) if status & (1 << bit)]


_VIN_VALUES = {
    **{str(i): i for i in range(10)},
    **dict(zip("ABCDEFGH", range(1, 9))),
    **dict(zip("JKLMN", range(1, 6))),
    "P": 7,
    "R": 9,
    **dict(zip("STUVWXYZ", range(2, 10))),
}
_VIN_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]


def vin_check_digit_valid(vin: str) -> Optional[bool]:
    """North American (49 CFR 565) check digit in position 9. None when the
    VIN isn't 17 valid characters; many non-NA VINs don't use the digit."""
    if len(vin) != 17 or any(c not in _VIN_VALUES for c in vin):
        return None
    remainder = sum(_VIN_VALUES[c] * w for c, w in zip(vin, _VIN_WEIGHTS)) % 11
    return vin[8] == ("X" if remainder == 10 else str(remainder))


def decode_did(did: int, data: bytes) -> Dict[str, Any]:
    name = KNOWN_DIDS.get(did)
    result: Dict[str, Any] = {"did": f"0x{did:04X}", "name": name, "raw": data.hex(" ")}
    if did in (0xF187, 0xF18C, 0xF190, 0xF195):
        result["value"] = data.decode("ascii", errors="replace").strip("\x00 ")
    elif 0xF400 <= did <= 0xF4FF:
        decoded = decode_pid_value(did & 0xFF, list(data))
        if decoded:
            result["value"], result["unit"] = decoded["value"], decoded.get("unit")
    elif data and all(32 <= b < 127 for b in data):
        result["value"] = data.decode("ascii")
    return result


class _RawCodec(udsoncan.DidCodec):
    """Return any DID's payload as raw bytes; `decode_did` interprets it."""

    def encode(self, *args: Any, **kwargs: Any) -> bytes:
        raise NotImplementedError("read-only codec")

    def decode(self, payload: bytes) -> bytes:
        return bytes(payload)

    def __len__(self) -> int:
        raise udsoncan.DidCodec.ReadAllRemainingData


def _isotp_stack(bus: Any, request_id: int, response_id: int) -> isotp.CanStack:
    address = isotp.Address(
        isotp.AddressingMode.Normal_11bits, txid=request_id, rxid=response_id
    )
    return isotp.CanStack(bus, address=address, params=ISOTP_PARAMS)


@contextmanager
def uds_client(
    settings: Settings,
    guard: TransmitGuard,
    purpose: str,
    write: bool,
    request_id: int = ENGINE_REQUEST_ID,
    response_id: int = ENGINE_RESPONSE_ID,
    timeout_s: float = 2.0,
) -> Iterator[Client]:
    # Fail fast with the policy's own message, before opening anything.
    guard.check(request_id, purpose, write=write)
    raw_bus = make_bus(settings.can_interface, settings.can_channel)
    try:
        bus = guard.wrap(raw_bus, purpose, write=write)
        conn = PythonIsoTpConnection(_isotp_stack(bus, request_id, response_id))
        config: Any = dict(udsoncan.configs.default_client_config)
        config["data_identifiers"] = {"default": _RawCodec}
        config["request_timeout"] = timeout_s
        config["p2_timeout"] = min(timeout_s, 1.0)
        with Client(conn, config=config) as client:
            yield client
    finally:
        shutdown_bus(raw_bus)


def uds_error(exc: Exception) -> str:
    if isinstance(exc, NegativeResponseException):
        code = exc.response.code or 0
        return f"negative response 0x{code:02X} ({NRC_NAMES.get(code, 'unknown')})"
    if isinstance(exc, TimeoutException):
        return "no response from the ECU within the timeout"
    return str(exc)


def read_dids(client: Client, dids: List[int]) -> List[Dict[str, Any]]:
    results = []
    for did in dids:
        response = client.read_data_by_identifier(did)
        assert response is not None
        results.append(decode_did(did, response.service_data.values[did]))
    return results


def read_dtcs(client: Client, status_mask: int = 0xFF) -> List[Dict[str, Any]]:
    response = client.get_dtc_by_status_mask(status_mask)
    assert response is not None
    dtcs = []
    for dtc in response.service_data.dtcs:
        status = dtc.status.get_byte_as_int()
        dtcs.append(
            {
                **uds_to_dtc(dtc.id),
                "status": f"0x{status:02X}",
                "status_bits": describe_status(status),
            }
        )
    return dtcs


def is_write_service(service_id: int) -> bool:
    return service_id in UDS_WRITE_SERVICES


def read_vin_obd(
    settings: Settings, guard: TransmitGuard, timeout_s: float = 2.0
) -> Dict[str, Any]:
    """OBD-II Mode 09 PID 02: functional request on 0x7DF, multi-frame
    ISO-TP answer from the engine ECU (flow control sent on 0x7E0)."""
    purpose = "read_vin"
    guard.check(OBD_BROADCAST_ID, purpose)
    raw_bus = make_bus(settings.can_interface, settings.can_channel)
    stack = None
    try:
        bus = guard.wrap(raw_bus, purpose)
        stack = _isotp_stack(bus, ENGINE_REQUEST_ID, ENGINE_RESPONSE_ID)
        stack.start()
        bus.send(
            can.Message(
                arbitration_id=OBD_BROADCAST_ID,
                data=[0x02, 0x09, 0x02, 0, 0, 0, 0, 0],
                is_extended_id=False,
            )
        )
        received = stack.recv(block=True, timeout=timeout_s)
        if received is None:
            raise TimeoutError("no Mode 09 VIN response within the timeout")
        payload = bytes(received)
        if payload[:2] != b"\x49\x02":
            raise ValueError(f"unexpected response {payload.hex(' ')}")
        # Byte 2 is the number of data items (1); the VIN follows.
        vin = payload[3:].decode("ascii", errors="replace").strip("\x00 ")
        return {"vin": vin, "check_digit_valid": vin_check_digit_valid(vin)}
    finally:
        if stack is not None:
            stack.stop()
        shutdown_bus(raw_bus)
