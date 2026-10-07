# Changelog

All notable changes to this project are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.2.1] - 2026-10-07

### Fixed
- The Docker image ran `peykan server` and `peykan simulate` as two
  processes, which can't share python-can's virtual bus, so the
  container's tools and dashboard saw no traffic. It now runs
  `peykan demo`; the compose file is a single service doing the same; both
  health checks call `/healthz` with curl (the old inline-Python check was
  malformed).
- The transmit policy classified `send_diagnostic_request` /
  `diag-request` by standard UDS service numbers, but the DBC's diagnostic
  protocol numbers its services differently: its `WRITE_MEMORY` (0x32)
  passed as a read on real hardware with only `PEYKAN_ALLOW_TRANSMIT`, and
  `READ_MEMORY` (0x31, RoutineControl in UDS) was refused as a write. It
  now uses the protocol's own read-only list (session start, read data,
  read memory) and treats everything else, including unknown IDs, as a
  write. `diagnostics.SERVICE_IDS` also had `WRITE_MEMORY` as 0x30; it's
  0x32 per the DBC, and a test now ties the two together.
- Log output went to stdout, so CLI commands that print JSON (e.g.
  `peykan uds-dtcs > dtcs.json`) got udsoncan's log lines mixed into it.
  All logging now goes to stderr, and udsoncan/can-isotp's per-request INFO
  messages are quieted (their warnings and errors still show).
- `peykan j1939-decode` guessed each payload byte's base separately, so
  "00 20 4e" read `20` as decimal next to hex `4e` and decoded 2500 rpm as
  2498.5. `decode` and `j1939-decode` now share one rule
  (`parsing.parse_data_bytes`): space-separated bytes are hex, as in
  candump; comma-separated bytes are decimal; `0x` prefixes work in both;
  values outside 0-255 are rejected instead of failing obscurely.
- `requirements.txt` pinned `rpds-py==2026.9.1`, which needs Python 3.11+,
  so CI on Python 3.10 (and the Ubuntu 22.04 Docker image) couldn't
  install it. Pinned to 0.30.0, the newest release supporting 3.10, with
  wheels for 3.10-3.14. The published `peykan` 0.2.0 package was
  unaffected: it doesn't pin rpds-py.

### Changed
- README: banner at the top, a new dashboard screenshot under the Peykan
  name, and absolute image URLs so images also render on the PyPI project
  page. Removed an unused, outdated Windsurf screenshot.
- CI/release workflows use `actions/checkout@v7` and `actions/setup-python@v7`
  (Node.js 24; v4/v5 ran on the deprecated Node.js 20).

## [0.2.0] - 2026-10-07

### Changed
- **Renamed from mcp-can to Peykan.** PyPI package `peykan`, import
  package `peykan`, CLI command `peykan`, environment prefix `PEYKAN_`,
  repository `farzadnadiri/peykan`. For compatibility the `mcp-can`
  command still works (with a deprecation note) and `MCP_CAN_*` variables
  are still read where the `PEYKAN_*` one isn't set (with a warning). The
  entries below this one use the name each release shipped under.
  `can-mcp.py` is now `peykan-server.py`.
- **Requires the `mcp` 2.x SDK (`mcp>=2.1.1,<3`)**, ported from `FastMCP`
  to `MCPServer`. The 1.x pin made `mcp-can` impossible to install next to
  current MCP tools such as `ollmcp`; they now share an environment.
  Tested against `mcp` 2.1.1 and 2.3.0. Sync tools now run on worker
  threads (v2 behaviour), so blocking bus reads no longer stall the
  server's event loop. `server/fastmcp_server.py` is now
  `server/mcp_server.py`; `main()` builds the ASGI app itself via
  `build_http_app()` (MCP transport + dashboard routes + CORS) instead of
  monkey-patching `sse_app`. Dropped the unused `httpx-sse` dependency and
  regenerated `requirements.txt` (Windows-only pins carry markers).
- **Listens on `127.0.0.1` by default** (was hardcoded `0.0.0.0`). New
  `MCP_CAN_MCP_HOST` setting and `--host` option on `server`/`demo`; the
  tools are unauthenticated, so exposing them to the network is now an
  explicit choice, and the server logs a warning when it is. On loopback,
  requests with a non-local `Host` header are rejected (421, DNS-rebinding
  protection); origins in `MCP_CAN_CORS_ALLOW_ORIGINS` are still admitted.
  The Dockerfile and compose file set `MCP_CAN_MCP_HOST=0.0.0.0`.

