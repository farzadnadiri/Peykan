import { AIChatAgent, type OnChatMessageOptions } from "@cloudflare/ai-chat";
import { callable } from "agents";
import {
  convertToModelMessages,
  createUIMessageStream,
  createUIMessageStreamResponse,
  dynamicTool,
  jsonSchema,
  pruneMessages,
  stepCountIs,
  streamText,
  type ToolSet
} from "ai";
import { createWorkersAI } from "workers-ai-provider";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { CfWorkerJsonSchemaValidator } from "@modelcontextprotocol/sdk/validation/cfworker";
import { vehicleFetcher } from "./vehicle";

export const FAULT_PRESETS = [
  "overheat",
  "abs_fault",
  "low_fuel",
  "crash",
  "door_ajar",
  "misfire",
  "battery_low"
] as const;

// Tool results go back into the model's context on every step; a 30 s frame
// dump would cost thousands of tokens for no benefit, so long lists are cut.
const MAX_LIST_ITEMS = 40;
const MAX_RESULT_CHARS = 12_000;

const SYSTEM_PROMPT = `You are Peykan, an automotive diagnostics assistant on peykan.ai.
You are connected to the visitor's own simulated car through Peykan's tools: live CAN signals (11-bit DBC and SAE J1939), OBD-II, UDS over ISO-TP, trouble codes, fault injection and a recorded sample drive log.
The visitor sees a live panel of the car next to this chat, with gauges and buttons that inject faults.

How to work:
- Use the tools to get facts; never invent signal values, codes or a VIN.
- For "what is X right now", call get_vehicle_snapshot first.
- For "anything wrong?", check the snapshot and trouble codes (OBD service 3, uds_read_dtcs, read_j1939_dtcs) before answering.
- Explain findings in plain language for a curious non-expert, with the key numbers and units. Keep answers short; use a short list when it helps.
- If a tool returns status "blocked" or "error", say so plainly instead of retrying in a loop.
- This is a simulator for learning and demos, not a real vehicle; say so if someone asks for real repair or safety advice.`;

type VehicleConnection = { client: Client; tools: ToolSet; instructions: string };

function compact(value: unknown): unknown {
  const trimmed = JSON.parse(JSON.stringify(value), (_key, v) =>
    Array.isArray(v) && v.length > MAX_LIST_ITEMS
      ? [...v.slice(0, MAX_LIST_ITEMS), `... ${v.length - MAX_LIST_ITEMS} more items omitted`]
      : v
  );
  const text = JSON.stringify(trimmed);
  return text.length > MAX_RESULT_CHARS ? `${text.slice(0, MAX_RESULT_CHARS)} ... (truncated)` : trimmed;
}

function textReply(text: string): Response {
  const stream = createUIMessageStream({
    execute: ({ writer }) => {
      const id = crypto.randomUUID();
      writer.write({ type: "text-start", id });
      writer.write({ type: "text-delta", id, delta: text });
      writer.write({ type: "text-end", id });
    }
  });
  return createUIMessageStreamResponse({ stream });
}

export class ChatAgent extends AIChatAgent<Env> {
  maxPersistedMessages = 120;

  private vehicle?: Promise<VehicleConnection>;

  /** MCP connection to this visitor's vehicle container, created on first use. */
  private connectVehicle(): Promise<VehicleConnection> {
    this.vehicle ??= (async () => {
      const send = vehicleFetcher(this.env, this.name);
      // The MCP client speaks HTTP to a fixed internal URL; every request is
      // routed through the container's Durable Object, never the internet.
      const transport = new StreamableHTTPClientTransport(new URL("http://vehicle/mcp"), {
        fetch: (input, init) => send(new Request(String(input), init))
      });
      // The default (Ajv) schema validator generates code with `new Function`,
      // which Workers forbid; this one interprets schemas instead.
      const client = new Client(
        { name: "peykan-web", version: "1.0.0" },
        { jsonSchemaValidator: new CfWorkerJsonSchemaValidator() }
      );
      await client.connect(transport);
      const { tools: mcpTools } = await client.listTools();
      const tools: ToolSet = {};
      for (const t of mcpTools) {
        tools[t.name] = dynamicTool({
          description: t.description ?? t.name,
          inputSchema: jsonSchema(t.inputSchema as Parameters<typeof jsonSchema>[0]),
          execute: async (args) => compact(await this.callVehicleTool(t.name, args))
        });
      }
      return { client, tools, instructions: client.getInstructions() ?? "" };
    })();
    // A failed attempt (e.g. the container is still starting) must not stick.
    this.vehicle.catch(() => (this.vehicle = undefined));
    return this.vehicle;
  }

  /** Call a Peykan tool, reconnecting once if the container restarted (the
   *  old MCP session is gone after it sleeps). */
  private async callVehicleTool(name: string, args: unknown): Promise<unknown> {
    for (let attempt = 0; ; attempt++) {
      const { client } = await this.connectVehicle();
      try {
        const result = await client.callTool({ name, arguments: (args ?? {}) as Record<string, unknown> });
        if (result.structuredContent) return result.structuredContent;
        const content = (result.content ?? []) as Array<{ type: string; text?: string }>;
        return content.map((c) => (c.type === "text" ? c.text : `[${c.type}]`)).join("\n");
      } catch (error) {
        this.vehicle = undefined;
        if (attempt >= 1) throw error;
      }
    }
  }

  @callable()
  async injectFault(preset: string | null) {
    if (preset !== null && !(FAULT_PRESETS as readonly string[]).includes(preset)) {
      return { status: "error", message: `Unknown fault "${preset}"` };
    }
    return this.callVehicleTool("activate_fault_scenario", { preset });
  }

  @callable()
  async readVin() {
    return this.callVehicleTool("read_vin", {});
  }

  async onChatMessage(_onFinish: unknown, options?: OnChatMessageOptions) {
    const maxMessages = Number(this.env.MAX_USER_MESSAGES) || 40;
    const userMessages = this.messages.filter((m) => m.role === "user").length;
    if (userMessages > maxMessages) {
      return textReply(
        `This demo session has reached its limit of ${maxMessages} messages. Start a new session to keep going, or run Peykan yourself: \`pip install peykan\`, then \`peykan demo\`.`
      );
    }
    const { success } = await this.env.MESSAGE_LIMITER.limit({ key: this.name });
    if (!success) {
      return textReply("You're sending messages faster than this demo allows. Please wait a moment and try again.");
    }

    let vehicle: VehicleConnection;
    try {
      vehicle = await this.connectVehicle();
    } catch (error) {
      console.error("vehicle connection failed", error);
      return textReply(
        "I couldn't reach your simulated car just now. It may still be starting, or the demo is at capacity. Please try again in a few seconds."
      );
    }

    const workersai = createWorkersAI({ binding: this.env.AI });
    const result = streamText({
      model: workersai(this.env.CHAT_MODEL as Parameters<typeof workersai>[0], {
        sessionAffinity: this.sessionAffinity
      }),
      system: `${SYSTEM_PROMPT}\n\nTool guide from the server:\n${vehicle.instructions}`,
      messages: pruneMessages({
        messages: await convertToModelMessages(this.messages),
        toolCalls: "before-last-2-messages",
        reasoning: "before-last-message"
      }),
      tools: vehicle.tools,
      stopWhen: stepCountIs(8),
      // Cost ceiling per reply (reasoning tokens count too).
      maxOutputTokens: 2000,
      abortSignal: options?.abortSignal
    });
    return result.toUIMessageStreamResponse();
  }
}
