import { routeAgentRequest } from "agents";
import { issueSession, readSession } from "./session";
import { vehicleFetcher } from "./vehicle";

export { ChatAgent } from "./agent";
export { Vehicle } from "./vehicle";

const SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify";

function json(data: unknown, init: ResponseInit = {}): Response {
  return Response.json(data, {
    ...init,
    headers: { "Cache-Control": "no-store", ...(init.headers ?? {}) }
  });
}

async function verifyTurnstile(env: Env, token: string, ip: string | null): Promise<boolean> {
  const form = new FormData();
  form.append("secret", env.TURNSTILE_SECRET_KEY);
  form.append("response", token);
  if (ip) form.append("remoteip", ip);
  form.append("idempotency_key", crypto.randomUUID());
  const response = await fetch(SITEVERIFY_URL, { method: "POST", body: form });
  if (!response.ok) {
    console.warn("turnstile siteverify HTTP error", response.status);
    return false;
  }
  const outcome = (await response.json()) as {
    success?: boolean;
    "error-codes"?: string[];
    hostname?: string;
  };
  if (outcome.success !== true) {
    // e.g. invalid-input-secret (wrong TURNSTILE_SECRET_KEY),
    // invalid-input-response (token from another widget, or expired),
    // timeout-or-duplicate. Visible with `npx wrangler tail`.
    console.warn("turnstile verification failed", outcome["error-codes"], outcome.hostname);
  }
  return outcome.success === true;
}

/** GET: the current session (if any) and the Turnstile sitekey.
 *  POST {token}: verify Turnstile, then start a session (sets the cookie). */
async function handleSession(request: Request, env: Env): Promise<Response> {
  if (request.method === "GET") {
    const sessionId = await readSession(request, env.SESSION_SECRET);
    return json({
      sessionId,
      turnstileSiteKey: env.TURNSTILE_SITE_KEY,
      maxUserMessages: Number(env.MAX_USER_MESSAGES) || 40
    });
  }
  if (request.method !== "POST") return new Response("Method not allowed", { status: 405 });

  const ip = request.headers.get("CF-Connecting-IP");
  const { success } = await env.SESSION_LIMITER.limit({ key: ip ?? "unknown" });
  if (!success) return json({ error: "Too many new sessions; try again in a minute." }, { status: 429 });

  const { token } = (await request.json().catch(() => ({}))) as { token?: string };
  if (!token || !(await verifyTurnstile(env, token, ip))) {
    return json({ error: "Verification failed; please reload and try again." }, { status: 403 });
  }
  const session = await issueSession(env.SESSION_SECRET);
  return json({ sessionId: session.id }, { headers: { "Set-Cookie": session.cookie } });
}

/** Live signal stream for the visitor's own vehicle (the dashboard's SSE feed). */
async function handleVehicleStream(request: Request, env: Env): Promise<Response> {
  const sessionId = await readSession(request, env.SESSION_SECRET);
  if (!sessionId) return new Response("No session", { status: 401 });
  const upstream = await vehicleFetcher(env, sessionId)(
    new Request("http://vehicle/dashboard/stream", { signal: request.signal })
  );
  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive"
    }
  });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/api/session") return handleSession(request, env);
    if (url.pathname === "/api/vehicle/stream") return handleVehicleStream(request, env);

    if (url.pathname.startsWith("/agents/")) {
      // /agents/chat-agent/<session id>: only the visitor holding that
      // session's cookie may open (or call into) its chat agent.
      const sessionId = await readSession(request, env.SESSION_SECRET);
      const instance = url.pathname.split("/")[3];
      if (!sessionId || instance !== sessionId) {
        return new Response("Forbidden", { status: 403 });
      }
      return (await routeAgentRequest(request, env)) ?? new Response("Not found", { status: 404 });
    }

    return new Response("Not found", { status: 404 });
  }
} satisfies ExportedHandler<Env>;
