// Hourly check of the DeepSeek credit (cron in wrangler.toml), because running out fails real visitors' questions: 2026-11-06 20:38 UTC.
// Answers now fall back to Workers AI, so running out is no longer an outage, but the fallback's answers measured worse (AGENTS.md).
// One email a day at most while the credit is low. A failed check only mails after 3 in a row, since a single failure says little.
import { sendMessage } from "./auth.js";

const BALANCE_URL = "https://api.deepseek.com/user/balance";
const DAY = 24 * 3600;
const FAILURES_BEFORE_MAIL = 3;

async function notify(env, key, subject, text, send) {
  if (await env.AUTH.get(key)) return false;
  await send(env, { from: env.FROM_ADDRESS, to: env.ALERT_EMAIL, subject, text });
  await env.AUTH.put(key, "1", { expirationTtl: DAY });
  return true;
}

// -> {state: "off" | "ok" | "low" | "check_failed", ...}, also written to the Worker log.
export async function checkBalance(env, { fetchFn = globalThis.fetch, send = sendMessage, nowIso = () => new Date().toISOString() } = {}) {
  if (!env.DEEPSEEK_API_KEY || !env.ALERT_EMAIL) return { state: "off" };
  const limit = Number(env.BALANCE_ALERT_USD ?? 5);
  let out;
  try {
    const res = await fetchFn(BALANCE_URL, { headers: { Authorization: `Bearer ${env.DEEPSEEK_API_KEY}` } });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const d = await res.json();
    const infos = d.balance_infos || [];
    const info = infos.find((b) => b.currency === "USD") || infos[0];
    const balance = Number(info?.total_balance);
    if (!Number.isFinite(balance)) throw new Error("no balance in the reply");
    await env.AUTH.delete("alert:balance-failures");
    const low = d.is_available === false || balance < limit;
    out = { state: low ? "low" : "ok", balance, currency: info.currency, limit };
    if (low)
      out.mailed = await notify(env, "alert:balance-low", `DeepSeek credit is low: ${balance} ${info.currency}`,
        `The DeepSeek credit is ${balance} ${info.currency} (alert below ${limit}).\n\n` +
        "When it runs out, Bill Assistant and Procurement Rules answer with the backup model, Workers AI gpt-oss-120b, which scored lower in the eval.\n\n" +
        "Top up: https://platform.deepseek.com/top_up\n", send);
  } catch (e) {
    const failures = Number((await env.AUTH.get("alert:balance-failures")) || 0) + 1;
    await env.AUTH.put("alert:balance-failures", String(failures), { expirationTtl: DAY });
    out = { state: "check_failed", failures, error: String((e && e.message) || e) };
    if (failures >= FAILURES_BEFORE_MAIL) {
      try {
        out.mailed = await notify(env, "alert:balance-check", "DeepSeek credit check is failing",
          `The hourly DeepSeek credit check has failed ${failures} times in a row. Last error: ${out.error}\n\nThe API key may have been revoked.\n`, send);
      } catch (mailError) {
        out.mailError = String((mailError && mailError.message) || mailError);
      }
    }
  }
  console.log(JSON.stringify({ event: "balance_check", ...out }));
  // The admin panel reads the last result from here, so it never spends a call to DeepSeek on a page view.
  const { mailed: _mailed, mailError: _mailError, ...last } = out;
  let lowSince = null;
  if (out.state === "low") lowSince = JSON.parse((await env.AUTH.get("admin:balance")) || "{}").lowSince || nowIso();
  await env.AUTH.put("admin:balance", JSON.stringify({ ...last, at: nowIso(), lowSince }));
  return out;
}
