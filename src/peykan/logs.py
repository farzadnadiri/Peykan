"""Recorded CAN logs: load, summarise, extract signals, replay, record.

Most people who want an AI to read CAN data have recordings rather than a
live vehicle. python-can reads the common formats (Vector .asc/.blf, PEAK
.trc, candump .log, .csv, .mf4 with asammdf installed); this module turns a
recording into what an LLM can reason about -- which IDs are on the bus and
how regularly, decoded signal ranges, trouble codes, timing gaps -- and can
replay it onto a bus so every live tool (and the dashboard) works on it.

MCP clients may only read files under `PEYKAN_LOG_DIR`, or the bundled
`sample` log; the CLI, run by the user directly, reads any path.
"""
import logging
import os
import statistics
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import can
import cantools

from . import j1939
from .dbc import decode_frame
from .obd import OBD_RESPONSE_BASE_ID, decode_response, parse_response

logger = logging.getLogger(__name__)

SAMPLE_LOG = Path(__file__).parent / "data" / "sample_drive.asc"
MAX_LOG_FRAMES = 2_000_000
SUPPORTED_SUFFIXES = (".asc", ".blf", ".csv", ".db", ".log", ".mf4", ".trc")


class LogAccessError(ValueError):
    pass


def resolve_log_path(path: str, log_dir: str) -> Path:
    """Map a client-supplied path to a file under `log_dir` (or the bundled
    sample), refusing anything that escapes the directory."""
    base = Path(log_dir).resolve()
    if path.strip() in ("sample", SAMPLE_LOG.name) and not (base / path).exists():
        return SAMPLE_LOG
    candidate = (base / path).resolve()
    if candidate != base and base not in candidate.parents:
        raise LogAccessError(
            f"{path!r} is outside the log directory {str(base)!r} (PEYKAN_LOG_DIR)"
        )
    if not candidate.is_file():
        raise LogAccessError(f"{path!r} not found in {str(base)!r}")
    if candidate.suffix.lower() not in SUPPORTED_SUFFIXES:
        supported = ", ".join(SUPPORTED_SUFFIXES)
        raise LogAccessError(
            f"unsupported log format {candidate.suffix!r}; use one of {supported}"
        )
    return candidate


MAX_LISTED_LOGS = 200
LIST_DEPTH = 2  # log_dir itself plus one level of subdirectories


def list_logs(log_dir: str) -> List[Dict[str, Any]]:
    """Log files under `log_dir`, shallowly: the default log_dir is the
    server's working directory, which may well be a home directory that a
    full recursive walk would take minutes to crawl."""
    base = Path(log_dir).resolve()
    files: List[Dict[str, Any]] = []
    for root, dirs, names in os.walk(base):
        depth = len(Path(root).relative_to(base).parts)
        descend = depth + 1 < LIST_DEPTH
        dirs[:] = sorted(d for d in dirs if not d.startswith(".")) if descend else []
        for name in sorted(names):
            path = Path(root) / name
            if path.suffix.lower() in SUPPORTED_SUFFIXES:
                size = path.stat().st_size
                files.append({"path": str(path.relative_to(base)), "size_bytes": size})
                if len(files) >= MAX_LISTED_LOGS:
                    break
        if len(files) >= MAX_LISTED_LOGS:
            break
    files.append({"path": "sample", "size_bytes": SAMPLE_LOG.stat().st_size, "bundled": True})
    return files


@dataclass(frozen=True)
class LoadedLog:
    path: str
    frames: Tuple[Dict[str, Any], ...]
    error_frames: int
    truncated: bool

    @property
    def start(self) -> float:
        return self.frames[0]["timestamp"] if self.frames else 0.0

    @property
    def duration_s(self) -> float:
        return self.frames[-1]["timestamp"] - self.start if self.frames else 0.0


def load_log(path: Path) -> LoadedLog:
    stat = path.stat()
    return _load_log_cached(str(path), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=4)
