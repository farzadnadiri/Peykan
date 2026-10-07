"""Transmit safety: one policy check and audit trail for every outgoing frame.

On the virtual simulator bus anything goes. On real hardware the tools are
read-only until `PEYKAN_ALLOW_TRANSMIT=true`, and services that change ECU
state (clearing DTCs, resets, writes, fault injection, log replay) need
`PEYKAN_ALLOW_WRITE_SERVICES=true` on top. `PEYKAN_TRANSMIT_ALLOWLIST`
narrows which arbitration IDs may be used at all.

Enforcement wraps the bus itself (`TransmitGuard.wrap`) rather than relying
on each tool to call a check before `bus.send`: libraries such as can-isotp
send frames on their own (flow control, consecutive frames), and those must
be covered too.
"""
import json
import logging
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

import can

from .config import Settings
from .parsing import parse_int

logger = logging.getLogger(__name__)

# UDS (ISO 14229) services that change ECU state rather than read it.
UDS_WRITE_SERVICES = {
    0x11,  # ECUReset
    0x14,  # ClearDiagnosticInformation
    0x27,  # SecurityAccess
    0x28,  # CommunicationControl
    0x2E,  # WriteDataByIdentifier
    0x2F,  # InputOutputControlByIdentifier
    0x31,  # RoutineControl
    0x34, 0x35, 0x36, 0x37,  # Request download/upload, transfer data, exit
    0x3D,  # WriteMemoryByAddress
    0x85,  # ControlDTCSetting
}
# OBD-II (SAE J1979) modes that change state.
OBD_WRITE_SERVICES = {0x04, 0x08}  # clear DTCs, control on-board system

TRANSMIT_LOG_SIZE = 200


class TransmitBlocked(Exception):
    """Raised instead of sending when the transmit policy refuses a frame."""


class TransmitGuard:
    def __init__(self, settings: Settings):
        self.interface = settings.can_interface
        self.transmit_allowed = settings.transmit_allowed
        self.write_allowed = settings.write_services_allowed
        self.allowlist: Optional[Set[int]] = (
            {parse_int(i) for i in settings.transmit_allowlist}
            if settings.transmit_allowlist
            else None
        )
        self.log_path = settings.transmit_log_path
        self._records: Deque[Dict[str, Any]] = deque(maxlen=TRANSMIT_LOG_SIZE)
        self._lock = threading.Lock()

    def policy(self) -> Dict[str, Any]:
        return {
            "interface": self.interface,
            "transmit_allowed": self.transmit_allowed,
            "write_services_allowed": self.write_allowed,
            "allowlist": sorted(hex(i) for i in self.allowlist) if self.allowlist else None,
        }

    def check(self, arbitration_id: int, purpose: str, write: bool = False) -> None:
        """Raise TransmitBlocked if policy forbids this frame."""
        reason = self._refusal(arbitration_id, write)
        if reason:
            raise TransmitBlocked(f"{purpose}: {reason}")

    def _refusal(self, arbitration_id: int, write: bool) -> Optional[str]:
        if not self.transmit_allowed:
            return (
                f"transmitting is disabled on interface {self.interface!r} "
                "(read-only by default on real hardware; set PEYKAN_ALLOW_TRANSMIT=true)"
            )
        if write and not self.write_allowed:
            return (
                "this changes ECU state and write services are disabled "
                "(set PEYKAN_ALLOW_WRITE_SERVICES=true)"
            )
        if self.allowlist is not None and arbitration_id not in self.allowlist:
            return f"ID {hex(arbitration_id)} is not in PEYKAN_TRANSMIT_ALLOWLIST"
        return None

    def send(self, bus: can.BusABC, msg: can.Message, purpose: str, write: bool = False) -> None:
        reason = self._refusal(msg.arbitration_id, write)
        self._record(msg, purpose, write, reason)
        if reason:
            raise TransmitBlocked(f"{purpose}: {reason}")
        bus.send(msg)

    def wrap(self, bus: can.BusABC, purpose: str, write: bool = False) -> "GuardedBus":
        return GuardedBus(bus, self, purpose, write)

    def _record(
        self, msg: can.Message, purpose: str, write: bool, refusal: Optional[str]
    ) -> None:
        entry = {
            "timestamp": time.time(),
            "arbitration_id": hex(msg.arbitration_id),
            "is_extended_id": bool(msg.is_extended_id),
            "data": bytes(msg.data).hex(" "),
            "purpose": purpose,
            "write": write,
            "sent": refusal is None,
            "blocked_reason": refusal,
        }
        with self._lock:
            self._records.append(entry)
        if refusal:
            logger.warning("TX blocked %s [%s]: %s", entry["arbitration_id"], purpose, refusal)
        elif self.interface != "virtual":
            # On hardware every frame is worth a visible line; on the
            # simulator bus that would just be noise.
            logger.info("TX %s %s [%s]", entry["arbitration_id"], entry["data"], purpose)
        if self.log_path:
            try:
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry) + "\n")
            except OSError:
                logger.exception("Could not append to transmit log %s", self.log_path)

    def records(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._records)[-limit:]


class GuardedBus(can.BusABC):
    """A bus whose `send` goes through a TransmitGuard and whose receive side
    is the wrapped bus. A real BusABC subclass (not a duck-typed proxy)
    because can-isotp's stacks refuse anything else."""

    def __init__(self, bus: can.BusABC, guard: TransmitGuard, purpose: str, write: bool):
        self._bus = bus
        self._guard = guard
        self._purpose = purpose
        self._write = write
        super().__init__(channel=getattr(bus, "channel_info", "guarded"))
        # Owns no resources: the wrapped bus is shut down by whoever opened
        # it. Marking this view shut down keeps python-can's "not properly
        # shut down" warning for it quiet.
        self._is_shutdown = True

    def send(self, msg: can.Message, timeout: Optional[float] = None) -> None:
        self._guard.send(self._bus, msg, self._purpose, self._write)

    def _recv_internal(self, timeout: Optional[float]) -> Tuple[Optional[can.Message], bool]:
        return self._bus.recv(timeout), False

    def shutdown(self) -> None:
        pass
