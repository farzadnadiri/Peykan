"""Simulated engine ECU speaking UDS (ISO 14229) over ISO-TP.

Listens on the engine's physical request ID (0x7E0) and answers on 0x7E8,
with can-isotp handling segmentation and flow control, so multi-frame
responses (VIN, DTC lists) behave like a real ECU's. It also answers the
functional (0x7DF) OBD-II Mode 09 PID 02 VIN request, which needs a
multi-frame reply the single-frame `OBDResponderThread` can't give.

DTC memory follows the fault presets: an active preset's codes are
`test_failed` + `confirmed`; after the fault clears they stay stored as
`confirmed` until a ClearDiagnosticInformation (0x14) -- the way a real
ECU keeps history until a technician clears it.
"""
import logging
import threading
from typing import Dict, Optional

import can
import isotp

from .. import __version__
from ..obd import OBD_BROADCAST_ID, simulate_response
from ..uds import ENGINE_REQUEST_ID, ENGINE_RESPONSE_ID, ISOTP_PARAMS, dtc_to_uds
from .faults import FaultState
from .state import DrivingState, VehicleState

logger = logging.getLogger(__name__)

SIM_VIN = "1MCAN00S0SM000042"  # synthetic, valid check digit
ASCII_DIDS: Dict[int, str] = {
    0xF187: "MCPCAN-ECM-01",
    0xF18C: "SIM0000042",
    0xF190: SIM_VIN,
    0xF195: __version__,
}
# F4xx DIDs mirror OBD Mode 01 PIDs (SAE J1979-2).
OBD_MIRROR_DIDS = {0xF405, 0xF40C, 0xF40D, 0xF442}

STATUS_TEST_FAILED = 0x01
STATUS_FAILED_THIS_CYCLE = 0x02
STATUS_CONFIRMED = 0x08
STATUS_AVAILABILITY_MASK = STATUS_TEST_FAILED | STATUS_FAILED_THIS_CYCLE | STATUS_CONFIRMED
ACTIVE_STATUS = STATUS_AVAILABILITY_MASK


def _negative(service: int, nrc: int) -> bytes:
    return bytes([0x7F, service, nrc])


