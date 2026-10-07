import { useEffect, useRef, useState } from "react";
import { CircleNotchIcon, ShieldCheckIcon } from "@phosphor-icons/react";

declare global {
  interface Window {
    turnstile?: {
      render: (el: HTMLElement, options: Record<string, unknown>) => string;
      remove: (widgetId: string) => void;
      reset: (widgetId: string) => void;
    };
  }
}

const SCRIPT_SRC = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";

function loadTurnstile(): Promise<void> {
  if (window.turnstile) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = SCRIPT_SRC;
    script.async = true;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("Could not load the verification script"));
    document.head.appendChild(script);
  });
}

/** Bot check before a session starts: each session runs a vehicle container
 *  and spends LLM tokens. The token is verified by our Worker, never here. */
export function Gate({ siteKey, onSession }: { siteKey: string; onSession: (sessionId: string) => void }) {
  const box = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [verifying, setVerifying] = useState(false);

  useEffect(() => {
    let widgetId: string | undefined;
    let cancelled = false;
    loadTurnstile()
      .then(() => {
        if (cancelled || !box.current || !window.turnstile) return;
        widgetId = window.turnstile.render(box.current, {
          sitekey: siteKey,
          action: "turnstile-spin-v1",
          theme: "dark",
          callback: async (token: string) => {
            setVerifying(true);
            setError(null);
            const response = await fetch("/api/session", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ token })
            });
            const data = (await response.json()) as { sessionId?: string; error?: string };
            if (response.ok && data.sessionId) {
              onSession(data.sessionId);
            } else {
              setVerifying(false);
              setError(data.error ?? "Verification failed.");
              if (widgetId) window.turnstile?.reset(widgetId);
            }
          },
          "error-callback": () => setError("Verification couldn't run. Please reload the page.")
        });
      })
      .catch((e: Error) => setError(e.message));
    return () => {
      cancelled = true;
      if (widgetId) window.turnstile?.remove(widgetId);
    };
  }, [siteKey, onSession]);

  return (
    <div className="flex h-full items-center justify-center p-4">
      <div className="w-full max-w-md rounded-3xl border border-ink-700 bg-ink-900/80 p-6 text-center shadow-2xl backdrop-blur">
        <img src="/peykan-banner.jpg" alt="Peykan: connecting AI agents to vehicle systems" className="mb-5 w-full rounded-2xl" />
        <h1 className="text-xl font-semibold text-white">Start your test drive</h1>
        <p className="mt-2 text-sm text-ink-300">
          You'll get a private simulated car and an AI agent that can read it over CAN, OBD-II, UDS and J1939. Sessions last up to two hours.
        </p>
        <div className="mt-5 flex min-h-[70px] items-center justify-center">
          {verifying ? (
            <span className="inline-flex items-center gap-2 text-sm text-ink-300">
              <CircleNotchIcon size={16} className="animate-spin text-cyan-glow" /> Preparing your session…
            </span>
          ) : (
            <div ref={box} />
          )}
        </div>
        {error && <p className="mt-3 text-sm text-bad">{error}</p>}
        <p className="mt-4 inline-flex items-center gap-1.5 text-xs text-ink-400">
          <ShieldCheckIcon size={14} /> Protected by Cloudflare Turnstile
        </p>
      </div>
    </div>
  );
}
