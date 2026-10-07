import { useEffect, useRef, useState } from "react";

export type Signal = { value: number | string; unit: string; message: string; timestamp: number };
export type Snapshot = { signals: Record<string, Signal>; frame_count: number; uptime_s: number };
export type StreamStatus = "starting" | "live" | "reconnecting";

const HISTORY = 90; // ~45 s at the dashboard's 2 updates/s

/** The visitor's vehicle, live: Peykan's dashboard SSE feed, proxied by the
 *  Worker. The first connect also starts (and waits for) the container. */
export function useVehicleStream(enabled: boolean) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [status, setStatus] = useState<StreamStatus>("starting");
  const history = useRef<{ speed: number[]; rpm: number[] }>({ speed: [], rpm: [] });

  useEffect(() => {
    if (!enabled) return;
    const source = new EventSource("/api/vehicle/stream");
    source.onmessage = (event) => {
      const data = JSON.parse(event.data) as Snapshot;
      const speed = Number(data.signals.WHEEL_BASED_VEHICLE_SPEED?.value ?? data.signals.WHEEL_SPEED_FL?.value);
      const rpm = Number(data.signals.ENGINE_SPEED?.value);
      const h = history.current;
      if (Number.isFinite(speed)) h.speed = [...h.speed, speed].slice(-HISTORY);
      if (Number.isFinite(rpm)) h.rpm = [...h.rpm, rpm].slice(-HISTORY);
      setSnapshot(data);
      setStatus("live");
    };
    // EventSource reconnects by itself; just reflect it in the UI.
    source.onerror = () => setStatus((s) => (s === "starting" ? s : "reconnecting"));
    return () => source.close();
  }, [enabled]);

  return { snapshot, status, history: history.current };
}

export function num(snapshot: Snapshot | null, name: string): number | null {
  const v = snapshot?.signals[name]?.value;
  const n = typeof v === "number" ? v : Number(v);
  return v === undefined || Number.isNaN(n) ? null : n;
}

export function text(snapshot: Snapshot | null, name: string): string | null {
  const v = snapshot?.signals[name]?.value;
  return v === undefined ? null : String(v);
}
