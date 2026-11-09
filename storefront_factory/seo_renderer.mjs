import { STOREFRONT_HTML } from "./_seo-shell.mjs";

const FALLBACK_HTML = '<!doctype html><html><head><meta name="robots" content="noindex,nofollow"><title>Storefront temporarily unavailable</title></head><body></body></html>';

class RendererError extends Error {
  constructor(category) {
    super(category);
    this.category = category;
  }
}

const escapeHtml = (value = "") => String(value).replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
const safeJson = (value) => JSON.stringify(value).replaceAll("<", "\\u003c");

function tags(meta) {
  const og = meta.openGraph ?? {};
  const twitter = meta.twitter ?? {};
  const values = [
    `<title>${escapeHtml(meta.title)}</title>`,
    `<meta name="description" content="${escapeHtml(meta.description)}">`,
    `<meta name="robots" content="${escapeHtml(meta.robots)}">`,
    `<link rel="canonical" href="${escapeHtml(meta.canonical)}">`,
    `<meta property="og:type" content="${escapeHtml(og.type ?? "website")}">`,
    `<meta property="og:title" content="${escapeHtml(og.title)}">`,
    `<meta property="og:description" content="${escapeHtml(og.description)}">`,
    `<meta property="og:url" content="${escapeHtml(og.url)}">`,
    `<meta name="twitter:card" content="${escapeHtml(twitter.card ?? "summary")}">`,
    `<meta name="twitter:title" content="${escapeHtml(twitter.title)}">`,
    `<meta name="twitter:description" content="${escapeHtml(twitter.description)}">`,
  ];
  if (og.image) values.push(`<meta property="og:image" content="${escapeHtml(og.image)}">`);
  if (twitter.image) values.push(`<meta name="twitter:image" content="${escapeHtml(twitter.image)}">`);
  for (const item of meta.structuredData ?? []) values.push(`<script type="application/ld+json">${safeJson(item)}</script>`);
  return values.join("\n    ");
}

export default async function handler(request, response) {
  const api = String(process.env.VITE_API_URL ?? "").replace(/\/$/, "");
  const slug = String(process.env.VITE_STORE_SLUG ?? "");
  const rawPath = Array.isArray(request.query.path) ? request.query.path[0] : request.query.path || "/";
  const forwardedQuery = new URLSearchParams();
  for (const [key, value] of Object.entries(request.query ?? {})) {
    if (key === "path") continue;
    for (const item of Array.isArray(value) ? value : [value]) {
      if (item != null) forwardedQuery.append(key, String(item));
    }
  }
  const requestedPath = forwardedQuery.size
    ? `${rawPath}${String(rawPath).includes("?") ? "&" : "?"}${forwardedQuery}`
    : rawPath;
  let html;
  try {
    if (typeof STOREFRONT_HTML !== "string" || !STOREFRONT_HTML.includes("</head>") || !STOREFRONT_HTML.includes('id="root"')) {
      throw new RendererError("html_shell_missing");
    }
    html = STOREFRONT_HTML;
    if (!api.startsWith("https://") || !/^[a-z][a-z0-9-]{0,49}$/.test(slug)) {
      throw new RendererError("renderer_configuration_invalid");
    }
    const endpoint = new URL(`${api}/api/public/storefront/${encodeURIComponent(slug)}/seo`);
    endpoint.searchParams.set("path", requestedPath);
    let metadataResponse;
    try {
      metadataResponse = await fetch(endpoint, { headers: { Accept: "application/json" } });
    } catch {
      throw new RendererError("metadata_fetch_failed");
    }
    if (![200, 308, 404].includes(Number(metadataResponse.status))) {
      throw new RendererError("metadata_fetch_failed");
    }
    let metadata;
    try {
      metadata = await metadataResponse.json();
    } catch {
      throw new RendererError("metadata_invalid");
    }
    if (!metadata || typeof metadata !== "object" || !metadata.title || !metadata.canonical || !metadata.robots) {
      throw new RendererError("metadata_invalid");
    }
    html = html
      .replace(/\s*<title>[^<]*<\/title>/i, "")
      .replace(/\s*<meta name="description"[^>]*>/i, "")
      .replace("</head>", `    ${tags(metadata)}\n  </head>`);
    response.statusCode = Number(metadata.status) || metadataResponse.status || 200;
    if (response.statusCode === 308 && metadata.redirect) response.setHeader("Location", metadata.redirect);
    response.setHeader("Content-Language", metadata.locale || "en");
    if (String(metadata.robots || "").includes("noindex")) response.setHeader("X-Robots-Tag", "noindex, nofollow, noarchive");
    response.setHeader("Cache-Control", "private, no-store");
  } catch (error) {
    const category = error instanceof RendererError ? error.category : "renderer_unexpected";
    console.error(JSON.stringify({ event: "storefront_seo_renderer_failed", category }));
    response.statusCode = 503;
    response.setHeader("X-Robots-Tag", "noindex, nofollow, noarchive");
    response.setHeader("X-TijaraGO-SEO-Error", category);
    response.setHeader("Cache-Control", "private, no-store");
    html = html || FALLBACK_HTML;
  }
  response.setHeader("Content-Type", "text/html; charset=utf-8");
  response.end(html);
}