def _load_log_cached(path: str, _mtime_ns: int, _size: int) -> LoadedLog:
    frames: List[Dict[str, Any]] = []
    error_frames = 0
    truncated = False
    with can.LogReader(path) as reader:
        for msg in reader:
            if msg.is_error_frame:
                error_frames += 1
                continue
            if msg.is_remote_frame:
                continue
            if len(frames) >= MAX_LOG_FRAMES:
                truncated = True
                break
            frames.append(
                {
                    "timestamp": float(msg.timestamp),
                    "arbitration_id": msg.arbitration_id,
                    "is_extended_id": bool(msg.is_extended_id),
                    "data": bytes(msg.data),
                }
            )
    frames.sort(key=lambda f: f["timestamp"])
    return LoadedLog(path, tuple(frames), error_frames, truncated)


def _decode(
    db: cantools.database.Database, frame: Dict[str, Any]
) -> Tuple[Optional[str], Dict[str, Any]]:
    """(message name, signals) for a frame, via the DBC or the J1939 catalog."""
    if frame["is_extended_id"]:
        try:
            pgn = j1939.parse_can_id(frame["arbitration_id"]).pgn
        except Exception:
            return None, {}
        definition = j1939.PGN_CATALOG.get(pgn)
        if definition is None or not definition.spns:
            return None, {}
        return f"J1939:{definition.acronym}", j1939.decode_pgn(pgn, frame["data"])
    try:
        name = db.get_message_by_frame_id(frame["arbitration_id"]).name
        return name, decode_frame(db, frame["arbitration_id"], frame["data"])
    except Exception:
        return None, {}


def _id_label(db: cantools.database.Database, arbitration_id: int, extended: bool) -> Optional[str]:
    if extended:
        try:
            pgn = j1939.parse_can_id(arbitration_id).pgn
        except Exception:
            return None
        definition = j1939.PGN_CATALOG.get(pgn)
        return f"J1939 {definition.acronym}" if definition else f"J1939 PGN 0x{pgn:04X}"
    try:
        return str(db.get_message_by_frame_id(arbitration_id).name)
    except Exception:
        pass
    if arbitration_id == 0x7DF:
        return "OBD-II functional request"
    if 0x7E0 <= arbitration_id <= 0x7E7:
        return "OBD/UDS physical request"
    if 0x7E8 <= arbitration_id <= 0x7EF:
        return "OBD/UDS response"
    return None


# Event-driven traffic: sent on demand rather than on a schedule, so a long
# silence there isn't a dropout worth reporting.
_EVENT_PGNS = {j1939.PGN_TP_CM, j1939.PGN_TP_DT, j1939.PGN_REQUEST}


def _is_event_driven(arbitration_id: int, extended: bool, label: Optional[str]) -> bool:
    if extended:
        try:
            return j1939.parse_can_id(arbitration_id).pgn in _EVENT_PGNS
        except Exception:
            return False
    if 0x7DF <= arbitration_id <= 0x7F1:  # OBD/UDS requests+responses, fault control
        return True
    return bool(label and label.startswith("DIAGNOSTIC_"))


def _units(db: cantools.database.Database) -> Dict[str, str]:
    units = {sig.name: sig.unit or "" for msg in db.messages for sig in msg.signals}
    for definition in j1939.PGN_CATALOG.values():
        for spn in definition.spns:
            units.setdefault(spn.name, spn.unit)
    return units


