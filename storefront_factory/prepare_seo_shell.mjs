import { readFile, rm, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

// The isolated-bundle builder replaces this value with the generated app path.
const APP_PATH = ".";
const MODULE_PATH = "api/_seo-shell.mjs";
const MAX_SHELL_BYTES = 256 * 1024;

const shellPath = resolve(process.cwd(), APP_PATH, "dist/index.html");
const modulePath = resolve(process.cwd(), MODULE_PATH);

try {
  const html = await readFile(shellPath, "utf8");
  if (!html.includes("</head>") || !html.includes('id="root"') || Buffer.byteLength(html) > MAX_SHELL_BYTES) {
    throw new Error("invalid shell");
  }
  await writeFile(modulePath, `export const STOREFRONT_HTML = ${JSON.stringify(html)};\n`, "utf8");
  await rm(shellPath);
  console.log("Prepared deterministic storefront SEO shell module");
} catch {
  console.error(JSON.stringify({ event: "storefront_seo_shell_prepare_failed", category: "html_shell_unreadable" }));
  process.exitCode = 1;
}