### Added
- **Transmit safety for real hardware** (`safety.py`). On anything but the
  virtual bus the tools are read-only until `MCP_CAN_ALLOW_TRANSMIT=true`;
  state-changing services (clear DTCs, ECU reset, UDS writes/routines,
  fault injection, log replay) also need `MCP_CAN_ALLOW_WRITE_SERVICES=true`.
  `MCP_CAN_TRANSMIT_ALLOWLIST` restricts arbitration IDs and
  `MCP_CAN_TRANSMIT_LOG_PATH` appends every frame sent or blocked as JSON
  lines. Enforced by wrapping the bus (`TransmitGuard.wrap`), so frames
  can-isotp sends itself are covered too. Refusals return status `blocked`;
  new `get_transmit_log` tool. The simulator refuses to start on real
  hardware unless `MCP_CAN_SIMULATOR_ON_HARDWARE=true`. New
  `MCP_CAN_CAN_BITRATE` setting, a `serial` extra (pyserial, for slcan), and
  a README section with adapter setup (SocketCAN, PCAN, Kvaser, Vector,
  slcan).
- **UDS over ISO-TP** (`uds.py`, `simulator/uds_ecu.py`), using can-isotp
  and udsoncan (new dependencies). Tools `read_vin` (OBD Mode 09 PID 02,
  multi-frame, with check-digit validation), `uds_read_data` (0x22),
  `uds_read_dtcs` (0x19 with status bits), `uds_clear_dtcs` (0x14); CLI
  `vin`, `uds-read`, `uds-dtcs`, `uds-clear`. The simulated engine ECU
  (0x7E0/0x7E8) implements session control, tester present, ECU reset,
  identification and live (F4xx) DIDs, and keeps DTC history: confirmed
  codes stay stored after a fault clears until 0x14.
- **J1939 multi-packet transport (BAM, J1939-21).** DM1s with more than one
  DTC are sent as TP.CM + TP.DT and reassembled by `read_j1939_dtcs`, the
  CLI and log analysis (`j1939.build_bam`, `TransportReassembler`,
  `latest_dm1`). New VEP1 PGN (battery potential, SPN 168).
- **Recorded CAN log analysis and replay** (`logs.py`): `.asc`, `.blf`,
  `.trc`, candump `.log`, `.csv` (`.mf4` with asammdf). Tools
  `list_can_logs`, `analyze_can_log` (IDs and rates, signal ranges per
  source message, OBD and J1939 DTCs, timing gaps on periodic IDs),
  `get_log_signal`, `replay_can_log`, `stop_log_replay`, and an
  `analyze_log` prompt. CLI `log-info`, `log-signal`, `replay`, `record`
  (optionally simulating and injecting a fault mid-recording) and
  `demo --log`. Clients may only read files under `MCP_CAN_LOG_DIR`. Ships
  `data/sample_drive.asc`, a 30 s simulated drive with a misfire at 15 s.
- **Four more fault presets**: `crash` (airbags deployed, vehicle stopped,
  doors unlocked, B0001), `door_ajar`, `misfire` (rough RPM, P0300, two
  J1939 DTCs via BAM) and `battery_low` (about 11.3 V, P0562). Presets can
  now change the driving state itself (`FaultPreset.state_effect`), so a
  fault shows up consistently on DBC signals, J1939, OBD-II and UDS.
- Battery voltage: `BATTERY_VOLTAGE` in the DBC's `ENGINE_STATUS`, J1939
  VEP1, OBD PID 0x42 and UDS DID F442, charging to about 14.2 V with the
  engine running.
- Built-in MCP prompts: `diagnose_vehicle`, `explain_dtc(code)`,
  `trip_summary(duration_s)`, `fault_drill(preset)`; and server
  `instructions` sent on connect telling clients which tool answers which
  question. Both aimed at small local models, which otherwise pick
  roundabout tools or skip steps.

