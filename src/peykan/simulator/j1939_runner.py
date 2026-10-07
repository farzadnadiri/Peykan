"""SAE J1939 side of the ECU simulator.

Runs alongside the light-vehicle `SimThread`/`OBDResponderThread` stack (see
`runner.py`), sharing the same virtual bus but using 29-bit extended IDs so
the two protocols never collide. Three pieces:

* `J1939Broadcaster` — periodically encodes EEC1/EEC2/ET1/CCVS1/LFE1/DD1/VEP1
  from the shared `VehicleState` (with the active fault's state effect), so
  J1939 engine speed / road speed / coolant temperature / battery voltage
  track the same driving dynamics the 11-bit signals do.
* `J1939RequestResponder` — answers Request PGN (0xEA00) frames by
  re-broadcasting the requested PGN once.
* `J1939Dm1Broadcaster` — emits DM1 (active DTCs) at 1 Hz, reflecting the
  active fault-injection preset via `J1939_FAULT_DTCS`. A DM1 with more than
  one DTC exceeds 8 bytes and goes out via the BAM transport protocol.

Like every listener thread here, each gets its **own** `make_bus(...)`
instance — see `runner.py::run_simulator`'s comment on frame theft.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional

import can

from .. import j1939
from .faults import FaultState
from .state import DrivingState, VehicleState

logger = logging.getLogger(__name__)

# preset name -> DTCs a DM1 should carry while that scenario is active.
# Broadcast from the engine address for simplicity even though e.g. the ABS
# wheel-speed fault would really originate at the brake controller.
J1939_FAULT_DTCS: Dict[str, List[j1939.J1939Dtc]] = {
    "overheat": [j1939.J1939Dtc(spn=110, fmi=0)],  # coolant temp high, most severe
    "abs_fault": [j1939.J1939Dtc(spn=84, fmi=5)],  # wheel speed sensor open circuit
    "low_fuel": [j1939.J1939Dtc(spn=96, fmi=18)],  # fuel level low, moderately severe
    "misfire": [
        j1939.J1939Dtc(spn=1322, fmi=31),  # misfire, multiple cylinders: condition exists
        j1939.J1939Dtc(spn=651, fmi=7),  # cylinder 1 injector: mechanical not responding
    ],
    "battery_low": [j1939.J1939Dtc(spn=168, fmi=18)],  # battery potential low
}
# Airbag and body faults aren't reported over J1939 (their DTCs show up via
# OBD-II/UDS only); listed so a newly added preset can't be silently missed.
PRESETS_WITHOUT_J1939_DTCS = {"crash", "door_ajar"}


def _fuel_rate_lph(throttle_pct: float) -> float:
    """Rough idle-to-load fuel burn, L/h."""
    return round(1.5 + throttle_pct * 0.4, 2)


def signals_for_pgn(pgn: int, state) -> Dict[str, float]:
    """Map a `DrivingState` snapshot onto one PGN's SPNs."""
    if pgn == j1939.PGN_EEC1:
        return {
            "ENGINE_SPEED": state.rpm,
            "ACTUAL_ENGINE_PERCENT_TORQUE": round(state.throttle_pct * 0.8, 1),
        }
    if pgn == j1939.PGN_EEC2:
        return {
            "ACCELERATOR_PEDAL_POSITION_1": round(state.throttle_pct, 1),
            "ENGINE_PERCENT_LOAD_AT_CURRENT_SPEED": round(state.throttle_pct * 0.9, 1),
        }
    if pgn == j1939.PGN_ET1:
        return {
            "ENGINE_COOLANT_TEMPERATURE": round(state.engine_temp_c, 1),
            "ENGINE_FUEL_TEMPERATURE_1": round(state.engine_temp_c - 20.0, 1),
        }
    if pgn == j1939.PGN_CCVS1:
        return {"WHEEL_BASED_VEHICLE_SPEED": round(state.speed_kph, 2)}
    if pgn == j1939.PGN_LFE1:
        return {
            "ENGINE_FUEL_RATE": _fuel_rate_lph(state.throttle_pct),
            "ENGINE_THROTTLE_VALVE_1_POSITION": round(state.throttle_pct, 1),
        }
    if pgn == j1939.PGN_DD1:
        return {"FUEL_LEVEL_1": round(state.fuel_pct, 1)}
    if pgn == j1939.PGN_VEP1:
        return {"BATTERY_POTENTIAL_POWER_INPUT_1": round(state.battery_v, 2)}
    return {}


# (pgn, source address, priority, period seconds)
BROADCAST_SCHEDULE = [
    (j1939.PGN_EEC1, j1939.SA_ENGINE, 3, 0.05),
    (j1939.PGN_EEC2, j1939.SA_ENGINE, 3, 0.05),
    (j1939.PGN_ET1, j1939.SA_ENGINE, 6, 1.0),
    (j1939.PGN_CCVS1, j1939.SA_BRAKES, 6, 0.1),
    (j1939.PGN_LFE1, j1939.SA_ENGINE, 6, 0.5),
    (j1939.PGN_DD1, j1939.SA_INSTRUMENT_CLUSTER, 6, 1.0),
    (j1939.PGN_VEP1, j1939.SA_ENGINE, 6, 1.0),
]

BROADCAST_PGNS = {pgn for pgn, *_ in BROADCAST_SCHEDULE}


