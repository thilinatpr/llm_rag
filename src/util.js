// Small helpers shared by auth.js and public.js.
export const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");

export function safeEqual(a, b) {
  if (a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}

export const json = (obj, status = 200, headers = {}) =>
  new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json", ...headers } });

export function getCookie(request, name) {
  for (const part of (request.headers.get("Cookie") || "").split(";")) {
    const [k, ...v] = part.trim().split("=");
    if (k === name) return v.join("=");
  }
  return null;
}

export async function readJson(request) {
  try {
    return await request.json();
  } catch {
    return {};
  }
}

// A short stable id for an address, so storage keys and counters never hold the address itself.
export async function emailId(email) {
  return hex(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(email))).slice(0, 16);
}
