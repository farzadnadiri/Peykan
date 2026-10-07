import { useState } from "react";
import {
  BatteryWarningIcon,
  BroomIcon,
  CarProfileIcon,
  CaretDownIcon,
  CircleNotchIcon,
  DoorIcon,
  DropIcon,
  EngineIcon,
  GasPumpIcon,
  LockIcon,
  LockOpenIcon,
  SirenIcon,
  ThermometerIcon,
  TireIcon
} from "@phosphor-icons/react";
import { num, text, type Snapshot, type StreamStatus } from "../useVehicleStream";

export type FaultPreset = {
  id: string;
  label: string;
  hint: string;
  icon: React.ReactNode;
};

export const FAULTS: FaultPreset[] = [
  { id: "overheat", label: "Overheat", hint: "Coolant at its maximum", icon: <ThermometerIcon size={16} /> },
  { id: "misfire", label: "Misfire", hint: "Rough, erratic engine speed", icon: <EngineIcon size={16} /> },
  { id: "battery_low", label: "Battery low", hint: "Charging failure, ~11.3 V", icon: <BatteryWarningIcon size={16} /> },
  { id: "abs_fault", label: "ABS fault", hint: "Wheel-speed sensors read zero", icon: <TireIcon size={16} /> },
  { id: "low_fuel", label: "Low fuel", hint: "Fuel critically low", icon: <GasPumpIcon size={16} /> },
  { id: "door_ajar", label: "Door ajar", hint: "Rear-right door open while driving", icon: <DoorIcon size={16} /> },
  { id: "crash", label: "Crash", hint: "Airbags deployed, car stopped", icon: <SirenIcon size={16} /> }
];

function Arc({ value, max, label, unit, digits = 0 }: { value: number | null; max: number; label: string; unit: string; digits?: number }) {
  const fraction = value === null ? 0 : Math.max(0, Math.min(1, value / max));
  // 240° sweep, open at the bottom.
  const r = 52;
  const circumference = 2 * Math.PI * r;
  const sweep = circumference * (240 / 360);
  return (
    <div className="relative flex flex-col items-center">
      <svg viewBox="0 0 140 120" className="w-full max-w-[180px]" aria-hidden="true">
        <defs>
          <linearGradient id={`g-${label}`} x1="0" x2="1">
            <stop offset="0%" stopColor="var(--color-cyan-glow)" />
            <stop offset="100%" stopColor="var(--color-peykan)" />
          </linearGradient>
        </defs>
        <g transform="rotate(150 70 70)">
          <circle cx="70" cy="70" r={r} fill="none" stroke="var(--color-ink-700)" strokeWidth="9" strokeLinecap="round" strokeDasharray={`${sweep} ${circumference}`} />
          <circle
            cx="70"
            cy="70"
            r={r}
            fill="none"
            stroke={`url(#g-${label})`}
            strokeWidth="9"
            strokeLinecap="round"
            strokeDasharray={`${sweep * fraction} ${circumference}`}
            style={{ transition: "stroke-dasharray 450ms ease-out" }}
          />
        </g>
      </svg>
      <div className="absolute inset-x-0 top-[34%] flex flex-col items-center">
        <span className="font-mono text-3xl font-semibold tabular-nums text-white">
          {value === null ? "--" : value.toFixed(digits)}
        </span>
        <span className="text-xs text-ink-400">{unit}</span>
      </div>
      <span className="-mt-3 text-xs font-medium uppercase tracking-wider text-ink-300">{label}</span>
    </div>
  );
}

type Level = "ok" | "warn" | "bad";
const levelColor: Record<Level, string> = { ok: "bg-cyan-glow", warn: "bg-warn", bad: "bg-bad" };

function Tile({ icon, label, value, unit, fraction, level = "ok", digits = 1 }: {
  icon: React.ReactNode; label: string; value: number | null; unit: string; fraction: number; level?: Level; digits?: number;
}) {
  return (
    <div className="rounded-xl border border-ink-700 bg-ink-850/80 p-3">
      <div className="flex items-center gap-1.5 text-xs text-ink-400">
        {icon}
        {label}
      </div>
      <div className="mt-1 flex items-baseline gap-1">
        <span className={`font-mono text-xl font-semibold tabular-nums ${level === "ok" ? "text-white" : level === "warn" ? "text-warn" : "text-bad"}`}>
          {value === null ? "--" : value.toFixed(digits)}
        </span>
        <span className="text-xs text-ink-400">{unit}</span>
      </div>
      <div className="mt-2 h-1 overflow-hidden rounded-full bg-ink-700">
        <div className={`h-full rounded-full ${levelColor[level]}`} style={{ width: `${Math.max(0, Math.min(1, fraction)) * 100}%`, transition: "width 450ms ease-out" }} />
      </div>
    </div>
  );
}

