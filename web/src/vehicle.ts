import { Container } from "@cloudflare/containers";

/**
 * One simulated vehicle per visitor: the Peykan Docker image (repo root
 * Dockerfile) running `peykan demo`, i.e. the ECU simulator and the MCP
 * server in one process. Addressed by the visitor's session id, so faults one
 * visitor injects never affect another.
 *
 * Nothing here is reachable from the internet directly: the chat agent talks
 * to it through this Durable Object (MCP over `/mcp`), and the Worker proxies
 * only the dashboard stream to the visitor's own browser.
 */
export class Vehicle extends Container<Env> {
  defaultPort = 6278;
  // Sleep (and stop billing) once nobody has used it for a while.
  sleepAfter = "10m";
  // The simulated vehicle never needs to reach the internet.
  enableInternet = false;
  // Readiness probe: Peykan answers /healthz once its DBC has loaded.
  pingEndpoint = "container/healthz";

  envVars = {
    PEYKAN_MCP_TRANSPORT: "streamable-http",
    PEYKAN_MCP_HOST: "0.0.0.0",
    PEYKAN_MCP_PORT: "6278",
    // An empty directory: visitors can only analyse the bundled sample log.
    PEYKAN_LOG_DIR: "/tmp/peykan-logs",
    PEYKAN_LOG_LEVEL: "WARNING"
  };
}

/**
 * How to send HTTP requests to a visitor's vehicle.
 *
 * Normally that's the session's container. Local development on Windows
 * can't run containers (`dev.enable_containers` is off), so when
 * VEHICLE_DEV_URL is set in .dev.vars (e.g. http://127.0.0.1:6401) every
 * request goes to a Peykan you started yourself:
 *   peykan demo --transport streamable-http --port 6401
 * All sessions then share that one vehicle, which is fine for development.
 */
export function vehicleFetcher(env: Env, sessionId: string): (request: Request) => Promise<Response> {
  const devUrl = (env as { VEHICLE_DEV_URL?: string }).VEHICLE_DEV_URL;
  if (devUrl) {
    const base = new URL(devUrl);
    return (request) => {
      const url = new URL(request.url);
      url.protocol = base.protocol;
      url.host = base.host;
      return fetch(new Request(url, request));
    };
  }
  const stub = env.VEHICLE.getByName(sessionId);
  return (request) => stub.fetch(request);
}