def analyze_log(log: LoadedLog, db: cantools.database.Database) -> Dict[str, Any]:
    t0 = log.start
    by_id: Dict[Tuple[int, bool], List[float]] = defaultdict(list)
    # Keyed by (signal, message): the same name can come from two sources
    # (e.g. ENGINE_SPEED in the DBC's ENGINE_STATUS and in J1939 EEC1).
    numeric: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    categorical: Dict[Tuple[str, str], Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    obd_dtcs: Dict[str, float] = {}
    j1939_dtcs: Dict[Tuple[int, int, int], Dict[str, Any]] = {}
    reassembler = j1939.TransportReassembler()

    for frame in log.frames:
        t = frame["timestamp"] - t0
        by_id[(frame["arbitration_id"], frame["is_extended_id"])].append(frame["timestamp"])
        name, signals = _decode(db, frame)
        for sig, value in signals.items():
            key = (sig, name or "")
            if hasattr(value, "name") and hasattr(value, "value"):  # choice signal
                categorical[key][str(value)] += 1
            elif isinstance(value, (int, float)) and sig != "dtcs":
                numeric[key].append(float(value))
        if frame["is_extended_id"]:
            dm1 = None
            pgn = j1939.parse_can_id(frame["arbitration_id"]).pgn
            if pgn == j1939.PGN_DM1:
                dm1 = (j1939.parse_can_id(frame["arbitration_id"]).source_address, frame["data"])
            else:
                done = reassembler.feed(frame["arbitration_id"], frame["data"])
                if done and done[0] == j1939.PGN_DM1:
                    dm1 = (done[1], done[2])
            if dm1:
                for dtc in j1939.parse_dm1(dm1[1])[1]:
                    dtc_key = (dm1[0], dtc.spn, dtc.fmi)
                    first = {**dtc.as_dict(), "source_address": dm1[0], "first_seen_s": round(t, 3)}
                    j1939_dtcs.setdefault(dtc_key, first)["last_seen_s"] = round(t, 3)
        elif OBD_RESPONSE_BASE_ID <= frame["arbitration_id"] <= OBD_RESPONSE_BASE_ID + 7:
            service, pid, value = parse_response(frame["data"])
            if service == 0x43:
                for code in (decode_response(service, pid, value) or {}).get("dtcs", []):
                    obd_dtcs.setdefault(code, round(t, 3))

    ids = []
    gaps = []
    for (arb_id, extended), stamps in sorted(by_id.items(), key=lambda kv: -len(kv[1])):
        label = _id_label(db, arb_id, extended)
        entry: Dict[str, Any] = {
            "arbitration_id": hex(arb_id),
            "extended": extended,
            "name": label,
            "count": len(stamps),
        }
        if len(stamps) > 1:
            intervals = [b - a for a, b in zip(stamps, stamps[1:])]
            period = statistics.median(intervals)
            entry["rate_hz"] = round(1 / period, 2) if period > 0 else None
            entry["median_period_ms"] = round(period * 1000, 1)
            worst = max(intervals)
            periodic = not _is_event_driven(arb_id, extended, label)
            if periodic and len(stamps) >= 10 and period > 0 and worst > max(5 * period, 0.5):
                at = stamps[intervals.index(worst)] - t0
                gaps.append(
                    f"{hex(arb_id)} {label or ''}: {worst:.2f} s gap at t={at:.1f} s "
                    f"(normally every {period * 1000:.0f} ms)".replace("  ", " ")
                )
        ids.append(entry)

    units = _units(db)
    signal_rows: List[Dict[str, Any]] = []
    for key in sorted(set(numeric) | set(categorical)):
        sig, message = key
        item: Dict[str, Any] = {"name": sig, "message": message, "unit": units.get(sig, "")}
        if key in numeric:
            values = numeric[key]
            item.update(
                min=round(min(values), 3),
                max=round(max(values), 3),
                mean=round(statistics.fmean(values), 3),
                first=round(values[0], 3),
                last=round(values[-1], 3),
                samples=len(values),
            )
        else:
            item["values"] = dict(categorical[key])
        signal_rows.append(item)

    return {
        "file": Path(log.path).name,
        "frame_count": len(log.frames),
        "error_frames": log.error_frames,
        "truncated": log.truncated,
        "duration_s": round(log.duration_s, 3),
        "distinct_ids": len(ids),
        "ids": ids[:60],
        "signals": signal_rows,
        "obd_dtcs": [{"code": c, "first_seen_s": t} for c, t in sorted(obd_dtcs.items())],
        "j1939_dtcs": list(j1939_dtcs.values()),
        "timing_gaps": gaps[:20],
    }


def signal_series(
    log: LoadedLog,
    db: cantools.database.Database,
    signal_name: str,
    max_points: int = 100,
    message: Optional[str] = None,
) -> Dict[str, Any]:
    """One signal over the log. When the name occurs in several messages
    (e.g. DBC ENGINE_STATUS and J1939:EEC1), `message` picks one; by default
    the most frequent source is used and the others are listed."""
    t0 = log.start
    by_source: Dict[str, List[Tuple[float, Any]]] = defaultdict(list)
    for frame in log.frames:
        name, signals = _decode(db, frame)
        if signal_name in signals:
            value = signals[signal_name]
            if hasattr(value, "name") and hasattr(value, "value"):
                value = str(value)
            by_source[name or ""].append((round(frame["timestamp"] - t0, 3), value))
    if not by_source:
        raise LogAccessError(f"signal {signal_name!r} does not occur in {Path(log.path).name}")
    if message is None:
        message = max(by_source, key=lambda m: len(by_source[m]))
    elif message not in by_source:
        raise LogAccessError(
            f"signal {signal_name!r} isn't in message {message!r}; it occurs in "
            f"{', '.join(sorted(by_source))}"
        )
    points = by_source[message]
    numbers = [v for _, v in points if isinstance(v, (int, float))]
    step = max(1, len(points) // max(1, max_points))
    sampled = points[::step]
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    result: Dict[str, Any] = {
        "signal": signal_name,
        "message": message,
        "unit": _units(db).get(signal_name, ""),
        "samples_total": len(points),
        "other_sources": sorted(m for m in by_source if m != message),
        "points": [{"t_s": t, "value": v} for t, v in sampled],
    }
    if numbers:
        result.update(
            min=round(min(numbers), 3), max=round(max(numbers), 3),
            mean=round(statistics.fmean(numbers), 3),
        )
    return result


class LogReplayer(threading.Thread):
    """Plays a log's frames onto a bus with their original spacing (scaled
    by `speed`), optionally looping. Frames get fresh timestamps from the
    bus, so live tools see them as current traffic."""

    def __init__(self, log: LoadedLog, bus: can.BusABC, speed: float = 1.0, loop: bool = False):
        super().__init__(daemon=True, name="LogReplayer")
        if speed <= 0:
            raise ValueError("speed must be positive")
        self.log = log
        self.bus = bus
        self.speed = speed
        self.loop = loop
        self.sent = 0
        self.loops = 0
        self.error: Optional[str] = None
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def status(self) -> Dict[str, Any]:
        return {
            "file": Path(self.log.path).name,
            "running": self.is_alive(),
            "frames_sent": self.sent,
            "frames_per_pass": len(self.log.frames),
            "completed_loops": self.loops,
            "speed": self.speed,
            "loop": self.loop,
            "error": self.error,
        }

    def run(self) -> None:
        try:
            while not self._stop_event.is_set():
                self._play_once()
                self.loops += 1
                if not self.loop:
                    break
        except Exception as e:  # e.g. TransmitBlocked
            self.error = str(e)
            logger.warning("Log replay stopped: %s", e)

    def _play_once(self) -> None:
        start_wall = time.monotonic()
        t0 = self.log.start
        for frame in self.log.frames:
            due = (frame["timestamp"] - t0) / self.speed
            delay = due - (time.monotonic() - start_wall)
            if delay > 0 and self._stop_event.wait(delay):
                return
            self.bus.send(
                can.Message(
                    arbitration_id=frame["arbitration_id"],
                    is_extended_id=frame["is_extended_id"],
                    data=frame["data"],
                )
            )
            self.sent += 1
            if self._stop_event.is_set():
                return


def record_bus(bus: can.BusABC, path: str, seconds: float) -> int:
    """Write everything seen on `bus` for `seconds` to `path` (format from
    the extension). Returns the number of frames written."""
    count = 0
    end = time.time() + seconds
    with can.Logger(path) as writer:
        while time.time() < end:
            msg = bus.recv(timeout=0.1)
            if msg is not None:
                writer(msg)
                count += 1
    return count
