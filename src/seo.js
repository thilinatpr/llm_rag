// Search-engine rules for the services that sit behind a sign-in (Bill Assistant, Procurement Rules, the admin panel).
// None of their pages should appear in search results: every response says so with X-Robots-Tag, which also covers the API and the
// sign-in page. Crawlers must still be allowed to FETCH a page to see that header, so robots.txt does not disallow the two chat
// services; the admin panel is not meant to be crawled at all, so its robots.txt disallows everything.
// Public pages that should rank (see TODO.md "SEO") belong on citizenlk.com, not on these hosts.
export const NOINDEX = "noindex, nofollow, noarchive";

export function robotsTxt(disallowAll) {
  return new Response(disallowAll ? "User-agent: *\nDisallow: /\n" : "User-agent: *\nAllow: /\n", {
    headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "public, max-age=3600", "X-Robots-Tag": NOINDEX },
  });
}

// Wraps a Worker fetch handler. Copying the response makes its headers writable (redirects and fetched responses are immutable)
// and keeps the body stream, so streamed answers pass through unchanged.
export function noindex(handler, { disallowAll = false } = {}) {
  return async function fetch(request, ...rest) {
    const { pathname } = new URL(request.url);
    if (pathname === "/robots.txt" && (request.method === "GET" || request.method === "HEAD")) return robotsTxt(disallowAll);
    const response = await handler(request, ...rest);
    if (response.status === 101) return response; // a WebSocket upgrade cannot be rebuilt
    const out = new Response(response.body, response);
    out.headers.set("X-Robots-Tag", NOINDEX);
    return out;
  };
}
