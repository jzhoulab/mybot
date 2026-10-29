// mybot's mailbox on Cloudflare: Email Routing hands this Worker each message
// sent to the bot's address; it keeps only the owner's mail in a KV queue that
// the owner's mybot polls (outbound-only from the Mac — nothing is exposed).
// Replies come back as finished MIME and go out through a send_email binding
// that Cloudflare restricts to the owner's verified addresses, so even a
// stolen token can only ever email the owner.
import { EmailMessage } from "cloudflare:email";

const MAX_RAW = 20 * 1024 * 1024;
const QUEUE = "in/";

function owners(env) {
  return String(env.OWNER_ADDRESSES || "").toLowerCase().split(/[,\s]+/).filter(Boolean);
}

function headerAddress(value) {
  const match = String(value || "").match(/<([^>]+)>/);
  return (match ? match[1] : String(value || "")).trim().toLowerCase();
}

function toBase64(bytes) {
  let out = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    out += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(out);
}

function authorized(request, env) {
  const got = request.headers.get("authorization") || "";
  const want = `Bearer ${env.MYBOT_MAIL_TOKEN || ""}`;
  if (!env.MYBOT_MAIL_TOKEN || got.length !== want.length) return false;
  let diff = 0;
  for (let i = 0; i < got.length; i++) diff |= got.charCodeAt(i) ^ want.charCodeAt(i);
  return diff === 0;
}

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

export default {
  async email(message, env) {
    const allowed = owners(env);
    const envelope = String(message.from || "").toLowerCase();
    const header = headerAddress(message.headers.get("from"));
    // Anyone else's mail is dropped silently (no bounce to a forged sender).
    // mybot re-checks the DKIM signature itself before trusting a message.
    if (!allowed.includes(header) || message.rawSize > MAX_RAW) {
      console.log("dropped mail", { allowedSender: allowed.includes(header), size: message.rawSize });
      return;
    }
    const raw = new Uint8Array(await new Response(message.raw).arrayBuffer());
    const id = `${QUEUE}${Date.now().toString(36)}-${crypto.randomUUID()}`;
    await env.MAIL_QUEUE.put(id, JSON.stringify({
      id, envelope_from: envelope, to: message.to, received: new Date().toISOString(), raw: toBase64(raw),
    }), { expirationTtl: 14 * 24 * 3600 });
  },

  async fetch(request, env) {
    if (!authorized(request, env)) return json({ ok: false, error: "unauthorized" }, 401);
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/inbox") {
      const listed = await env.MAIL_QUEUE.list({ prefix: QUEUE, limit: 20 });
      const messages = [];
      for (const key of listed.keys) {
        const value = await env.MAIL_QUEUE.get(key.name);
        if (value) messages.push(JSON.parse(value));
      }
      return json({ ok: true, messages });
    }
    if (request.method === "POST" && url.pathname === "/ack") {
      const { ids = [] } = await request.json();
      await Promise.all(ids.filter((id) => String(id).startsWith(QUEUE)).map((id) => env.MAIL_QUEUE.delete(id)));
      return json({ ok: true });
    }
    if (request.method === "POST" && url.pathname === "/send") {
      // { to, raw } — raw is the finished MIME message, base64.
      const { to, raw } = await request.json();
      const recipient = String(to || "").toLowerCase();
      if (!owners(env).includes(recipient)) return json({ ok: false, error: "recipient not allowed" }, 403);
      const bytes = Uint8Array.from(atob(String(raw || "")), (c) => c.charCodeAt(0));
      try {
        await env.EMAIL.send(new EmailMessage(env.MAIL_FROM, recipient, bytes));
      } catch (error) {
        return json({ ok: false, error: String(error && error.message || error) }, 502);
      }
      return json({ ok: true });
    }
    return json({ ok: false, error: "not found" }, 404);
  },
};
