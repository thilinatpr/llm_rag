// Which chat model the container uses, and the environment it gets for it. Pure function so it can be tested without Cloudflare.
//
//   CHAT_PROVIDER (Worker var)   "workers-ai" (default) | "deepseek"
//   workers-ai : Cloudflare Workers AI, OpenAI-compatible endpoint, same token as embeddings/rerank (CHAT_MODEL, default gpt-oss-120b)
//   deepseek   : api.deepseek.com, model CHAT_MODEL (default deepseek-flash), key = the DEEPSEEK_API_KEY Worker secret.
//                Grading runs with thinking OFF (BILLRAG_GRADE_EFFORT=off): with thinking on the grader's JSON is unusable and the
//                pipeline falls back to retrieval order. The answer keeps the model's default thinking. Measured on the 72 recent
//                eval questions: strict 0.93 vs 0.89 for gpt-oss-120b, same 0 warnings, ~9 s vs ~16 s (AGENTS.md).
//   Translation: a TRANSLATE_MODEL that is a Workers AI id ("@cf/...") cannot run on another provider's endpoint, so it is dropped
//                for deepseek and the chat model translates. Embeddings, rerank and R2 always use the Cloudflare credentials.
//   A missing DEEPSEEK_API_KEY falls back to Workers AI (the site keeps working) and says so in the Worker log.
//   DeepSeek failing while answering (no credit, outage): the container repeats the call on Workers AI (billrag/llm.py FallbackLLM),
//   FALLBACK_MODEL or gpt-oss-120b. Needs CLOUDFLARE_API_TOKEN, which the container has anyway; without it there is no fallback.
export const DEEPSEEK_BASE_URL = "https://api.deepseek.com";
const DEFAULT_WORKERS_AI_MODEL = "@cf/openai/gpt-oss-120b";
const workersAiUrl = (env) => `https://api.cloudflare.com/client/v4/accounts/${env.CF_ACCOUNT_ID}/ai/v1`;

export function chatEnv(env) {
  const wanted = String(env.CHAT_PROVIDER || "workers-ai").toLowerCase();
  if (wanted === "deepseek" && env.DEEPSEEK_API_KEY) {
    const model = env.CHAT_MODEL && !env.CHAT_MODEL.startsWith("@cf/") ? env.CHAT_MODEL : "deepseek-flash";
    const tm = env.TRANSLATE_MODEL && !env.TRANSLATE_MODEL.startsWith("@cf/") ? env.TRANSLATE_MODEL : undefined;
    return {
      provider: "deepseek",
      vars: {
        BILLRAG_CHAT_PROVIDER: "openai",
        BILLRAG_CHAT_MODEL: model,
        BILLRAG_CHAT_BASE_URL: DEEPSEEK_BASE_URL,
        BILLRAG_CHAT_API_KEY: env.DEEPSEEK_API_KEY,
        BILLRAG_GRADE_EFFORT: "off",
        BILLRAG_TRANSLATE_MODEL: tm,
        ...(env.CLOUDFLARE_API_TOKEN && {
          BILLRAG_FALLBACK_MODEL: env.FALLBACK_MODEL || DEFAULT_WORKERS_AI_MODEL,
          BILLRAG_FALLBACK_BASE_URL: workersAiUrl(env),
          BILLRAG_FALLBACK_API_KEY: env.CLOUDFLARE_API_TOKEN,
        }),
      },
    };
  }
  if (wanted === "deepseek") console.warn("CHAT_PROVIDER=deepseek but the DEEPSEEK_API_KEY secret is missing: using Workers AI");
  return {
    provider: "workers-ai",
    vars: {
      BILLRAG_CHAT_PROVIDER: "openai",
      BILLRAG_CHAT_MODEL: env.CHAT_MODEL || DEFAULT_WORKERS_AI_MODEL,
      BILLRAG_CHAT_BASE_URL: workersAiUrl(env),
      BILLRAG_CHAT_API_KEY: env.CLOUDFLARE_API_TOKEN,
      BILLRAG_TRANSLATE_MODEL: env.TRANSLATE_MODEL,
    },
  };
}