### Fixed
- OBD-II Mode 01 replies were fixed demo values (speed always 50 km/h,
  coolant always 90 degC); they now follow the simulated vehicle, and PIDs
  0x0C (RPM) and 0x42 (module voltage) were added. The supported-PIDs
  bitmaps now cover 0x20/0x40 too (0x51 was advertised in a range that
  can't express it).
- `send_obd_request` / `mcp-can obd-request` took the *first* frame
  received as the answer, so periodic traffic (e.g. a J1939 EEC1 frame)
  was sometimes returned instead -- 2 calls in 60 against the simulator,
  and most of the time on a busy real bus. They now wait for a frame from
  an OBD response ID (0x7E8-0x7EF) that answers the requested service
  (positive or negative response).
- `read_j1939_dtcs` / `mcp-can j1939-dtcs` reported only the most recent
  DM1 from any ECU, hiding faults from every other ECU on a real truck.
  They now report each ECU's latest DM1: all active DTCs tagged with their
  `source_address`, lamps at their most severe state, plus a per-ECU
  `ecus` list.
- Simulated cabin temperature noise reduced to a realistic level, and a
  marginal engine warm-up test made robust.

## [0.1.4] - 2026-10-07

### Fixed
- **Simulator no longer reports emergencies during a normal drive.** Body
  and airbag signals that weren't tied to the driving state were drawn
  independently at random on every frame, so a car doing 60 km/h reported
  `CRASH_DETECTED`, open doors, a 61 degC cabin and random
  `SYSTEM_STATUS` faults (an LLM reading the bus rightly called it an
  emergency). `simulator/state.py` now models cabin/environment state too:
  doors closed, driver belted, passenger belt/airbag following occupancy,
  auto-lock above 15 km/h, cabin temperature settling to a 21.5 degC
  climate setpoint, ambient light drifting with headlights following it
  (and rain), wipers following rain and speed. Crashes and faults only come
  from fault presets. A test guards that every broadcast signal is modeled.

### Added
- GitHub repository link in the live dashboard's header and footer.

### Docs
- Ollama section: install `ollmcp` in its own environment (it needs the
  `mcp` 2.x SDK, `mcp-can` needs 1.x), turn thinking off with `/tm`
  (slow, and `ollmcp` sometimes drops the tool call after thinking), and
  keep Ollama on the dedicated GPU (`OLLAMA_VULKAN=0`,
  `OLLAMA_CONTEXT_LENGTH=8192`).

## [0.1.3] - 2026-09-28

### Changed
- MCP tools that take IDs (`request_j1939_pgn`'s `pgn`, `arbitration_id`
  on the decode/filter tools, `send_obd_request`'s `service`/`pid`,
  `send_diagnostic_request`'s IDs) now accept hex strings (`"0xF004"`,
  `"F004"`) as well as integers, and `pgn` also accepts J1939 acronyms
  (`"EEC1"`, `"ET1"`). Small local models were converting hex to decimal
  wrongly (e.g. requesting PGN 39652 for EEC1's 0xF004) and getting
  timeouts. A `request_j1939_pgn` timeout now lists the known PGNs so the
  model can correct itself. Shared parser: `mcp_can.parsing.parse_int`;
  the CLI uses it too, and `mcp-can j1939-request` accepts acronyms.

### Docs
- Rewrote the Ollama section around `ollmcp` (Ollama can't reach MCP
  servers by itself) and pointed at tool-calling models; the previous
  `llama3` suggestion doesn't support tools.

## [0.1.2] - 2026-09-28

### Changed
- `GET /` now redirects to `/dashboard` instead of returning 404, and the
  server logs the dashboard and MCP endpoint URLs at startup.

## [0.1.1] - 2026-09-28

### Fixed
- **`pip install mcp-can` now works outside a repo checkout.** The sample
  `vehicle.dbc` moved to `src/mcp_can/data/` and ships in the wheel;
  `MCP_CAN_DBC_PATH` defaults to that bundled copy (exposed as
  `mcp_can.config.DEFAULT_DBC_PATH`). Previously the default was a relative
  `vehicle.dbc`, so every DBC-backed command failed with `FileNotFoundError`
  unless run from the repo root.

### Changed
- Packaging: SPDX `license = "MIT"` + `license-files` (setuptools>=77),
  version single-sourced from `mcp_can.__version__`, `dev` extra
  (`pip install -e ".[dev]"`), explicit `__init__.py` for the `server` and
  `simulator` subpackages.
- CI tests Python 3.10–3.13 and gains a `package` job that builds the
  sdist/wheel, runs `twine check`, and smoke-tests the installed wheel from
  outside the checkout.

### Added
- **SAE J1939 support** (heavy-duty / 29-bit extended IDs), alongside the
  existing 11-bit light-vehicle bus:
  - `src/mcp_can/j1939.py` — self-contained protocol logic (not DBC-driven):
    29-bit ID decomposition (priority / PGN / source + destination address,
    PDU1 vs PDU2), a curated PGN/SPN catalog (EEC1, EEC2, ET1, CCVS1, LFE1,
    DD1) with encode/decode, J1939-73 DM1 diagnostic trouble codes
    (SPN/FMI/OC/CM pack + lamp status), and the Request PGN (`0xEA00`) helper.
  - `src/mcp_can/simulator/j1939_runner.py` — J1939 simulator threads:
    periodic PGN broadcasters driven by the same `VehicleState` as the 11-bit
    signals, a Request PGN responder, and a 1 Hz DM1 emitter. Fault-injection
    presets map to J1939 DTCs (`overheat` → SPN 110 FMI 0, `abs_fault` → SPN
    84 FMI 5, `low_fuel` → SPN 96 FMI 18) and drive the MIL lamp.
  - Four MCP tools: `decode_j1939_frame`, `list_j1939_pgns`,
    `request_j1939_pgn`, `read_j1939_dtcs`, plus matching CLI commands
    `mcp-can j1939-decode|j1939-pgns|j1939-request|j1939-dtcs`.
  - `live_state.py` now also decodes J1939 (29-bit) frames, so J1939 signals
    show up in `get_vehicle_snapshot` and the dashboard under
    `J1939:<acronym>`.
  - `MCP_CAN_J1939_ENABLED` setting (default `true`) to turn the J1939 side
    of the simulator off.
- `Settings.cors_allow_origins` (default `["*"]`, override via
  `MCP_CAN_CORS_ALLOW_ORIGINS`) so the SSE endpoint's CORS origins are
  configurable instead of hardcoded.
- Fault injection (`simulator/faults.py`): three named scenario presets
  (`overheat`, `abs_fault`, `low_fuel`) that force specific signals to
  fault-condition values and, where applicable, populate a matching DTC
  (`P0217`, `C0035`) visible in `send_obd_request`'s Mode 03 response.
  Activated via the new `activate_fault_scenario` MCP tool or `mcp-can
  fault <preset|clear|list>` CLI command; both round-trip a small control
  frame to the simulator process, the same pattern already used by
  OBD/diagnostic requests. `obd.py` gained `encode_dtc`/`decode_dtc`
  (J2012-style 2-byte DTC wire format) and `decode_response` (dispatches
  Mode 03 responses to DTC decoding instead of `decode_pid_value`, which
  only handles PID'd responses).
- Correlated, plausible signal generation (`simulator/state.py`): a
  background `VehicleState` ticks a small set of driving-dynamics variables
  (throttle, RPM, speed, engine temp, fuel level) with realistic lag and
  relationships between them, instead of `SimThread` drawing every signal
  independently at random. `ENGINE_SPEED`/`ENGINE_LOAD` now track throttle,
  `WHEEL_SPEED_*` tracks a common vehicle speed with small per-wheel jitter,
  `FUEL_LEVEL` only decreases, `ENGINE_TEMP` warms toward operating
  temperature over time (capped at 87.5, the actual ceiling its 8-bit width
  can encode -- see `ENGINE_TEMP_MAX_C` -- rather than the DBC's declared
  but unencodable 127.5 max). Signals with no correlation rule (doors,
  seatbelts, fault flags, etc.) keep the previous independent-random
  behavior. `SimThread` also gained a general clamp-to-encodable-range step
  for correlated/fault-overridden signal values, so a value outside what a
  signal's bit width can hold gets saturated instead of raising out of
  `Message.encode` and silently dropping that frame.
- `get_vehicle_snapshot` MCP tool (plus `mcp-can snapshot` CLI command):
  the last known value of every signal seen so far, one entry per signal
  with an `age_s` freshness indicator, instead of decoding a stream of raw
  frames yourself. Built on top of the frame history buffer's signal
  tracking (`live_state.py`), so it's effectively free given that already
  existed for the dashboard.
- Read-only live web dashboard at `/dashboard` (`server/live_state.py` +
  `server/templates/dashboard.html`): signal values grouped by ECU message
  and a recent-frames feed, updated over Server-Sent Events. Self-contained
  single page, no build step or external assets.
- UDS-style diagnostic responder (`DiagnosticResponderThread`) implementing
  the `DIAGNOSTIC_REQUEST`/`DIAGNOSTIC_RESPONSE_*` messages `vehicle.dbc`
  already defined but nothing previously answered.
- Two new MCP tools: `send_obd_request` and `send_diagnostic_request`,
  exposing OBD-II and the new diagnostic protocol to LLM clients (previously
  OBD-II was CLI-only).
- Matching CLI commands: `mcp-can diag-request`, plus a decoded-value field
  added to `mcp-can obd-request`'s output.
- `mcp-can dbc-info` command — pretty-printed table of a DBC's
  messages/signals, for discovery without reading the full JSON resource.
- `--json` flag on `decode`/`monitor` (default output is now a Rich table);
  `--transport` flag on `server`/`demo`.
- Structured MCP tool output: all tools now return typed Pydantic models
  (`server/schemas.py`) instead of ad-hoc dicts.
- Optional `streamable-http` transport (`MCP_CAN_MCP_TRANSPORT`), alongside
  the existing `sse`. Falls back with a clear error, rather than a raw SDK
  traceback, if the installed `mcp` version doesn't support it.
- `max_duration_s` setting (default 30s) capping every tool's
  `duration_s`/`timeout_s`, so a client can't tie up a listener indefinitely.
- Colorized, leveled logging via `rich.logging.RichHandler` in place of bare
  `print()` calls throughout the simulator and server.
- `/healthz` endpoint.
- `CONTRIBUTING.md`.

### Fixed
- Dashboard values with a `scale`/`offset` (e.g. `58 * 0.4`) could display
  IEEE-754 noise like `23.200000000000003`; `live_state.py` now rounds
  float values for display.
- **Diagnostic/OBD responder message theft**: two threads calling `.recv()`
  on the *same* `python-can` `Bus` instance silently split incoming messages
  between them instead of each seeing every message — the diagnostic
  responder would work or not depending on which thread happened to dequeue
  a given frame first. Each listener thread now gets its own bus instance.
- `int()` on a choice-decoded signal (e.g. `SERVICE_ID`, `RESPONSE_CODE`,
  which `cantools` decodes to a `NamedSignalValue` for known enum values)
  raised `TypeError`; added `dbc.signal_int()` to unwrap it consistently.
- `mcp>=1.7.0` had no upper bound, allowing `pip install` to resolve the
  breaking `mcp` 2.0 line, which removed the `fastmcp` API this project is
  built on; pinned `<2.0.0`.
- CORS `sse_app` monkey-patch crashed on newer `mcp` SDK versions that pass
  a `mount_path` argument the patch didn't accept.

### Changed
- CORS no longer sends `allow_credentials=True` unconditionally: it's now
  tied to `Settings.cors_allow_origins` and only enabled once that's
  narrowed away from the default `"*"`, since browsers reject the
  wildcard-origin + credentials combination outright anyway.
- `requires-python` corrected from `>=3.8` to `>=3.10` (the code already
  used `X | None` union syntax that only works natively on 3.10+).
- Removed redundant `mcp-can-server`/`mcp-can-sim` console-script entries —
  `mcp-can server`/`mcp-can simulate` are the one canonical entrypoint.
- Removed dead code (`models.FrameView`/`frame_to_view`, unused since
  introduction).
- `read_can_frames`, `filter_frames`, `monitor_signal` no longer open a
  fresh bus listener per call (each racing the risk of missing frames sent
  between calls, or being stolen by a competing listener on a shared bus
  instance). They now read from `live_state.py`'s continuously-running
  frame history buffer — same background listener that backs the
  dashboard — so they return near-instantly instead of blocking for
  `duration_s`, and no longer accept a `ctx` progress-reporting parameter
  (there's no longer a live poll loop to report progress on).
