// Entrypoint. workerd accepts only the default handler and the Durable Object class as exports (see AGENTS.md runbook).
import { Container, getContainer } from "@cloudflare/containers";
import { createProcurementHandler } from "./handler.js";

const PORT = 8000;

export class ProcurementContainer extends Container {
  defaultPort = PORT;
  sleepAfter = "15m";

  constructor(ctx, env) {
    super(ctx, env);
    this.envVars = {
      PROCUREMENT_PROXY_SECRET: env.PROXY_SECRET,
      PROCUREMENT_RERANK_URL: env.RERANK_URL,
      BILLRAG_CHAT_PROVIDER: "openai",
      BILLRAG_CHAT_MODEL: env.CHAT_MODEL,
      BILLRAG_CHAT_BASE_URL: env.CHAT_BASE_URL,
      BILLRAG_CHAT_API_KEY: env.DEEPSEEK_API_KEY,
      BILLRAG_RERANK: "none", // the server uses the Worker's reranker instead
      BILLRAG_EMBED_PROVIDER: "none",
      BILLRAG_CHATLOG: "0",
    };
  }

  async fetch(request) {
    if (!this.ctx.container.running) {
      await this.startAndWaitForPorts({ ports: [PORT], cancellationOptions: { portReadyTimeoutMS: 180_000 } });
    }
    return super.fetch(request);
  }
}

const handle = createProcurementHandler((env) => getContainer(env.BILLRAG));
export default { fetch: handle };
