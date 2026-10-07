import { useCallback, useEffect, useState } from "react";
import { useAgent } from "agents/react";
import { useAgentChat } from "@cloudflare/ai-chat/react";
import { BookOpenIcon, CarProfileIcon, ChatCircleDotsIcon, CircleNotchIcon, GithubLogoIcon, PackageIcon } from "@phosphor-icons/react";
import type { ChatAgent } from "./agent";
import { Chat } from "./components/Chat";
import { Gate } from "./components/Gate";
import { FAULTS, VehiclePanel } from "./components/VehiclePanel";
import { useVehicleStream } from "./useVehicleStream";

type SessionInfo = { sessionId: string | null; turnstileSiteKey: string; maxUserMessages: number };

const LINKS = [
  { href: "https://github.com/farzadnadiri/peykan", label: "GitHub", icon: <GithubLogoIcon size={16} /> },
  { href: "https://github.com/farzadnadiri/peykan/wiki", label: "Docs", icon: <BookOpenIcon size={16} /> },
  { href: "https://pypi.org/project/peykan/", label: "PyPI", icon: <PackageIcon size={16} /> }
];

function Header({ connected }: { connected: boolean | null }) {
  return (
    <header className="flex items-center justify-between border-b border-ink-800 bg-ink-950/70 px-4 py-2.5 backdrop-blur">
      <a href="/" className="flex items-center gap-2.5">
        <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-peykan text-white">
          <CarProfileIcon size={18} weight="bold" />
        </span>
        <span className="text-lg font-semibold tracking-tight text-white">Peykan</span>
        <span className="hidden text-sm text-ink-400 md:inline">Connecting AI agents to vehicle systems</span>
      </a>
      <div className="flex items-center gap-1">
        {connected === false && (
          <span className="mr-2 inline-flex items-center gap-1.5 text-xs text-warn">
            <CircleNotchIcon size={12} className="animate-spin" /> Reconnecting
          </span>
        )}
        {LINKS.map((l) => (
          <a
            key={l.label}
            href={l.href}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-sm text-ink-300 hover:bg-ink-800 hover:text-white"
          >
            {l.icon}
            <span className="hidden sm:inline">{l.label}</span>
          </a>
        ))}
      </div>
    </header>
  );
}

function Workspace({ sessionId, maxUserMessages }: { sessionId: string; maxUserMessages: number }) {
  const [connected, setConnected] = useState<boolean | null>(null);
  const [tab, setTab] = useState<"chat" | "car">("chat");
  const [vin, setVin] = useState<string | null>(null);
  const [activeFault, setActiveFault] = useState<string | null>(null);
  // undefined: idle; a preset id or null (clear) while that request runs.
  const [pendingFault, setPendingFault] = useState<string | null | undefined>(undefined);
  const [nudge, setNudge] = useState<string | null>(null);

  const agent = useAgent<ChatAgent>({
    agent: "ChatAgent",
    name: sessionId,
    onOpen: useCallback(() => setConnected(true), []),
    onClose: useCallback(() => setConnected(false), [])
  });
  const { messages, sendMessage, stop, status, error, clearHistory } = useAgentChat({ agent, experimental_throttle: 80 });
  const vehicle = useVehicleStream(true);

  // Once the car is running, read its VIN for the panel header.
  const live = vehicle.status === "live";
  useEffect(() => {
    if (!live || vin) return;
    agent.stub
      .readVin()
      .then((r: unknown) => setVin((r as { vin?: string }).vin ?? null))
      .catch(() => {});
  }, [live, vin, agent]);

  const onFault = async (preset: string | null) => {
    setPendingFault(preset);
    try {
      const result = (await agent.stub.injectFault(preset)) as { status?: string };
      if (result.status === "success") {
        setActiveFault(preset);
        const label = FAULTS.find((f) => f.id === preset)?.label;
        setNudge(preset ? "Something feels off with my car. Can you find out what's wrong?" : null);
        if (label) setTab("chat");
      }
    } finally {
      setPendingFault(undefined);
    }
  };

  const userMessages = messages.filter((m) => m.role === "user").length;

  return (
    <div className="flex h-full flex-col">
      <Header connected={connected} />
      <div className="flex border-b border-ink-800 lg:hidden">
        {(["chat", "car"] as const).map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTab(t)}
            className={`flex flex-1 items-center justify-center gap-1.5 py-2.5 text-sm ${tab === t ? "border-b-2 border-peykan text-white" : "text-ink-400"}`}
          >
            {t === "chat" ? <ChatCircleDotsIcon size={16} /> : <CarProfileIcon size={16} />}
            {t === "chat" ? "Chat" : "Car"}
            {t === "car" && activeFault && <span className="h-1.5 w-1.5 rounded-full bg-peykan" />}
          </button>
        ))}
      </div>
      <main className="grid min-h-0 flex-1 lg:grid-cols-[minmax(0,1fr)_420px]">
        <section className={`min-h-0 ${tab === "chat" ? "block" : "hidden"} lg:block`}>
          <Chat
            messages={messages}
            status={status}
            error={error}
            onSend={(text) => {
              setNudge(null);
              sendMessage({ text });
            }}
            onStop={stop}
            onNewChat={clearHistory}
            nudge={nudge}
            messagesLeft={Math.max(0, maxUserMessages - userMessages)}
          />
        </section>
        <aside className={`min-h-0 border-ink-800 bg-ink-950/40 lg:border-l ${tab === "car" ? "block" : "hidden"} lg:block`}>
          <VehiclePanel
            snapshot={vehicle.snapshot}
            status={vehicle.status}
            history={vehicle.history}
            vin={vin}
            activeFault={activeFault}
            pendingFault={pendingFault}
            onFault={onFault}
          />
        </aside>
      </main>
    </div>
  );
}

export default function App() {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/session")
      .then((r) => r.json() as Promise<SessionInfo>)
      .then(setSession)
      .catch(() => setLoadError("Couldn't reach peykan.ai. Please reload."));
  }, []);

  const onSession = useCallback((sessionId: string) => {
    setSession((s) => (s ? { ...s, sessionId } : s));
  }, []);

  if (loadError) return <div className="flex h-full items-center justify-center text-bad">{loadError}</div>;
  if (!session) {
    return (
      <div className="flex h-full items-center justify-center">
        <CircleNotchIcon size={24} className="animate-spin text-cyan-glow" />
      </div>
    );
  }
  if (!session.sessionId) {
    return (
      <div className="flex h-full flex-col">
        <Header connected={null} />
        <div className="min-h-0 flex-1">
          <Gate siteKey={session.turnstileSiteKey} onSession={onSession} />
        </div>
      </div>
    );
  }
  return <Workspace sessionId={session.sessionId} maxUserMessages={session.maxUserMessages} />;
}