def _encode_frame(pgn: int, source_address: int, priority: int, state) -> can.Message:
    data = j1939.encode_pgn(pgn, signals_for_pgn(pgn, state))
    return can.Message(
        arbitration_id=j1939.build_can_id(pgn, source_address, priority=priority),
        data=data,
        is_extended_id=True,
    )


def _faulted_snapshot(
    vehicle_state: VehicleState, fault_state: Optional[FaultState]
) -> DrivingState:
    state = vehicle_state.snapshot()
    return fault_state.apply(state) if fault_state is not None else state


class J1939Broadcaster(threading.Thread):
    def __init__(
        self,
        bus: can.BusABC,
        vehicle_state: VehicleState,
        fault_state: Optional[FaultState] = None,
    ):
        super().__init__(daemon=True)
        self.bus = bus
        self.vehicle_state = vehicle_state
        self.fault_state = fault_state
        self._next = {pgn: 0.0 for pgn, *_ in BROADCAST_SCHEDULE}

    def run(self) -> None:
        while True:
            now = time.time()
            state = _faulted_snapshot(self.vehicle_state, self.fault_state)
            for pgn, sa, priority, period in BROADCAST_SCHEDULE:
                if now < self._next[pgn]:
                    continue
                self._next[pgn] = now + period
                try:
                    self.bus.send(_encode_frame(pgn, sa, priority, state))
                except Exception:
                    logger.exception("J1939 broadcast error for PGN 0x%04X", pgn)
            time.sleep(0.02)


class J1939RequestResponder(threading.Thread):
    """Answers Request PGN (0xEA00) frames by re-sending the requested PGN once."""

    def __init__(
        self,
        bus: can.BusABC,
        vehicle_state: VehicleState,
        fault_state: Optional[FaultState] = None,
    ):
        super().__init__(daemon=True)
        self.bus = bus
        self.vehicle_state = vehicle_state
        self.fault_state = fault_state

    def run(self) -> None:
        while True:
            msg = self.bus.recv(timeout=0.1)
            if msg is None or not msg.is_extended_id:
                continue
            parsed = j1939.parse_can_id(msg.arbitration_id)
            if parsed.pgn != j1939.PGN_REQUEST:
                continue
            requested = j1939.parse_request_pgn(bytes(msg.data))
            if requested is None:
                continue
            try:
                self._answer(requested)
            except Exception:
                logger.exception("J1939 request responder error (PGN 0x%04X)", requested)

    def _answer(self, requested_pgn: int) -> None:
        if requested_pgn == j1939.PGN_DM1:
            send_dm1(self.bus, self.fault_state)
            return
        for pgn, sa, priority, _period in BROADCAST_SCHEDULE:
            if pgn == requested_pgn:
                state = _faulted_snapshot(self.vehicle_state, self.fault_state)
                self.bus.send(_encode_frame(pgn, sa, priority, state))
                return


def active_dm1_dtcs(fault_state: Optional[FaultState]) -> List[j1939.J1939Dtc]:
    preset = fault_state.active_preset_name() if fault_state else None
    return list(J1939_FAULT_DTCS.get(preset, [])) if preset else []


def dm1_messages(fault_state: Optional[FaultState]) -> List[can.Message]:
    """The frame(s) carrying the current DM1: one frame, or a BAM sequence
    once two or more DTCs push the payload past 8 bytes."""
    dtcs = active_dm1_dtcs(fault_state)
    payload = j1939.build_dm1(dtcs, mil_on=bool(dtcs))
    return [
        can.Message(arbitration_id=can_id, data=data, is_extended_id=True)
        for can_id, data in j1939.frames_for_pgn(j1939.PGN_DM1, payload, j1939.SA_ENGINE)
    ]


def send_dm1(bus: can.BusABC, fault_state: Optional[FaultState]) -> None:
    messages = dm1_messages(fault_state)
    for i, msg in enumerate(messages):
        if i > 1:  # gap between BAM data packets, none before the first one
            time.sleep(j1939.BAM_PACKET_GAP_S)
        bus.send(msg)


class J1939Dm1Broadcaster(threading.Thread):
    """Broadcasts DM1 (active DTCs) at 1 Hz, per SAE J1939-73."""

    def __init__(self, bus: can.BusABC, fault_state: Optional[FaultState] = None):
        super().__init__(daemon=True)
        self.bus = bus
        self.fault_state = fault_state

    def run(self) -> None:
        while True:
            try:
                send_dm1(self.bus, self.fault_state)
            except Exception:
                logger.exception("J1939 DM1 broadcast error")
            time.sleep(1.0)


def start_j1939_threads(
    make_bus_fn,
    can_interface: str,
    can_channel: str,
    vehicle_state: VehicleState,
    fault_state: Optional[FaultState] = None,
) -> List[threading.Thread]:
    """Spin up the three J1939 simulator threads, each on its own bus."""
    threads: List[threading.Thread] = [
        J1939Broadcaster(make_bus_fn(can_interface, can_channel), vehicle_state, fault_state),
        J1939RequestResponder(
            make_bus_fn(can_interface, can_channel), vehicle_state, fault_state
        ),
        J1939Dm1Broadcaster(make_bus_fn(can_interface, can_channel), fault_state),
    ]
    for t in threads:
        t.start()
    return threads
