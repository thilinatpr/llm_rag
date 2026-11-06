// The Python chat UI (`python -m billrag serve`) running in a Cloudflare Container (instance type "lite").
// Only the login Worker can reach it: every request carries X-Proxy-Secret, which the server checks.
import { Container } from "@cloudflare/containers";
import { chatEnv } from "./chatconfig.js";

const PORT = 8000;
// First start downloads ~260 MB from R2 before the server listens, so allow far longer than the 8s default.
const PORT_READY_TIMEOUT_MS = 300_000;

export class BillragContainer extends Container {
  defaultPort = PORT;
  sleepAfter = "15m"; // stop (and stop paying for memory) after 15 idle minutes

  constructor(ctx, env) {
    super(ctx, env);
    const pass = {
      R2_ACCOUNT_ID: env.CF_ACCOUNT_ID,
      R2_BUCKET: env.R2_BUCKET,
      R2_ACCESS_KEY_ID: env.R2_ACCESS_KEY_ID,
      R2_SECRET_ACCESS_KEY: env.R2_SECRET_ACCESS_KEY,
      CLOUDFLARE_API_TOKEN: env.CLOUDFLARE_API_TOKEN, // Workers AI: query embeddings + rerank
      BILLRAG_CF_ACCOUNT_ID: env.CF_ACCOUNT_ID,
      // Chat model: Workers AI (default) or DeepSeek, chosen by the CHAT_PROVIDER var (see chatconfig.js).
      ...chatEnv(env).vars,
      BILLRAG_PROXY_SECRET: env.PROXY_SECRET,
      // Translation layer (billrag/translate.py): off | script | ai. Unset -> the Python default ("script").
      BILLRAG_TRANSLATE: env.TRANSLATE,
      // Bill-focused follow-ups from the whole bill text (pipeline.py, issue #37): "1" turns it on. Unset -> off.
      BILLRAG_WHOLE_BILL: env.WHOLE_BILL,
      // Optional model for the translate calls only (BILLRAG_TRANSLATE_MODEL) comes from chatEnv: a Workers AI id only with Workers AI.
    };
    this.envVars = Object.fromEntries(Object.entries(pass).filter(([, v]) => v));
  }

  async fetch(request) {
    if (!this.ctx.container.running) {
      await this.startAndWaitForPorts({ ports: [PORT], cancellationOptions: { portReadyTimeoutMS: PORT_READY_TIMEOUT_MS } });
    }
    return super.fetch(request);
  }
}
