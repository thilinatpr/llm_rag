// Anonymous visitor counts, the technique LankaWatch uses (sri-lanka-ds-boundaries apps/web/src/events.js, ev.js).
// A small script on the page (EV_SCRIPT, inlined by `counted`) sends events to POST /e; `countEvents` checks them against the
// fixed lists in SCHEMA and writes each to Workers Analytics Engine (binding EVENTS, dataset DATASET). The admin panel reads them
// back (src/admin-visitors.js). Used by every Worker that serves a page people open: the homepage, Bill Assistant, Procurement Rules.
//
// What keeps this anonymous, and must stay true (test/events.test.mjs checks the parts it can):
// - No ID of any kind, no cookie, nothing written to the visitor's browser. `pv` is a random number for one page view, held in the
//   page's memory only.
// - The site and page come from the page token (an HMAC the Worker signed over host, path and hour when it served the page), never
//   from the browser. The browser sends only an event name and at most two values, each from the fixed lists below; anything else is
//   dropped. The one free-text exception is where a visit came from (`from`, `tag`): the link's utm_* text and the referring site's
//   host name, each a short [a-z0-9.-_] word that the script cleans (no paths, no e-mail addresses, no long numbers).
// - Nothing about the visitor is stored: not the IP, not the user agent (only used to drop bots), nothing typed, no screen positions.
// - Nothing is sent with Global Privacy Control or Do Not Track on, in an automated browser, or before a person has been seen.
export const DATASET = "citizenlk_events";

const SOURCES = ["facebook", "whatsapp", "viber", "telegram", "x", "instagram", "tiktok", "youtube", "linkedin", "reddit", "email", "qr", "ai", "share", "other"];
const MEDIUMS = ["", "social", "message", "paid", "email", "print", "other"];
const WORD = /^(?:[a-z0-9][a-z0-9._-]{0,59})?$/;
const HOST = /^(?:[a-z0-9][a-z0-9.-]{0,99})?$/;
export const GO = ["bills", "procurement", "admin", "other"]; // where a tap on a link to another citizenlk service went
export const SCHEMA = {
  view: { a: [""], b: [""] }, // a person was here (visible 2 s and touched the page)
  nav: { a: ["new", "reload", "back"], b: ["out", "in"] }, // how the page was opened; new + out = a visit starts
  src: { a: SOURCES, b: MEDIUMS }, // with a visit start, only when the address carries utm_source
  from: { a: WORD, b: HOST }, // with every visit start: utm_source as written ('' = none), the referring host ('' = none)
  tag: { a: WORD, b: WORD }, // with a tagged visit start: utm_medium, utm_campaign as written
  go: { a: GO, b: [""] },
  scroll: { a: ["25", "50", "75", "100"], b: [""] },
  skip: { a: ["noint", "webdriver"], b: [""] }, // the page saw no person (counted, so the panel can say how many)
};
export const SITES = { "citizenlk.com": "home", "bills.citizenlk.com": "bills", "procurement.citizenlk.com": "procurement" };
const PAGES = ["/", "/index.html"]; // the pages that carry the counter
export const MAX_EVENTS = 20, MAX_BYTES = 4096;
const PV = /^[a-z0-9]{8,12}$/;
const HOUR = 3600e3;

// Crawlers, link previews, monitors and scripts. A browser with a person in it never sends these.
const BOT_UA = /bot\b|bot\/|crawl|spider|slurp|headless|lighthouse|pagespeed|preview|facebookexternalhit|whatsapp|telegram|curl|wget|python|httpclient|okhttp|axios|node-fetch|undici|go-http|java\/|libwww|scrapy|phantom|selenium|playwright|puppeteer|pingdom|uptime|monitor/i;
export const isBotUA = (ua) => !ua || BOT_UA.test(ua);

export const siteOf = (host) => SITES[String(host || "").toLowerCase()] || null;
const keyOf = (env) => env.EV_KEY || env.OTP_PEPPER || "";

