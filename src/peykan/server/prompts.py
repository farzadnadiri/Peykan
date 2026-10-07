"""Server instructions and ready-made MCP prompts.

Small local models (e.g. an 8B model via Ollama) pick roundabout tools or
miss steps when left to plan a diagnosis from twelve tool descriptions
alone. `SERVER_INSTRUCTIONS` gives every client a short map of which tool
answers which question, and the prompts package common multi-step tasks
(a health check, a DTC explanation, a fault drill) so a user can invoke
them by name instead of hoping the model sequences the calls itself.
"""
from mcp.server.mcpserver import MCPServer

from ..simulator.faults import PRESETS

SERVER_INSTRUCTIONS = """\
This server reads a vehicle's CAN bus: 11-bit light-vehicle signals from a
DBC, OBD-II, UDS-style diagnostics and SAE J1939 (heavy-duty). By default the
vehicle is a built-in simulator.

- Current value of any signal (speed, RPM, temperatures, doors, ...): call
  get_vehicle_snapshot first. It returns every signal in one call.
- Trouble codes: send_obd_request with service=3 (light vehicle),
  uds_read_dtcs (stored codes with status, including past faults) and
  read_j1939_dtcs (heavy-duty DM1).
- VIN: read_vin. ECU identification and live values over UDS: uds_read_data.
- Recorded log files: list_can_logs, then analyze_can_log and get_log_signal.
- How a signal changes over time: monitor_signal.
- Tools that take IDs (PGNs, CAN IDs, OBD services/PIDs, UDS services) accept
  hex strings such as "0x0D" and J1939 acronyms such as "EEC1". Pass them
  as-is; don't convert hex to decimal.
- Only call activate_fault_scenario, uds_clear_dtcs or replay_can_log when
  the user asks for it: they change ECU or bus state. A "blocked" status
  means the transmit policy refused (real hardware is read-only by default);
  explain that rather than retrying. get_transmit_log shows the policy.
"""


def register_prompts(mcp: MCPServer) -> None:
    @mcp.prompt()
    def diagnose_vehicle() -> str:
        """Health check: current signals plus active trouble codes, summarised."""
        return """\
Run a health check on the vehicle connected to this CAN bus.

1. Call get_vehicle_snapshot. Look for anything abnormal: SYSTEM_STATUS other
   than OK, CRASH_DETECTED=1, doors open while moving, engine/coolant
   temperature pinned at its maximum, fuel below 10%, wheel speeds that
   disagree with each other or with vehicle speed.
2. Call send_obd_request with service=3 to read OBD-II trouble codes, and
   uds_read_dtcs for stored codes with their status (present now, or only
   stored from earlier).
3. Call read_j1939_dtcs to read active J1939 trouble codes.

Then report:
- Overall status: OK, needs attention, or stop driving.
- Each issue found, with the signal values or codes that show it and the
  likely cause.
- A recommended next step for each issue.
If nothing is wrong, say so briefly and quote the key values (speed, RPM,
coolant temperature, fuel level)."""

    @mcp.prompt()
    def explain_dtc(code: str) -> str:
        """Explain a diagnostic trouble code and check whether it's active now."""
        return f"""\
Explain diagnostic trouble code {code}.

1. Say which system it belongs to and what it means in plain language.
   (OBD-II codes start with P/C/B/U; J1939 codes are SPN + FMI.)
2. List the most common causes, most likely first.
3. Check whether it is active on this vehicle right now: call
   send_obd_request with service=3, and read_j1939_dtcs.
4. Say how urgent it is and what to check first."""

    @mcp.prompt()
    def trip_summary(duration_s: float = 10.0) -> str:
        """Summarise driving over the last few seconds (speed, RPM, throttle)."""
        return f"""\
Summarise how the vehicle has been driven over the last {duration_s:g} seconds.

Call monitor_signal with duration_s={duration_s:g} for each of ENGINE_SPEED,
THROTTLE_POSITION and WHEEL_SPEED_FL (vehicle speed in km/h). Then report
minimum, average and maximum for each, whether the vehicle was accelerating,
cruising or slowing down, and anything unusual (e.g. high RPM at low speed)."""

    preset_list = ", ".join(PRESETS)

    @mcp.prompt()
    def fault_drill(preset: str = "overheat") -> str:
        """Inject a simulated fault, diagnose it blind, then clear it."""
        return f"""\
Run a fault-injection drill on the simulator. Available presets: {preset_list}.

1. Call activate_fault_scenario with preset="{preset}".
2. Diagnose the vehicle without relying on the preset's name: call
   get_vehicle_snapshot, send_obd_request with service=3, and
   read_j1939_dtcs, and explain what is wrong from the evidence alone.
3. Compare your diagnosis with the preset's description returned in step 1,
   and say whether you got it right.
4. Call activate_fault_scenario with no preset to clear the fault."""

    @mcp.prompt()
    def analyze_log(path: str = "sample") -> str:
        """Investigate a recorded CAN log file and report what happened."""
        return f"""Investigate the recorded CAN log "{path}".

1. Call analyze_can_log with path="{path}".
2. From the result, note the duration, which ECUs/messages are present, and
   any trouble codes (obd_dtcs, j1939_dtcs) or timing gaps.
3. For any signal whose range looks abnormal (e.g. engine speed swinging
   wildly, voltage below 12.5 V while running, temperature at its maximum),
   call get_log_signal for it and find when the change started.

Report a short timeline of what happened during the recording, the likely
root cause of any problem, and the evidence (signal values, times, codes)."""
