// The privacy page of each service, at /privacy, readable without signing in. Every sentence states what the code does today: change it when the code
// changes (what is logged: billrag/chatlog.py, workers/procurement/handler.js; cookies and limits: auth.js, public.js; providers: chatconfig.js).
// PRIVACY_CONTACT (Worker var): where to write. Without it the page leaves that sentence out. Not reviewed by a lawyer.
const UPDATED = "9 November 2026";

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const list = (items) => `<ul>${items.map((i) => `<li>${i}</li>`).join("")}</ul>`;

const PROVIDERS = (extra = []) => list([
  "<b>DeepSeek</b>, an AI company based in China, receives your question and the passages used to answer it, and writes the answer. Its own terms apply to what it does with them.",
  "<b>Cloudflare</b> runs this service and stores its logs, sees your IP address as it does for any site it hosts, sends the sign-in email, and handles your question when searching. If DeepSeek is unavailable, Cloudflare's AI service writes the answer instead.",
  "<b>Google Fonts</b> supplies the page's typefaces, so Google sees your IP address when the page loads.",
  ...extra,
]);

const SERVICES = {
  bills: {
    name: "Bill Assistant",
    keep: [
      "<b>Your questions and the answers.</b> Each is saved in a log with its time, the bills it cited, how long it took and what it cost. The record carries a random chat ID, not your name, email or IP address. If a question contains something that identifies you, the log contains it too.",
      "<b>If you use it without signing in:</b> a cookie with a random ID (30 days) counts your free questions. Your IP address is used for a daily count that stops one network taking every free question; that count is deleted within a day or so.",
      "<b>If you sign in:</b> your email address is used to send a one-time code and is kept with your session for 7 days (the session cookie works on every citizenlk.com address). The code is stored only in scrambled form, for 5 minutes. The list of invited addresses is kept for as long as you have access.",
      "<b>If you are signed in, your chat history:</b> each chat (your questions and the answers) is saved with your account, so you can read it again. It is kept for 30 days after the last question in that chat, then removed, and you can delete one chat or all of them under History on the chat page. Deleting history does not remove the separate question log above, which does not know who you are.",
      "<b>If you create a free account:</b> the number of questions you have asked today and this month is counted against a scrambled form of your email address, and kept for up to a month. Requests for sign-in codes are counted per network and per address for a day.",
      "<b>Your agreement:</b> when you accept the statements shown after your first sign-in, we keep the date and version of the wording with your email address. One account works on all citizenlk services, so the same sign-in and the same record are used on each. They are kept for as long as you have access.",
      "<b>In your browser:</b> a random chat ID, your Thorough or Fast choice, and the list of latest bills. These stay on your device.",
    ],
    others: [
      "<b>Cloudflare Turnstile</b> checks that a visitor without an account is a person. It reads information from your browser to do so.",
    ],
  },
  procurement: {
    name: "Procurement Rules",
    keep: [
      "<b>Your questions and the answers.</b> Each is saved in a log with its time, the passages it used, how long it took and what it cost. The record does not carry your name, email or IP address. If a question contains something that identifies you, the log contains it too. A copy of each question and answer also goes to the service's system logs at Cloudflare.",
      "<b>Your sign-in:</b> your email address is used to send a one-time code and is kept with your session for 7 days (the session cookie works on every citizenlk.com address). The code is stored only in scrambled form, for 5 minutes. The list of invited addresses is kept for as long as you have access.",
      "<b>Your agreement:</b> when you accept the statements shown after your first sign-in, we keep the date and version of the wording with your email address. One account works on all citizenlk services, so the same sign-in and the same record are used on each. They are kept for as long as you have access.",
      "<b>Your conversation:</b> the page holds your last questions and answers while it is open, and sends the last two with a new question so that a follow-up can be understood. The service does not keep a conversation between questions, apart from the log above.",
    ],
    others: [],
  },
};

export function privacyPage(service, contact) {
  const s = SERVICES[service];
  const write = contact ? ` To ask about this, or to have a question removed from the logs, write to <a href="mailto:${esc(contact)}">${esc(contact)}</a>; give the date and the wording, since the logs do not know who you are.` : "";
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Privacy - ${s.name}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Abhaya+Libre:wght@800&family=Yaldevi:wght@400;500&display=swap">
<style>
  :root { --lime:#F4F2EE; --ink:#1C1511; --muted:#6E6157; --line:#DAD3CA; --sindoor:#A3241B; --ochre:#E0A93A; color-scheme: light dark; }
  @media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --lime:#15100D; --ink:#F1E6D6; --muted:#B3A391; --line:#3A2D24; --sindoor:#E0675A; --ochre:#E8B550; } }
  :root[data-theme="dark"] { --lime:#15100D; --ink:#F1E6D6; --muted:#B3A391; --line:#3A2D24; --sindoor:#E0675A; --ochre:#E8B550; }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--lime); color: var(--ink); font: 400 1rem/1.6 Yaldevi, "Segoe UI", system-ui, sans-serif; }
  main { max-width: 700px; margin: 0 auto; padding: 32px max(16px, env(safe-area-inset-right, 0px)) 48px max(16px, env(safe-area-inset-left, 0px)); overflow-wrap: anywhere; }
  .mark { font: 800 1.5rem/1 "Abhaya Libre", Georgia, serif; color: var(--ink); text-decoration: none; }
  .mark b { color: var(--sindoor); font-weight: 800; }
  h1 { font: 800 2rem/1.2 "Abhaya Libre", Georgia, serif; margin: 24px 0 4px; }
  h2 { font-size: 1.05rem; font-weight: 500; margin: 28px 0 6px; }
  p { margin: 0 0 10px; } ul { margin: 0; padding-left: 20px; } li { margin-bottom: 8px; } b { font-weight: 500; }
  a { color: inherit; text-underline-offset: 2px; } a:focus-visible { outline: 2px solid var(--ochre); outline-offset: 2px; }
  .muted { color: var(--muted); font-size: .9rem; }
  footer { margin-top: 32px; padding-top: 12px; border-top: 1px solid var(--line); color: var(--muted); font-size: .84rem; }
</style></head><body><main>
<a class="mark" href="https://citizenlk.com">citizen<b>lk</b></a>
<h1>Privacy</h1>
<p class="muted">${s.name} · Last updated ${UPDATED}</p>
<p>${s.name} is an independent project, not a government service. This page says what it keeps and who else sees it.</p>
<h2>Please do not include personal details</h2>
<p>Ask about the rules, not about a person. Leave out names, ID numbers and the private details of a case, because your question is saved and sent to the companies listed below.</p>
<h2>What is kept</h2>
${list(s.keep)}
<h2>Who else sees it</h2>
${PROVIDERS(s.others)}
<h2>How it is used</h2>
<p>The people who run this service read the logs to find mistakes and improve the answers. There is no advertising and there are no tracking cookies.</p>
<h2>How long it is kept</h2>
<p>The logs of questions and answers have no fixed deletion date at the moment: they stay until they are deleted by hand. Sign-in details and daily counts expire as described above.</p>
<h2>Your choices</h2>
<p>You can stop using the service at any time, clear this site's data in your browser, and sign out.${write}</p>
<p><a href="/">Back to ${s.name}</a></p>
<footer>Not legal advice · Not a government service</footer>
</main></body></html>`;
}
