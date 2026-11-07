// Chat history for signed-in accounts (staff and free): the last 30 days, with delete. Not for anonymous visitors.
//
// Stored in the shared accounts KV (env.AUTH) under hist:<service>:<hash of address>:<chat id>, value = the chat, metadata = {title, updated, n}
// so the list is one KV list call. Every write sets a 30-day expiry (a chat that is added to stays 30 days after its last question), so
// "30 days" is enforced by the store, not by a job. Deleting removes the keys at once.
// The PAGE sends the chat after each answer (PUT): a person can only write or read their own keys, and size caps keep one account small.
// Not the question log: logs (R2) carry no account and are not touched by deleting history (privacy.js says so).
import { emailId, json, readJson } from "./util.js";

export const HISTORY_DAYS = 30;
const TTL = HISTORY_DAYS * 24 * 3600;
const MAX_CHATS = 50, MAX_TURNS = 60, MAX_Q = 1000, MAX_A = 20000, MAX_BYTES = 200_000;
const ID = /^[A-Za-z0-9_-]{6,40}$/;

const prefix = async (service, email) => `hist:${service}:${await emailId(email)}:`;
const clip = (s, n) => String(s ?? "").slice(0, n);

// Checks and shapes what the page sent. Returns null for anything that is not a chat.
export function cleanChat(body) {
  if (!body || !Array.isArray(body.turns) || !body.turns.length || body.turns.length > MAX_TURNS) return null;
  const turns = [];
  for (const t of body.turns) {
    if (!t || typeof t.q !== "string" || typeof t.a !== "string" || !t.q.trim()) return null;
    turns.push({ q: clip(t.q, MAX_Q), a: clip(t.a, MAX_A) });
  }
  const chat = { turns };
  return JSON.stringify(chat).length <= MAX_BYTES ? chat : null;
}

// /history (list, delete all) and /history/<id> (read, write, delete) for a signed-in account. url path is already known to start with /history.
export async function historyApi(env, request, email, service, nowMs) {
  const url = new URL(request.url), parts = url.pathname.split("/").filter(Boolean); // ["history"] or ["history", id]
  const mine = await prefix(service, email);
  if (request.method !== "GET") {
    // Cookies are SameSite=Lax already; this also refuses a write that names another site as its origin.
    const origin = request.headers.get("Origin");
    if (origin && origin !== url.origin) return json({ error: "Not allowed." }, 403);
  }
  const headers = { "Cache-Control": "no-store" };
  if (parts.length === 1) {
    if (request.method === "GET") {
      const list = await env.AUTH.list({ prefix: mine, limit: 1000 });
      const chats = list.keys.map((k) => ({ id: k.name.slice(mine.length), ...(k.metadata || {}) })).sort((a, b) => String(b.updated).localeCompare(String(a.updated)));
      return json({ days: HISTORY_DAYS, chats }, 200, headers);
    }
    if (request.method === "DELETE") {
      let n = 0, cursor;
      do { // a person's whole history, however many pages the list has
        const page = await env.AUTH.list({ prefix: mine, cursor });
        await Promise.all(page.keys.map((k) => env.AUTH.delete(k.name)));
        n += page.keys.length; cursor = page.list_complete ? undefined : page.cursor;
      } while (cursor);
      return json({ ok: true, deleted: n }, 200, headers);
    }
    return json({ error: "Not found." }, 404);
  }
  const id = parts[1];
  if (parts.length !== 2 || !ID.test(id)) return json({ error: "Not found." }, 404);
  const key = mine + id;
  if (request.method === "GET") {
    const raw = await env.AUTH.get(key);
    return raw ? new Response(raw, { headers: { "Content-Type": "application/json", ...headers } }) : json({ error: "Not found." }, 404);
  }
  if (request.method === "DELETE") { await env.AUTH.delete(key); return json({ ok: true }, 200, headers); }
  if (request.method === "PUT") {
    const chat = cleanChat(await readJson(request));
    if (!chat) return json({ error: "That chat could not be saved." }, 400);
    if (!(await env.AUTH.get(key, { type: "text" }))) { // a new chat: keep the number per account bounded
      const list = await env.AUTH.list({ prefix: mine, limit: MAX_CHATS + 1 });
      if (list.keys.length >= MAX_CHATS) return json({ error: `History holds ${MAX_CHATS} chats. Delete one to save a new chat.`, reason: "full" }, 409);
    }
    const meta = { title: clip(chat.turns[0].q, 80), updated: new Date(nowMs).toISOString(), n: chat.turns.length };
    await env.AUTH.put(key, JSON.stringify(chat), { expirationTtl: TTL, metadata: meta });
    return json({ ok: true }, 200, headers);
  }
  return json({ error: "Not found." }, 404);
}
