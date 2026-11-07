// citizenlk.com homepage: the static page in site/, no login and no container. www redirects to the bare domain.
const HOME = "citizenlk.com";
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
    const asset = await env.ASSETS.fetch(request);
    const response = new Response(asset.body, asset);
    for (const [name, value] of Object.entries(SECURITY_HEADERS)) response.headers.set(name, value);
    return response;
  },
};
