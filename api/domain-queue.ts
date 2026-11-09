import { QueueClient } from "@vercel/queue";

type DomainJob = { action: "verify" | "remove"; domainId: number; operationId: string };
const queue = new QueueClient();

function decode(raw: unknown): DomainJob {
  let value = raw;
  if (value instanceof Uint8Array) value = new TextDecoder().decode(value);
  if (typeof value === "string") value = JSON.parse(value);
  const job = value as Partial<DomainJob>;
  if (!Number.isSafeInteger(job.domainId) || Number(job.domainId) <= 0 || !job.operationId) {
    throw new Error("Invalid domain job payload");
  }
  return { action: job.action === "remove" ? "remove" : "verify", domainId: Number(job.domainId), operationId: String(job.operationId) };
}

export default queue.handleNodeCallback<unknown>(async (raw) => {
  const job = decode(raw);
  const configured = process.env.STOREFRONT_API_URL?.trim();
  const host = process.env.VERCEL_URL?.trim();
  const origin = new URL(configured || (host ? `https://${host}` : ""));
  if (origin.protocol !== "https:") throw new Error("Executor origin must use HTTPS");
  const response = await fetch(`${origin.origin}/api/internal/storefront-domains/execute`, {
    method: "POST", redirect: "error",
    headers: { "content-type": "application/json", "x-domain-operation": job.operationId },
    body: JSON.stringify(job),
  });
  if (!response.ok) throw new Error(`Domain executor returned HTTP ${response.status}`);
}, { visibilityTimeoutSeconds: 120 });