function Chip({ level, children }: { level: Level | "neutral"; children: React.ReactNode }) {
  const styles = {
    ok: "border-ok/30 bg-ok/10 text-ok",
    warn: "border-warn/30 bg-warn/10 text-warn",
    bad: "border-bad/40 bg-bad/15 text-bad",
    neutral: "border-ink-600 bg-ink-800 text-ink-300"
  }[level];
  return <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs ${styles}`}>{children}</span>;
}

function Sparkline({ values, max }: { values: number[]; max: number }) {
  if (values.length < 2) return <div className="h-10" />;
  const points = values
    .map((v, i) => `${(i / (values.length - 1)) * 100},${40 - Math.max(0, Math.min(1, v / max)) * 38}`)
    .join(" ");
  return (
    <svg viewBox="0 0 100 40" preserveAspectRatio="none" className="h-10 w-full" aria-hidden="true">
      <polyline points={points} fill="none" stroke="var(--color-cyan-glow)" strokeWidth="1.2" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

export function VehiclePanel({
  snapshot,
  status,
  history,
  vin,
  activeFault,
  pendingFault,
  onFault
}: {
  snapshot: Snapshot | null;
  status: StreamStatus;
  history: { speed: number[]; rpm: number[] };
  vin: string | null;
  activeFault: string | null;
  pendingFault: string | null | undefined;
  onFault: (preset: string | null) => void;
}) {
  const [showSignals, setShowSignals] = useState(false);
  const speed = num(snapshot, "WHEEL_BASED_VEHICLE_SPEED") ?? num(snapshot, "WHEEL_SPEED_FL");
  const rpm = num(snapshot, "ENGINE_SPEED");
  const coolant = num(snapshot, "ENGINE_TEMP");
  const battery = num(snapshot, "BATTERY_VOLTAGE");
  const fuel = num(snapshot, "FUEL_LEVEL");
  const throttle = num(snapshot, "THROTTLE_POSITION");
  const crash = num(snapshot, "CRASH_DETECTED") === 1;
  const system = text(snapshot, "SYSTEM_STATUS");
  const doorsOpen = ["FL", "FR", "RL", "RR"].filter((d) => num(snapshot, `DOOR_OPEN_${d}`) === 1);
  const locked = num(snapshot, "VEHICLE_LOCKED") === 1;
  const wipers = text(snapshot, "WIPER_STATUS");

  return (
    <div className="flex h-full flex-col gap-4 overflow-y-auto p-4 scroll-thin">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <CarProfileIcon size={20} className="text-peykan" />
          <div>
            <h2 className="text-sm font-semibold text-white">Your simulated car</h2>
            <p className="font-mono text-[11px] text-ink-400">{vin ? `VIN ${vin}` : "Private to this session"}</p>
          </div>
        </div>
        {status === "live" ? (
          <Chip level="ok"><span className="live-dot h-1.5 w-1.5 rounded-full bg-ok" />Live</Chip>
        ) : status === "starting" ? (
          <Chip level="neutral"><CircleNotchIcon size={12} className="animate-spin" />Starting engine…</Chip>
        ) : (
          <Chip level="warn">Reconnecting…</Chip>
        )}
      </div>

      <div className="grid grid-cols-2 gap-2 rounded-2xl border border-ink-700 bg-ink-900/70 p-3">
        <Arc value={speed} max={180} label="Speed" unit="km/h" />
        <Arc value={rpm} max={7000} label="Engine" unit="rpm" />
        <Sparkline values={history.speed} max={180} />
        <Sparkline values={history.rpm} max={7000} />
      </div>

      <div className="grid grid-cols-2 gap-2">
        <Tile icon={<ThermometerIcon size={14} />} label="Coolant" value={coolant} unit="°C" fraction={((coolant ?? -40) + 40) / 127.5} level={coolant !== null && coolant >= 87 ? "bad" : "ok"} />
        <Tile icon={<BatteryWarningIcon size={14} />} label="Battery" value={battery} unit="V" fraction={((battery ?? 10) - 10) / 5} level={battery !== null && battery < 12 ? "bad" : battery !== null && battery < 13 ? "warn" : "ok"} />
        <Tile icon={<GasPumpIcon size={14} />} label="Fuel" value={fuel} unit="%" fraction={(fuel ?? 0) / 100} level={fuel !== null && fuel < 10 ? "bad" : "ok"} digits={0} />
        <Tile icon={<EngineIcon size={14} />} label="Throttle" value={throttle} unit="%" fraction={(throttle ?? 0) / 100} digits={0} />
      </div>

      <div className="flex flex-wrap gap-1.5">
        <Chip level={crash ? "bad" : "ok"}><SirenIcon size={12} />{crash ? "Crash detected" : "No crash"}</Chip>
        <Chip level={system && system !== "OK" ? "bad" : "ok"}>Airbag system {system ?? "--"}</Chip>
        <Chip level={doorsOpen.length ? "warn" : "ok"}><DoorIcon size={12} />{doorsOpen.length ? `Door open: ${doorsOpen.join(", ")}` : "Doors closed"}</Chip>
        <Chip level="neutral">{locked ? <LockIcon size={12} /> : <LockOpenIcon size={12} />}{locked ? "Locked" : "Unlocked"}</Chip>
        {wipers && wipers !== "OFF" && <Chip level="neutral"><DropIcon size={12} />Wipers {wipers.toLowerCase().replace("_", " ")}</Chip>}
      </div>

      <section>
        <div className="mb-2 flex items-center justify-between">
          <h3 className="text-xs font-semibold uppercase tracking-wider text-ink-300">Inject a fault</h3>
          <button
            type="button"
            onClick={() => onFault(null)}
            disabled={!activeFault || pendingFault !== undefined}
            className="inline-flex items-center gap-1 rounded-lg px-2 py-1 text-xs text-ink-300 hover:bg-ink-800 hover:text-white disabled:opacity-40"
          >
            <BroomIcon size={14} /> Clear
          </button>
        </div>
        <div className="grid grid-cols-2 gap-1.5">
          {FAULTS.map((f) => {
            const active = activeFault === f.id;
            const pending = pendingFault === f.id;
            return (
              <button
                key={f.id}
                type="button"
                title={f.hint}
                onClick={() => onFault(f.id)}
                disabled={pendingFault !== undefined || status !== "live"}
                className={`flex items-center gap-2 rounded-xl border px-3 py-2 text-left text-sm transition disabled:opacity-50 ${
                  active
                    ? "border-peykan bg-peykan-soft text-white shadow-[0_0_0_1px_var(--color-peykan)]"
                    : "border-ink-700 bg-ink-850 text-ink-100 hover:border-ink-600 hover:bg-ink-800"
                }`}
              >
                <span className={active ? "text-peykan" : "text-ink-400"}>
                  {pending ? <CircleNotchIcon size={16} className="animate-spin" /> : f.icon}
                </span>
                {f.label}
              </button>
            );
          })}
        </div>
      </section>

      <section className="rounded-xl border border-ink-700 bg-ink-900/60">
        <button
          type="button"
          onClick={() => setShowSignals((s) => !s)}
          className="flex w-full items-center justify-between px-3 py-2 text-xs font-semibold uppercase tracking-wider text-ink-300"
        >
          All signals {snapshot ? `(${Object.keys(snapshot.signals).length})` : ""}
          <CaretDownIcon size={14} className={showSignals ? "rotate-180" : ""} />
        </button>
        {showSignals && snapshot && (
          <table className="w-full text-xs">
            <tbody>
              {Object.entries(snapshot.signals)
                .sort(([a], [b]) => a.localeCompare(b))
                .map(([name, s]) => (
                  <tr key={name} className="border-t border-ink-800">
                    <td className="px-3 py-1 font-mono text-ink-300">{name}</td>
                    <td className="px-3 py-1 text-right font-mono tabular-nums text-white">
                      {s.value} <span className="text-ink-400">{s.unit}</span>
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
