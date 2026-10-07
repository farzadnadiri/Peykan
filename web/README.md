# peykan.ai

The live demo: chat with an AI agent connected to your own simulated car. Each visitor gets a private Peykan vehicle in a Cloudflare Container and a chat agent running `gpt-oss-120b` on Workers AI, with a live dashboard of the car next to the chat.

```
Browser (React)
 ├─ /api/session          Turnstile check → signed session cookie
 ├─ /agents/chat-agent/…  ChatAgent (Durable Object, one per session)
 │                          └─ MCP over the Vehicle's Durable Object (never the internet)
 └─ /api/vehicle/stream   live signals, proxied from the visitor's own Vehicle
                            Vehicle = Container running `peykan demo` (repo-root Dockerfile)
```

| File | What it is |
|---|---|
| `src/server.ts` | Worker entry: sessions, access checks, live-stream proxy |
| `src/agent.ts` | `ChatAgent`: model, system prompt, Peykan tools over MCP, limits |
| `src/vehicle.ts` | `Vehicle`: the per-visitor Peykan container (no internet access, sleeps after 10 min idle) |
| `src/session.ts` | HMAC-signed session cookies |
| `src/app.tsx`, `src/components/` | The UI |
| `wrangler.jsonc` | Bindings, container config, rate limits, model choice |

## Limits and protection

- **Turnstile** before a session starts (verified server-side), sessions last 2 hours.
- **Rate limits:** 5 new sessions per minute per IP; 10 chat messages per minute per session.
- **Caps:** 40 user messages per session (`MAX_USER_MESSAGES`), 8 tool steps and 2000 output tokens per reply, 25 concurrent vehicles (`max_instances`).
- A visitor can only open the chat agent and vehicle named by their own signed cookie.
- Vehicles can only read the bundled sample log, and have no outbound internet.

## Requirements

- Node 22+ and npm
- A Cloudflare account on the **Workers Paid** plan (Containers), with `peykan.ai` as a zone
- **Docker running locally** for `wrangler deploy` and for local development (it builds the vehicle image)

## Local development

```bash
cd web
npm install
cp .dev.vars.example .dev.vars        # Turnstile test secret + a session secret
npx wrangler login                    # Workers AI runs remotely even in dev
npm run dev                           # http://localhost:5173
```

`.dev.vars.example` uses Cloudflare's always-pass Turnstile test keys, so the check succeeds locally.

## First deployment

1. **Turnstile widget.** In the Cloudflare dashboard → Turnstile → Add widget: mode **Managed**, hostnames `peykan.ai` and `localhost`. Put the **site key** in `wrangler.jsonc` → `vars.TURNSTILE_SITE_KEY` (it's public).
2. **Secrets** (never in files):
   ```bash
   npx wrangler secret put TURNSTILE_SECRET_KEY     # the widget's secret key
   npx wrangler secret put SESSION_SECRET           # any long random string
   ```
   PowerShell: `[guid]::NewGuid().ToString() + [guid]::NewGuid().ToString()` makes a usable `SESSION_SECRET`.
3. **Deploy** (Docker must be running):
   ```bash
   npm run deploy
   ```
   The first container deploy takes a few minutes to become available. The custom domain route in `wrangler.jsonc` attaches the Worker to `peykan.ai`.

Later deploys: `npm run deploy` again. The vehicle image rebuilds from the repo's `Dockerfile`, so the site runs whatever Peykan version is checked out.

## Changing the model

`vars.CHAT_MODEL` in `wrangler.jsonc` selects the Workers AI model; any model with tool calling works, e.g. `@cf/meta/llama-4-scout-17b-16e-instruct`. Redeploy after changing it.

## Costs (rough)

- Workers Paid: $5/month, which includes about 375 vCPU-minutes and 25 GiB-hours of container time: around 150 ten-minute visits on the `basic` instance. After that, roughly $0.50 per 100 visits.
- `gpt-oss-120b`: $0.35 / M input and $0.75 / M output tokens; a chat turn with tools is typically around $0.01.
- Watch usage in the Cloudflare dashboard (Workers AI and Containers), and lower `max_instances` / `MAX_USER_MESSAGES` if needed.
