import { useEffect, useRef, useState } from "react";
import { getToolName, isToolUIPart, type UIMessage } from "ai";
import { Streamdown } from "streamdown";
import {
  ArrowClockwiseIcon,
  BrainIcon,
  CaretDownIcon,
  CheckCircleIcon,
  CircleNotchIcon,
  PaperPlaneRightIcon,
  SparkleIcon,
  StopIcon,
  WarningCircleIcon,
  WrenchIcon
} from "@phosphor-icons/react";

const TOOL_LABELS: Record<string, string> = {
  get_vehicle_snapshot: "Read all live signals",
  read_can_frames: "Captured raw CAN frames",
  filter_frames: "Filtered CAN frames",
  monitor_signal: "Monitored a signal",
  decode_can_frame: "Decoded a CAN frame",
  send_obd_request: "Sent an OBD-II request",
  send_diagnostic_request: "Sent a diagnostic request",
  activate_fault_scenario: "Changed the fault scenario",
  decode_j1939_frame: "Decoded a J1939 frame",
  list_j1939_pgns: "Listed J1939 parameter groups",
  request_j1939_pgn: "Requested a J1939 parameter group",
  read_j1939_dtcs: "Read J1939 trouble codes",
  read_vin: "Read the VIN",
  uds_read_data: "Read ECU data over UDS",
  uds_read_dtcs: "Read stored trouble codes (UDS)",
  uds_clear_dtcs: "Cleared trouble codes (UDS)",
  get_transmit_log: "Checked the transmit log",
  list_can_logs: "Listed recorded logs",
  analyze_can_log: "Analysed a recorded log",
  get_log_signal: "Extracted a signal from a log",
  replay_can_log: "Started a log replay",
  stop_log_replay: "Stopped the log replay"
};

export const SUGGESTIONS = [
  { title: "What's happening right now?", prompt: "What are the vehicle's speed, engine RPM, coolant temperature and battery voltage right now?" },
  { title: "Identify the car", prompt: "Read the VIN and tell me what the ECU reports about itself." },
  { title: "Run a health check", prompt: "Is anything wrong with my car? Check the live signals and every kind of trouble code." },
  { title: "Investigate a recording", prompt: "Analyse the sample drive log and tell me what happened during it." }
];

type Part = UIMessage["parts"][number];

function ToolCard({ part }: { part: Part }) {
  const [open, setOpen] = useState(false);
  if (!isToolUIPart(part)) return null;
  const name = getToolName(part);
  const label = TOOL_LABELS[name] ?? name;
  const running = part.state === "input-streaming" || part.state === "input-available";
  const failed = part.state === "output-error";
  return (
    <div className="rounded-xl border border-ink-700 bg-ink-900/70 text-sm">
      <button type="button" onClick={() => setOpen((o) => !o)} className="flex w-full items-center gap-2 px-3 py-2 text-left">
        {running ? (
          <CircleNotchIcon size={14} className="animate-spin text-cyan-glow" />
        ) : failed ? (
          <WarningCircleIcon size={14} className="text-bad" />
        ) : (
          <CheckCircleIcon size={14} className="text-ok" />
        )}
        <WrenchIcon size={14} className="text-ink-400" />
        <span className="text-ink-100">{running ? `${label}…` : label}</span>
        <span className="ml-auto font-mono text-[11px] text-ink-400">{name}</span>
        <CaretDownIcon size={12} className={`text-ink-400 transition ${open ? "rotate-180" : ""}`} />
      </button>
      {open && (
        <div className="space-y-2 border-t border-ink-800 px-3 py-2">
          <Pre label="Input" value={part.input} />
          {"output" in part && <Pre label="Result" value={part.output} />}
          {failed && <Pre label="Error" value={part.errorText} />}
        </div>
      )}
    </div>
  );
}

function Pre({ label, value }: { label: string; value: unknown }) {
  if (value === undefined || value === null) return null;
  const textValue = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return (
    <div>
      <div className="mb-0.5 text-[11px] font-semibold uppercase tracking-wider text-ink-400">{label}</div>
      <pre className="max-h-60 overflow-auto rounded-lg bg-ink-950 p-2 font-mono text-[11px] leading-relaxed text-ink-300 scroll-thin">{textValue}</pre>
    </div>
  );
}

function Reasoning({ textValue, done }: { textValue: string; done: boolean }) {
  return (
    <details className="group text-sm" open={!done}>
      <summary className="flex cursor-pointer select-none items-center gap-1.5 text-xs text-ink-400 hover:text-ink-300">
        <BrainIcon size={14} />
        {done ? "Reasoning" : "Thinking…"}
        <CaretDownIcon size={12} className="transition group-open:rotate-180" />
      </summary>
      <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded-lg bg-ink-900 p-2 font-sans text-xs text-ink-400 scroll-thin">{textValue}</pre>
    </details>
  );
}

