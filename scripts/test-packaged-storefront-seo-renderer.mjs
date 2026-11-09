import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { join, resolve } from "node:path";

const artifact = resolve(process.argv[2] ?? "");
const expectMissing = process.argv.includes("--expect-missing");
assert.ok(artifact, "pass the isolated packaged function directory");

process.env.VITE_API_URL = "https://api.example.invalid";
process.env.VITE_STORE_SLUG = "bundle-canary";

const { default: handler } = await import(pathToFileURL(join(artifact, "seo-render.mjs")));

globalThis.fetch = async (url) => {
  const path = new URL(url).searchParams.get("path") || "/";
  const privateRoute = ["/account", "/checkout", "/search"].includes(path) || path.includes("?");
  const unknown = path === "/missing";
  const canonicalPath = privateRoute || unknown ? "/" : path;
  return {
    ok: !unknown,
    status: unknown ? 404 : 200,
    json: async () => ({
      status: unknown ? 404 : 200,
      locale: "en",
      title: `SEO ${path}`,
      description: "Packaged renderer verification",
      robots: privateRoute || unknown ? "noindex, nofollow, noarchive" : "index, follow",
      canonical: `https://bundle-canary.tijarago.com${canonicalPath}`,
      openGraph: { title: `SEO ${path}`, description: "Packaged renderer verification", url: `https://bundle-canary.tijarago.com${canonicalPath}` },
      twitter: { title: `SEO ${path}`, description: "Packaged renderer verification" },
      structuredData: privateRoute || unknown ? [] : [{ "@context": "https://schema.org", "@type": path.startsWith("/products/") ? "Product" : "WebPage" }],
    }),
  };
};

async function render(path, query = {}) {
  const headers = new Map();
  let body = "";
  const response = {
    statusCode: 0,
    setHeader: (key, value) => headers.set(key.toLowerCase(), value),
    end: (value) => { body = value; },
  };
  await handler({ query: { path, ...query } }, response);
  return { status: response.statusCode, headers, body };
}

if (!expectMissing) {
  const searchQuery = await render("/", { search: "market tote" });
  assert.notEqual(searchQuery.status, 503);
  assert.equal(searchQuery.headers.get("x-robots-tag"), "noindex, nofollow, noarchive");
}

if (expectMissing) {
  const result = await render("/products/market-tote");
  assert.equal(result.status, 503);
  assert.equal(result.headers.get("x-tijarago-seo-error"), "html_shell_missing");
  assert.equal(result.headers.get("x-robots-tag"), "noindex, nofollow, noarchive");
  assert.ok(!result.body.includes(artifact));
  console.log("Packaged storefront missing-shell failure is safe and categorized");
  process.exit(0);
}

for (const path of ["/", "/products/market-tote", "/collections/home", "/account", "/checkout", "/search", "/missing"]) {
  const result = await render(path);
  assert.notEqual(result.status, 503, `${path} unexpectedly failed closed`);
  if (path === "/missing") assert.equal(result.status, 404);
  assert.equal(result.headers.get("content-type"), "text/html; charset=utf-8");
  assert.ok(result.body.includes(`<title>SEO ${path}</title>`));
  assert.ok(result.body.includes("bundle-canary.tijarago.com"));
  for (const match of result.body.matchAll(/<script type="application\/ld\+json">([^<]+)<\/script>/g)) JSON.parse(match[1]);
  if (["/account", "/checkout", "/search", "/missing"].includes(path)) {
    assert.equal(result.headers.get("x-robots-tag"), "noindex, nofollow, noarchive");
  }
}

console.log("Packaged storefront renderer serves representative SEO routes from an isolated function artifact");
