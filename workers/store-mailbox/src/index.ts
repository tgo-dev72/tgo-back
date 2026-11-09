import PostalMime from "postal-mime";

interface Env {
  API_URL: string;
  INBOUND_SECRET: string;
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  const chunk = 0x8000;
  for (let index = 0; index < bytes.length; index += chunk) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunk));
  }
  return btoa(binary);
}

function addresses(value: unknown): string {
  if (!value || typeof value !== "object") return "";
  const entry = value as { name?: string; address?: string };
  return entry.name ? `${entry.name} <${entry.address ?? ""}>` : (entry.address ?? "");
}

function attachmentBytes(value: string | ArrayBuffer | Uint8Array): Uint8Array {
  if (typeof value === "string") return new TextEncoder().encode(value);
  return value instanceof Uint8Array ? value : new Uint8Array(value);
}

async function signature(secret: string, timestamp: string, body: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const digest = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(`${timestamp}.${body}`));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

export default {
  async email(message: ForwardableEmailMessage, env: Env): Promise<void> {
    const raw = await new Response(message.raw).arrayBuffer();
    const parsed = await new PostalMime().parse(raw);
    const header = (name: string) => parsed.headers.find((item) => item.key.toLowerCase() === name)?.value ?? "";
    const references = header("references").split(/\s+/).filter(Boolean);
    const payload = {
      from: addresses(parsed.from) || message.from,
      to: message.to,
      subject: parsed.subject ?? "",
      text: parsed.text ?? "",
      html: parsed.html ?? "",
      message_id: parsed.messageId ?? header("message-id"),
      in_reply_to: header("in-reply-to"),
      references,
      attachments: parsed.attachments.map((attachment) => ({
        filename: attachment.filename ?? "attachment",
        content_type: attachment.mimeType,
        content_id: attachment.contentId ?? "",
        content: bytesToBase64(attachmentBytes(attachment.content)),
      })),
    };
    const body = JSON.stringify(payload);
    const timestamp = Math.floor(Date.now() / 1000).toString();
    const response = await fetch(`${env.API_URL.replace(/\/$/, "")}/api/internal/mail/cloudflare/inbound/`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-tijarago-timestamp": timestamp,
        "x-tijarago-signature": await signature(env.INBOUND_SECRET, timestamp, body),
      },
      body,
    });
    if (response.status === 404) {
      message.setReject("Unknown TijaraGO store mailbox");
      return;
    }
    if (!response.ok) throw new Error(`Mailbox API returned ${response.status}`);
  },
} satisfies ExportedHandler<Env>;