export function Chat({
  messages,
  status,
  error,
  onSend,
  onStop,
  onNewChat,
  nudge,
  messagesLeft
}: {
  messages: UIMessage[];
  status: "ready" | "submitted" | "streaming" | "error";
  error: Error | undefined;
  onSend: (text: string) => void;
  onStop: () => void;
  onNewChat: () => void;
  nudge: string | null;
  messagesLeft: number;
}) {
  const [input, setInput] = useState("");
  const endRef = useRef<HTMLDivElement>(null);
  const busy = status === "submitted" || status === "streaming";

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, status]);

  const send = (value: string) => {
    const trimmed = value.trim();
    if (!trimmed || busy) return;
    onSend(trimmed);
    setInput("");
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 overflow-y-auto px-4 py-6 scroll-thin">
        <div className="mx-auto flex max-w-3xl flex-col gap-5">
          {messages.length === 0 && (
            <div className="pt-6 text-center sm:pt-14">
              <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-2xl border border-cyan-glow/30 bg-cyan-soft text-cyan-glow">
                <SparkleIcon size={24} />
              </div>
              <h1 className="text-2xl font-semibold text-white sm:text-3xl">Talk to your car</h1>
              <p className="mx-auto mt-2 max-w-md text-sm text-ink-300">
                An AI agent connected to a simulated vehicle over CAN, OBD-II, UDS and J1939. Ask it anything, or break the car with a fault and see if it can find out what's wrong.
              </p>
              <div className="mt-8 grid gap-2 text-left sm:grid-cols-2">
                {SUGGESTIONS.map((s) => (
                  <button
                    key={s.title}
                    type="button"
                    onClick={() => send(s.prompt)}
                    className="rounded-xl border border-ink-700 bg-ink-850/70 p-3 transition hover:border-cyan-glow/40 hover:bg-ink-800"
                  >
                    <div className="text-sm font-medium text-white">{s.title}</div>
                    <div className="mt-0.5 line-clamp-2 text-xs text-ink-400">{s.prompt}</div>
                  </button>
                ))}
              </div>
            </div>
          )}

          {messages.map((message, mi) => {
            const isUser = message.role === "user";
            const isLast = mi === messages.length - 1;
            return (
              <div key={message.id} className={`flex flex-col gap-2 ${isUser ? "items-end" : "items-start"}`}>
                {message.parts.map((part, i) => {
                  const key = `${message.id}-${i}`;
                  if (isToolUIPart(part)) return <div key={key} className="w-full max-w-[92%]"><ToolCard part={part} /></div>;
                  if (part.type === "reasoning" && part.text.trim()) {
                    return (
                      <div key={key} className="w-full max-w-[92%]">
                        <Reasoning textValue={part.text} done={part.state === "done" || !(isLast && busy)} />
                      </div>
                    );
                  }
                  if (part.type === "text" && part.text) {
                    return isUser ? (
                      <div key={key} className="max-w-[85%] rounded-2xl rounded-br-md bg-peykan px-4 py-2.5 text-[15px] leading-relaxed text-white">
                        {part.text}
                      </div>
                    ) : (
                      <div key={key} className="prose-chat max-w-[92%] text-[15px] leading-relaxed text-ink-100">
                        <Streamdown controls={false} isAnimating={isLast && busy}>
                          {part.text}
                        </Streamdown>
                      </div>
                    );
                  }
                  return null;
                })}
              </div>
            );
          })}

          {status === "submitted" && (
            <div className="flex items-center gap-2 text-sm text-ink-400">
              <CircleNotchIcon size={16} className="animate-spin text-cyan-glow" />
              {messages.length <= 1 ? "Starting your car and the agent…" : "Thinking…"}
            </div>
          )}
          {error && (
            <div className="rounded-xl border border-bad/40 bg-bad/10 px-3 py-2 text-sm text-bad">
              Something went wrong: {error.message}
            </div>
          )}
          <div ref={endRef} />
        </div>
      </div>

      <div className="border-t border-ink-800 bg-ink-950/80 px-4 py-3 backdrop-blur">
        <div className="mx-auto max-w-3xl">
          {nudge && !busy && (
            <button
              type="button"
              onClick={() => send(nudge)}
              className="mb-2 inline-flex items-center gap-1.5 rounded-full border border-peykan/50 bg-peykan-soft px-3 py-1 text-xs text-white hover:border-peykan"
            >
              <SparkleIcon size={12} className="text-peykan" />
              {nudge}
            </button>
          )}
          <form
            onSubmit={(e) => {
              e.preventDefault();
              send(input);
            }}
            className="flex items-end gap-2 rounded-2xl border border-ink-700 bg-ink-900 p-2 focus-within:border-cyan-glow/50"
          >
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send(input);
                }
              }}
              rows={1}
              placeholder="Ask about the car…"
              aria-label="Message"
              className="max-h-40 min-h-[40px] flex-1 resize-none bg-transparent px-2 py-2 text-[15px] text-white placeholder:text-ink-400 focus:outline-none"
            />
            {busy ? (
              <button type="button" onClick={onStop} aria-label="Stop" className="flex h-10 w-10 items-center justify-center rounded-xl bg-ink-700 text-white hover:bg-ink-600">
                <StopIcon size={18} weight="fill" />
              </button>
            ) : (
              <button type="submit" aria-label="Send" disabled={!input.trim()} className="flex h-10 w-10 items-center justify-center rounded-xl bg-peykan text-white transition hover:brightness-110 disabled:bg-ink-700 disabled:text-ink-400">
                <PaperPlaneRightIcon size={18} weight="fill" />
              </button>
            )}
          </form>
          <div className="mt-2 flex items-center justify-between text-[11px] text-ink-400">
            <span>Simulated vehicle · the AI can make mistakes · {messagesLeft} messages left</span>
            {messages.length > 0 && (
              <button type="button" onClick={onNewChat} className="inline-flex items-center gap-1 hover:text-ink-100">
                <ArrowClockwiseIcon size={12} /> New chat
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