class UdsEcu:
    """Protocol logic, independent of any bus: `handle(request) -> response`."""

    def __init__(
        self,
        vehicle_state: Optional[VehicleState] = None,
        fault_state: Optional[FaultState] = None,
    ):
        self.vehicle_state = vehicle_state
        self.fault_state = fault_state
        self.session = 0x01
        self._dtc_memory: Dict[str, int] = {}
        self._lock = threading.Lock()

    def sync_dtcs(self) -> None:
        active = set(self.fault_state.dtcs()) if self.fault_state else set()
        with self._lock:
            for code in active:
                self._dtc_memory[code] = ACTIVE_STATUS
            for code in list(self._dtc_memory):
                if code not in active:
                    self._dtc_memory[code] &= ~(STATUS_TEST_FAILED | STATUS_FAILED_THIS_CYCLE)

    def dtc_memory(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._dtc_memory)

    def handle(self, request: bytes) -> Optional[bytes]:
        if not request:
            return None
        self.sync_dtcs()
        service = request[0]
        handler = {
            0x09: self._obd_vehicle_info,
            0x10: self._session_control,
            0x11: self._ecu_reset,
            0x14: self._clear_dtcs,
            0x19: self._read_dtc_information,
            0x22: self._read_data_by_identifier,
            0x3E: self._tester_present,
        }.get(service)
        if handler is None:
            return _negative(service, 0x11)  # serviceNotSupported
        return handler(request)

    def _state(self) -> Optional[DrivingState]:
        if self.vehicle_state is None:
            return None
        state = self.vehicle_state.snapshot()
        return self.fault_state.apply(state) if self.fault_state else state

    def _obd_vehicle_info(self, request: bytes) -> Optional[bytes]:
        if len(request) < 2:
            return _negative(0x09, 0x13)
        if request[1] == 0x02:  # VIN: 1 data item, 17 ASCII characters
            return bytes([0x49, 0x02, 0x01]) + SIM_VIN.encode("ascii")
        return None  # other Mode 09 PIDs: left to the single-frame OBD responder

    def _session_control(self, request: bytes) -> bytes:
        if len(request) != 2:
            return _negative(0x10, 0x13)
        session = request[1] & 0x7F
        if session not in (0x01, 0x03):  # default, extended
            return _negative(0x10, 0x12)
        self.session = session
        # P2 = 50 ms, P2* = 5000 ms (in 10 ms units)
        return bytes([0x50, session, 0x00, 0x32, 0x01, 0xF4])

    def _ecu_reset(self, request: bytes) -> bytes:
        if len(request) != 2:
            return _negative(0x11, 0x13)
        if request[1] & 0x7F not in (0x01, 0x03):  # hard, soft
            return _negative(0x11, 0x12)
        self.session = 0x01
        return bytes([0x51, request[1] & 0x7F])

    def _clear_dtcs(self, request: bytes) -> bytes:
        if len(request) != 4:
            return _negative(0x14, 0x13)
        active = set(self.fault_state.dtcs()) if self.fault_state else set()
        with self._lock:
            # Faults still present are detected again straight away.
            self._dtc_memory = {code: ACTIVE_STATUS for code in active}
        return bytes([0x54])

    def _read_dtc_information(self, request: bytes) -> bytes:
        if len(request) < 2:
            return _negative(0x19, 0x13)
        sub = request[1] & 0x7F
        memory = self.dtc_memory()
        if sub in (0x01, 0x02):
            if len(request) != 3:
                return _negative(0x19, 0x13)
            mask = request[2]
            matching = {code: st for code, st in memory.items() if st & mask}
            if sub == 0x01:  # number of DTCs, ISO 14229-1 format
                count = len(matching)
                return bytes([0x59, 0x01, STATUS_AVAILABILITY_MASK, 0x01, count >> 8, count & 0xFF])
            return bytes([0x59, 0x02, STATUS_AVAILABILITY_MASK]) + self._dtc_records(matching)
        if sub == 0x0A:  # all supported DTCs
            return bytes([0x59, 0x0A, STATUS_AVAILABILITY_MASK]) + self._dtc_records(memory)
        return _negative(0x19, 0x12)

    @staticmethod
    def _dtc_records(dtcs: Dict[str, int]) -> bytes:
        out = bytearray()
        for code, status in sorted(dtcs.items()):
            out += dtc_to_uds(code).to_bytes(3, "big") + bytes([status])
        return bytes(out)

    def _read_data_by_identifier(self, request: bytes) -> bytes:
        if len(request) < 3 or (len(request) - 1) % 2:
            return _negative(0x22, 0x13)
        out = bytearray([0x62])
        state = self._state()
        for i in range(1, len(request), 2):
            did = (request[i] << 8) | request[i + 1]
            if did in ASCII_DIDS:
                value = ASCII_DIDS[did].encode("ascii")
            elif did in OBD_MIRROR_DIDS:
                payload = simulate_response(0x01, did & 0xFF, state=state)
                if payload is None:
                    return _negative(0x22, 0x31)
                value = bytes(payload[2:])
            else:
                return _negative(0x22, 0x31)  # requestOutOfRange
            out += did.to_bytes(2, "big") + value
        return bytes(out)

    def _tester_present(self, request: bytes) -> Optional[bytes]:
        if len(request) != 2:
            return _negative(0x3E, 0x13)
        if request[1] & 0x80:  # suppressPosRspMsgIndicationBit
            return None
        return bytes([0x7E, 0x00])


class UdsEcuThread(threading.Thread):
    """Runs a `UdsEcu` on the bus: physical requests via an ISO-TP stack,
    plus a raw listener for functional (0x7DF) single-frame requests."""

    def __init__(
        self,
        bus: can.BusABC,
        functional_bus: can.BusABC,
        ecu: UdsEcu,
        request_id: int = ENGINE_REQUEST_ID,
        response_id: int = ENGINE_RESPONSE_ID,
    ):
        super().__init__(daemon=True, name="UdsEcu")
        self.ecu = ecu
        self.functional_bus = functional_bus
        address = isotp.Address(
            isotp.AddressingMode.Normal_11bits, txid=response_id, rxid=request_id
        )
        self.stack = isotp.CanStack(bus, address=address, params=ISOTP_PARAMS)
        self._send_lock = threading.Lock()

    def _respond(self, request: bytes) -> None:
        try:
            response = self.ecu.handle(request)
        except Exception:
            logger.exception("UDS ECU error handling %s", request.hex(" "))
            response = _negative(request[0], 0x10) if request else None
        if response:
            with self._send_lock:
                self.stack.send(response)

    def _functional_loop(self) -> None:
        while True:
            msg = self.functional_bus.recv(timeout=0.2)
            if msg is None or msg.arbitration_id != OBD_BROADCAST_ID or not msg.data:
                continue
            length = msg.data[0]
            if not 1 <= length <= 7:  # single frames only, per ISO 15765-4
                continue
            request = bytes(msg.data[1 : 1 + length])
            if request[:1] == b"\x09":
                self._respond(request)

    def run(self) -> None:
        self.stack.start()
        threading.Thread(target=self._functional_loop, daemon=True, name="UdsEcuFunctional").start()
        while True:
            # Record faults as they happen, not only when a tester asks: a
            # fault that comes and goes between two reads must still end up
            # stored as a confirmed DTC.
            self.ecu.sync_dtcs()
            request = self.stack.recv(block=True, timeout=0.2)
            if request is not None:
                self._respond(bytes(request))