// Page token: HMAC over site, path and hour. The same for everyone who opens that page that hour, so it identifies nobody; it only
// proves the event came from a page we served in the last 3 hours.
const enc = new TextEncoder();
const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
async function mac(secret, msg) {
  const key = await crypto.subtle.importKey("raw", enc.encode(`ev|${secret}`), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return hex(await crypto.subtle.sign("HMAC", key, enc.encode(msg))).slice(0, 32);
}
const hourOf = (now) => Math.floor(now / HOUR);
export const signPage = (secret, site, path, now = Date.now()) => mac(secret, `${site}|${path}|${hourOf(now)}`);
export async function verifyPage(secret, site, path, token, now = Date.now()) {
  if (!secret || typeof token !== "string" || !/^[0-9a-f]{32}$/.test(token)) return false;
  for (let h = 0; h <= 2; h++) if ((await mac(secret, `${site}|${path}|${hourOf(now) - h}`)) === token) return true;
  return false;
}

// One event from the page -> { ev, a, b } or null. Only names and values from SCHEMA pass.
export function cleanEvent(e) {
  if (!e || typeof e !== "object" || !Object.hasOwn(SCHEMA, e.ev)) return null;
  const s = SCHEMA[e.ev];
  const a = e.a == null ? "" : String(e.a), b = e.b == null ? "" : String(e.b);
  const ok = (rule, v) => (rule instanceof RegExp ? rule.test(v) : rule.includes(v));
  return ok(s.a, a) && ok(s.b, b) ? { ev: e.ev, a, b } : null;
}

// The request body -> { ok, why, pv, events }. why: size | json | path | token | pv | none. `site` is the host's, not the browser's.
export async function readBatch(secret, site, text, now = Date.now()) {
  if (!text || text.length > MAX_BYTES) return { ok: false, why: "size" };
  let body;
  try { body = JSON.parse(text); } catch { return { ok: false, why: "json" }; }
  const { p, t, pv, e } = body || {};
  if (!PAGES.includes(p)) return { ok: false, why: "path" };
  if (!(await verifyPage(secret, site, p, t, now))) return { ok: false, why: "token" };
  if (!PV.test(pv || "")) return { ok: false, why: "pv" };
  const events = (Array.isArray(e) ? e.slice(0, MAX_EVENTS) : []).map(cleanEvent).filter(Boolean);
  return events.length ? { ok: true, pv, events } : { ok: false, why: "none" };
}

// Analytics Engine row. Fixed positions; the panel's queries (src/admin-visitors.js) read them so:
// blob1 ev, blob2 site, blob3 page, blob4 country, blob5 a, blob6 b, blob7 pv.
export const point = (site, pv, e, country) => ({
  indexes: [e.ev],
  blobs: [e.ev, site, "main", /^[A-Z]{2}$/.test(country || "") ? country : "XX", e.a, e.b, pv],
  doubles: [1],
});
// A dropped request, counted with its reason only (nothing about the sender).
export const dropPoint = (site, why) => ({ indexes: ["drop"], blobs: ["drop", site, "", "XX", why, "", ""], doubles: [1] });

// POST /e. Always answers 204 (counting never shows in a page); a browser sending Global Privacy Control is not counted at all.
export async function countEvents(request, env) {
  if (request.method !== "POST") return new Response(null, { status: 405, headers: { allow: "POST" } });
  const done = new Response(null, { status: 204, headers: { "cache-control": "no-store" } });
  const site = siteOf(new URL(request.url).hostname);
  if (!site || !env.EVENTS || request.headers.get("sec-gpc") === "1") return done;
  const write = (p) => { try { env.EVENTS.writeDataPoint(p); } catch { /* counting never breaks a page */ } };
  const drop = (why) => { write(dropPoint(site, why)); return done; };
  if (isBotUA(request.headers.get("user-agent"))) return drop("ua");
  if (Number(request.headers.get("content-length") || 0) > MAX_BYTES) return drop("size");
  const r = await readBatch(keyOf(env), site, await request.text());
  if (!r.ok) return drop(r.why);
  for (const e of r.events) write(point(site, r.pv, e, request.cf && request.cf.country));
  return done;
}

// The page the person opens, with the counter in it: the token for this host and path, and the script. No key, no EVENTS binding, an
// unknown host, another path, or anything that is not a finished HTML page = the response as it was, counting nothing.
export async function counted(response, request, env) {
  const { hostname, pathname } = new URL(request.url);
  const site = siteOf(hostname);
  if (!site || !env.EVENTS || !keyOf(env) || request.method !== "GET" || !PAGES.includes(pathname) || response.status !== 200) return response;
  if (!(response.headers.get("Content-Type") || "").includes("text/html")) return response;
  const html = await response.text();
  const headers = new Headers(response.headers);
  headers.delete("Content-Length");
  if (!html.includes("</head>")) return new Response(html, { status: response.status, headers });
  const token = await signPage(keyOf(env), site, pathname);
  const tag = `<meta name="clk-ev" content="${token}" data-p="${pathname}">\n<script>${EV_SCRIPT}</script>\n</head>`;
  return new Response(html.replace("</head>", tag), { status: response.status, headers });
}

// The page's side. Plain ES5 so any phone runs it; it must not contain a closing script tag.
export const EV_SCRIPT = `(function(){
var m=document.querySelector('meta[name="clk-ev"]');
if(!m||!navigator.sendBeacon)return;
if(navigator.globalPrivacyControl||navigator.doNotTrack==='1'||window.doNotTrack==='1')return;
var P=m.getAttribute('data-p'),T=m.getAttribute('content');
var PV=Math.random().toString(36).slice(2,12);while(PV.length<8)PV+='0';
var queue=[],open=false,touched=false,robot=!!navigator.webdriver,sent=false,maxDepth=0;
var visibleMs=0,visibleSince=document.visibilityState==='visible'?performance.now():null;
function send(){if(!queue.length)return;var b=JSON.stringify({p:P,t:T,pv:PV,e:queue.splice(0,20)});try{navigator.sendBeacon('/e',new Blob([b],{type:'text/plain'}))}catch(e){}if(queue.length)send()}
function ev(n,a,b){queue.push({ev:n,a:a==null?'':String(a),b:b==null?'':String(b)});if(open&&queue.length>=10)send()}
var SRC={ai:'chatgpt chatgpt-com openai perplexity copilot gemini claude claude-ai',facebook:'facebook fb meta messenger',whatsapp:'whatsapp wa',viber:'viber',telegram:'telegram tg',x:'x twitter tw',instagram:'instagram ig insta',tiktok:'tiktok tt',youtube:'youtube yt',linkedin:'linkedin li',reddit:'reddit',email:'email e-mail mail newsletter gmail',qr:'qr qrcode qr-code poster flyer print',share:'share'};
var MED={social:'social social-media',message:'message messaging chat dm',paid:'paid cpc ppc ad ads',email:'email e-mail newsletter mail',print:'print qr poster flyer offline'};
function sort(v,t){v=String(v||'').trim().toLowerCase().replace(/[\\s_.]+/g,'-');if(!v)return '';for(var k in t)if((' '+t[k]+' ').indexOf(' '+v+' ')>=0)return k;return 'other'}
function word(v){v=String(v||'').trim().toLowerCase();if(v.indexOf('@')>=0)return 'email-address';v=v.replace(/[\\s_]+/g,'-').replace(/[^a-z0-9.-]/g,'').replace(/\\d{6,}/g,'').replace(/^[.-]+|[.-]+$/g,'');return v.slice(0,60).replace(/[.-]+$/,'')}
function nav(){var n=performance.getEntriesByType&&performance.getEntriesByType('navigation')[0];var t=n&&n.type==='reload'?'reload':n&&n.type==='back_forward'?'back':'new';var inside=false;try{inside=!!document.referrer&&new URL(document.referrer).host===location.host}catch(e){}return{t:t,inside:inside}}
function start(){
 queue.push({ev:'view',a:'',b:''});
 var n=nav();queue.push({ev:'nav',a:n.t,b:n.inside?'in':'out'});
 if(n.t==='new'&&!n.inside){
  var q;try{q=new URLSearchParams(location.search)}catch(e){q={get:function(){return null}}}
  var host='';try{host=document.referrer?new URL(document.referrer).hostname.toLowerCase().replace(/^www\\./,'').replace(/[^a-z0-9.-]/g,'').slice(0,100):''}catch(e){}
  if(q.get('utm_source'))queue.push({ev:'src',a:sort(q.get('utm_source'),SRC)||'other',b:sort(q.get('utm_medium'),MED)});
  queue.push({ev:'from',a:word(q.get('utm_source')),b:host});
  if(q.get('utm_source'))queue.push({ev:'tag',a:word(q.get('utm_medium')),b:word(q.get('utm_campaign'))});
 }
}
function check(){
 if(open||robot||!touched)return;
 var ms=visibleMs+(visibleSince!=null?performance.now()-visibleSince:0);
 if(ms<2000){setTimeout(check,2050-ms);return}
 open=true;start();send();
}
function touch(){if(touched)return;touched=true;check()}
['pointerdown','keydown','touchstart','wheel'].forEach(function(t){addEventListener(t,touch,{passive:true,capture:true})});
addEventListener('scroll',function(){touch();var h=document.documentElement.scrollHeight-innerHeight;var d=h<=0?1:(scrollY||pageYOffset)/h;if(d>maxDepth)maxDepth=d},{passive:true});
document.addEventListener('click',function(e){var a=e.target&&e.target.closest?e.target.closest('a[href]'):null;if(!a||!open)return;var h;try{h=new URL(a.href).hostname}catch(x){return}if(h===location.hostname||h.slice(-13)!=='citizenlk.com')return;ev('go',h.indexOf('bills.')===0?'bills':h.indexOf('procurement.')===0?'procurement':h.indexOf('admin.')===0?'admin':'other','')},true);
function leaving(){
 if(!sent){sent=true;
  if(open){var d=maxDepth>=0.98?'100':maxDepth>=0.75?'75':maxDepth>=0.5?'50':maxDepth>=0.25?'25':'';if(d)queue.push({ev:'scroll',a:d,b:''})}
  else queue.push({ev:'skip',a:robot?'webdriver':'noint',b:''});
 }
 send();
}
document.addEventListener('visibilitychange',function(){
 if(document.visibilityState==='visible'){visibleSince=performance.now();check();return}
 if(visibleSince!=null){visibleMs+=performance.now()-visibleSince;visibleSince=null}
 leaving();
});
addEventListener('pagehide',leaving);
})();`;
