import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = await mkdtemp(join(tmpdir(), "tijarago-seo-renderer-"));
const rendererSource = await readFile(fileURLToPath(new URL("../storefront_factory/seo_renderer.mjs", import.meta.url)), "utf8");
const shell = '<!doctype html><html><head><title>Shell</title><meta name="description" content="Shell"></head><body><div id="root"></div></body></html>';
await mkdir(join(root, "api"));
await writeFile(join(root, "api", "seo-render.mjs"), rendererSource);
await writeFile(join(root, "api", "_seo-shell.mjs"), `export const STOREFRONT_HTML = ${JSON.stringify(shell)};\n`);
process.chdir(root);
const { default: handler } = await import(pathToFileURL(join(root, "api", "seo-render.mjs")));
process.env.VITE_API_URL = "https://api.tijarago.com";
process.env.VITE_STORE_SLUG = "atlas";

const cases = new Map([
  ["/", { status: 200, locale: "fr", title: "Atlas & Maison", description: "Créations مغربية", robots: "index, follow", canonical: "https://atlas.tijarago.com/", openGraph: { title: "Atlas & Maison", description: "Créations مغربية", url: "https://atlas.tijarago.com/", type: "website" }, twitter: { title: "Atlas & Maison", description: "Créations مغربية", card: "summary" }, structuredData: [{ "@context": "https://schema.org", "@type": "OnlineStore", name: "Atlas </script> & Maison" }] }],
  ["/products/caftan", { status: 200, locale: "fr", title: "Caftan d’été", description: "Tissé à Fès", robots: "index, follow", canonical: "https://atlas.tijarago.com/products/caftan", openGraph: { title: "Caftan d’été", description: "Tissé à Fès", url: "https://atlas.tijarago.com/products/caftan", type: "product", image: "https://cdn.example/caftan.jpg" }, twitter: { title: "Caftan d’été", description: "Tissé à Fès", card: "summary_large_image", image: "https://cdn.example/caftan.jpg" }, structuredData: [{ "@context": "https://schema.org", "@type": "Product", name: "Caftan" }] }],
  ["/collections/artisanat", { status: 200, locale: "fr", title: "Artisanat", description: "Fait main", robots: "index, follow", canonical: "https://atlas.tijarago.com/collections/artisanat", openGraph: { title: "Artisanat", description: "Fait main", url: "https://atlas.tijarago.com/collections/artisanat" }, twitter: { title: "Artisanat", description: "Fait main" }, structuredData: [{ "@context": "https://schema.org", "@type": "CollectionPage", name: "Artisanat" }] }],
  ["/account", { status: 200, locale: "fr", title: "Compte", description: "Privé", robots: "noindex, nofollow, noarchive", canonical: "https://atlas.tijarago.com/", openGraph: {}, twitter: {}, structuredData: [] }],
  ["/missing", { status: 404, locale: "fr", title: "Page introuvable", description: "Introuvable", robots: "noindex, nofollow, noarchive", canonical: "https://atlas.tijarago.com/missing", openGraph: {}, twitter: {}, structuredData: [] }],
  ["/?search=caftan", { status: 200, locale: "fr", title: "Recherche", description: "Résultats", robots: "noindex, nofollow, noarchive", canonical: "https://atlas.tijarago.com/", openGraph: {}, twitter: {}, structuredData: [] }],
]);

globalThis.fetch = async (url) => {
  const metadata = cases.get(new URL(url).searchParams.get("path"));
  assert.ok(metadata, `unexpected metadata path ${url}`);
  return { ok: metadata.status < 400, status: metadata.status, json: async () => metadata };
};

async function render(path, query = {}) {
  const headers = new Map();
  let body = "";
  const response = { statusCode: 0, setHeader: (key, value) => headers.set(key.toLowerCase(), value), end: (value) => { body = value; } };
  await handler({ query: { path, ...query } }, response);
  return { body, headers, status: response.statusCode };
}

for (const path of ["/", "/products/caftan", "/collections/artisanat", "/account", "/missing"]) {
  const result = await render(path);
  const expected = cases.get(path);
  assert.equal(result.status, expected.status);
  assert.equal(result.headers.get("content-type"), "text/html; charset=utf-8");
  assert.match(result.body, new RegExp(`<title>${expected.title.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replaceAll("&", "&amp;")}</title>`));
  assert.ok(result.body.includes(`href="${expected.canonical}"`));
  assert.ok(result.body.includes(`content="${expected.robots}"`));
  for (const match of result.body.matchAll(/<script type="application\/ld\+json">([^<]+)<\/script>/g)) JSON.parse(match[1]);
}
const search = await render("/", { search: "caftan" });
assert.equal(search.status, 200);
assert.ok(search.body.includes("<title>Recherche</title>"));
assert.equal(search.headers.get("x-robots-tag"), "noindex, nofollow, noarchive");

const home = await render("/");
assert.ok(home.body.includes("Atlas &amp; Maison"));
assert.ok(home.body.includes("Atlas \\u003c/script> & Maison"));
assert.ok(!home.body.includes("Atlas </script>"));
const account = await render("/account");
assert.equal(account.headers.get("x-robots-tag"), "noindex, nofollow, noarchive");

const missingRoot = await mkdtemp(join(tmpdir(), "tijarago-seo-renderer-missing-"));
await mkdir(join(missingRoot, "api"));
await writeFile(join(missingRoot, "api", "seo-render.mjs"), rendererSource);
await writeFile(join(missingRoot, "api", "_seo-shell.mjs"), 'export const STOREFRONT_HTML = "";\n');
const { default: missingHandler } = await import(pathToFileURL(join(missingRoot, "api", "seo-render.mjs")));
const missingHeaders = new Map();
let missingBody = "";
await missingHandler(
  { query: { path: "/products/caftan" } },
  { statusCode: 0, setHeader: (key, value) => missingHeaders.set(key.toLowerCase(), value), end: (value) => { missingBody = value; } },
);
assert.equal(missingHeaders.get("x-tijarago-seo-error"), "html_shell_missing");
assert.equal(missingHeaders.get("x-robots-tag"), "noindex, nofollow, noarchive");
assert.ok(!missingBody.includes(process.cwd()));

console.log("Storefront raw-HTML SEO renderer OK — metadata routes, safe packaged shell, and fail-closed missing-shell diagnostics");
