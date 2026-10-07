import time
from typing import Any, Dict, List

import can

from .config import get_settings
from .models import Frame


def make_bus(interface: str, channel: str) -> can.BusABC:
    kwargs: Dict[str, Any] = {}
    bitrate = get_settings().can_bitrate
    if bitrate:
        kwargs["bitrate"] = bitrate
    # ThreadSafeBus provides thread-safe send; for reading, regular interface is fine
    try:
        return can.ThreadSafeBus(interface=interface, channel=channel, **kwargs)
    except Exception:
        # Fallback to standard Bus if ThreadSafeBus not available or fails
        return can.interface.Bus(interface=interface, channel=channel, **kwargs)


def read_frames(bus: can.BusABC, duration_s: float = 1.0) -> List[Frame]:
    end = time.time() + duration_s
    frames: List[Frame] = []
    while time.time() < end:
        msg = bus.recv(timeout=0.1)
        if msg:
            frames.append(
                Frame(
                    timestamp=msg.timestamp,
                    arbitration_id=msg.arbitration_id,
                    data=bytes(msg.data),
                )
            )
    return frames


def shutdown_bus(bus: can.BusABC) -> None:
    """Attempt to cleanly shutdown a python-can bus; swallow errors for mock buses."""
    shutdown_fn = getattr(bus, "shutdown", None)
    if callable(shutdown_fn):
        try:
            shutdown_fn()
        except Exception:
            pass
