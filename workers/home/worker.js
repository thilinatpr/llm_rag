// citizenlk.com homepage: the static page in site/, no login and no container. www redirects to the bare domain.
import { countEvents, counted } from "../../src/events.js";

export const HOME = "citizenlk.com";
const SECURITY_HEADERS = {
  "Strict-Transport-Security": "max-age=31536000",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Referrer-Policy": "strict-origin-when-cross-origin",
};

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.hostname === `www.${HOME}`) {
      url.hostname = HOME;
      return Response.redirect(url.toString(), 301);
    }
    if (url.pathname === "/e") return countEvents(request, env); // anonymous visitor counts (src/events.js)
    const asset = await counted(await env.ASSETS.fetch(request), request, env);
    const response = new Response(asset.body, asset);
    for (const [name, value] of Object.entries(SECURITY_HEADERS)) response.headers.set(name, value);
    return response;
  },
};
