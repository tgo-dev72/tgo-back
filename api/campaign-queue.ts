import { QueueClient, send } from "@vercel/queue";

type CampaignJob = { campaignId: string; taskId: string; batch: number };
const TOPIC = "marketing-campaigns";
const queue = new QueueClient();

function decode(raw: unknown): CampaignJob {
  let value = raw;
  if (value instanceof Uint8Array) value = new TextDecoder().decode(value);
  if (typeof value === "string") value = JSON.parse(value);
  const job = value as Partial<CampaignJob>;
  if (!job?.campaignId || !job.taskId) throw new Error("Invalid campaign job payload");
  const batch = Number(job.batch ?? 0);
  return { campaignId: String(job.campaignId), taskId: String(job.taskId), batch: Number.isSafeInteger(batch) ? batch : 0 };
}

function executorOrigin(): string {
  const configured = process.env.STOREFRONT_API_URL?.trim();
  const host = process.env.VERCEL_URL?.trim();
  const origin = new URL(configured || (host ? `https://${host}` : ""));
  if (origin.protocol !== "https:") throw new Error("Campaign executor origin must use HTTPS");
  return origin.origin;
}

export default queue.handleNodeCallback<unknown>(async (raw) => {
  const job = decode(raw);
  const headers: Record<string, string> = { "content-type": "application/json", "x-campaign-task": job.taskId };
  const bypass = process.env.VERCEL_AUTOMATION_BYPASS_SECRET;
  if (bypass) headers["x-vercel-protection-bypass"] = bypass;
  const response = await fetch(`${executorOrigin()}/api/internal/marketing-campaigns/${job.campaignId}/execute`, {
    method: "POST", headers, body: "{}", redirect: "error",
  });
  if (!response.ok) throw new Error(`Campaign executor returned HTTP ${response.status}`);
  const result = await response.json() as { terminal?: boolean; remaining?: number };
  if (!result.terminal) {
    const next = { ...job, batch: job.batch + 1 };
    await send(TOPIC, next, {
      delaySeconds: 1, retentionSeconds: 86400,
      idempotencyKey: `${job.taskId}:batch:${next.batch}`,
    });
  }
}, {
  visibilityTimeoutSeconds: 180,
  retry: (_error, metadata) => metadata.deliveryCount >= 6
    ? { acknowledge: true }
    : { afterSeconds: Math.min(300, 15 * 2 ** (metadata.deliveryCount - 1)) },
});
