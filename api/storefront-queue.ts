import { QueueClient, send } from "@vercel/queue";

type StorefrontJob = {
  action?: "deploy" | "reconcile";
  requestId: number;
  taskId: string;
  reconciliationAttempt?: number;
};

const TOPIC = "storefront-deployments";
const queue = new QueueClient();
const { handleNodeCallback } = queue;

function decodeStorefrontJob(rawJob: unknown): StorefrontJob {
  let candidate = rawJob;
  if (candidate instanceof Uint8Array) {
    candidate = new TextDecoder().decode(candidate);
  }
  if (typeof candidate === "string") {
    try {
      candidate = JSON.parse(candidate) as unknown;
    } catch {
      throw new Error("Invalid storefront job payload");
    }
  }
  if (!candidate || typeof candidate !== "object") {
    throw new Error("Invalid storefront job payload");
  }

  const value = candidate as Record<string, unknown>;
  const requestId = Number(value.requestId);
  const taskId = typeof value.taskId === "string" ? value.taskId.trim() : "";
  if (!Number.isSafeInteger(requestId) || requestId <= 0 || !taskId) {
    throw new Error("Invalid storefront job payload");
  }

  const reconciliationAttempt = Number(value.reconciliationAttempt ?? 0);
  return {
    action: value.action === "reconcile" ? "reconcile" : "deploy",
    requestId,
    taskId,
    reconciliationAttempt: Number.isSafeInteger(reconciliationAttempt)
      ? Math.max(0, reconciliationAttempt)
      : 0,
  };
}

function executorOrigin(): string {
  // Prefer the stable custom API origin. VERCEL_URL remains a deployment-local
  // fallback for projects that do not configure the application setting.
  const configured = process.env.STOREFRONT_API_URL?.trim();
  const vercelHost = process.env.VERCEL_URL?.trim();
  const rawOrigin = configured || (vercelHost ? `https://${vercelHost}` : "");
  if (!rawOrigin) {
    throw new Error("The storefront executor origin is unavailable");
  }

  const origin = new URL(rawOrigin);
  if (origin.protocol !== "https:") {
    throw new Error("The storefront executor origin must use HTTPS");
  }
  return origin.origin;
}

export default handleNodeCallback<unknown>(
  async (rawJob) => {
    const job = decodeStorefrontJob(rawJob);

    const action = job.action === "reconcile" ? "reconcile" : "execute";
    const reconciliationAttempt = Math.max(0, job.reconciliationAttempt ?? 0);
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      "X-Storefront-Task": job.taskId,
      "X-Reconciliation-Attempt": String(reconciliationAttempt),
    };
    const bypass = process.env.VERCEL_AUTOMATION_BYPASS_SECRET;
    if (bypass) headers["x-vercel-protection-bypass"] = bypass;

    const response = await fetch(
      `${executorOrigin()}/api/internal/storefront-deployments/${job.requestId}/${action}`,
      { method: "POST", headers, body: "{}", redirect: "error" },
    );
    if (!response.ok) {
      throw new Error(`Storefront executor returned HTTP ${response.status}`);
    }
    const result = (await response.json()) as { terminal?: boolean };
    if (!result.terminal) {
      const nextAttempt = reconciliationAttempt + 1;
      await send(
        TOPIC,
        {
          action: "reconcile",
          requestId: job.requestId,
          taskId: job.taskId,
          reconciliationAttempt: nextAttempt,
        } satisfies StorefrontJob,
        {
          delaySeconds: 20,
          retentionSeconds: 86400,
          idempotencyKey: `${job.taskId}:reconcile:${nextAttempt}`,
        },
      );
    }
  },
  {
    visibilityTimeoutSeconds: 300,
    retry: (error, metadata) => {
      // The payload and capability token are deliberately omitted from logs.
      console.error("Storefront queue delivery failed", {
        deliveryCount: metadata.deliveryCount,
        reason: error instanceof Error ? error.message : "Unknown queue delivery error",
      });
      if (metadata.deliveryCount >= 6) return { acknowledge: true };
      return { afterSeconds: Math.min(300, 15 * 2 ** (metadata.deliveryCount - 1)) };
    },
  },
);
