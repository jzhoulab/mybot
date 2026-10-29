// mybot mailboxes on Cloudflare — one Worker for the whole team, one mailbox
// per person's mybot (e.g. mybot-<name>@zhoulab.io).
//
// Email Routing hands this Worker each message sent to a mailbox address; it
// keeps only mail from that mailbox's owner, in a KV queue the owner's mybot
// polls (outbound-only from their Mac — nothing is exposed). Replies come back
// as finished MIME and are sent only to that mailbox's owner; Cloudflare also
// refuses any recipient that isn't a verified address in the account.
//
// Each mailbox is a KV entry `mailbox/<address>`:
//   { "owners": ["you@gmail.com"], "token_sha256": "<hex>" }
// written by scripts/mail_mailbox.py, so adding a teammate needs no redeploy.
// A mybot authenticates with its own token plus `X-Mybot-Mailbox: <address>`.
import { EmailMessage } from "cloudflare:email";

const MAX_RAW = 20 * 1024 * 1024;

const lower = (s) => String(s || "").trim().toLowerCase();
const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function headerAddress(value) {
  const match = String(value || "").match(/<([^>]+)>/);
  return lower(match ? match[1] : value);
}

function toBase64(bytes) {
  let out = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    out += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(out);
}

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function sameString(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

async function mailbox(env, address) {
  const value = await env.MAIL_QUEUE.get(`mailbox/${lower(address)}`);
  if (!value) return null;
  const box = JSON.parse(value);
  box.owners = (box.owners || []).map(lower);
  return box;
}

export default {
  async email(message, env) {
    const address = lower(message.to);
    const box = await mailbox(env, address);
    const from = headerAddress(message.headers.get("from"));
    // Anything else is dropped silently (no bounce to a forged sender). The
    // owner's mybot re-checks the DKIM signature before trusting a message.
    if (!box || !box.owners.includes(from) || message.rawSize > MAX_RAW) {
      console.log("dropped mail", { mailbox: Boolean(box), size: message.rawSize });
      return;
    }
    const raw = new Uint8Array(await new Response(message.raw).arrayBuffer());
    const id = `in/${address}/${Date.now().toString(36)}-${crypto.randomUUID()}`;
    await env.MAIL_QUEUE.put(id, JSON.stringify({
      id, envelope_from: lower(message.from), to: address, received: new Date().toISOString(), raw: toBase64(raw),
    }), { expirationTtl: 14 * 24 * 3600 });
  },

  async fetch(request, env) {
    const address = lower(request.headers.get("x-mybot-mailbox"));
    const token = (request.headers.get("authorization") || "").replace(/^Bearer\s+/i, "");
    const box = address ? await mailbox(env, address) : null;
    if (!box || !token || !sameString(await sha256Hex(token), String(box.token_sha256 || ""))) {
      return json({ ok: false, error: "unauthorized" }, 401);
    }
    const prefix = `in/${address}/`;
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/inbox") {
      const listed = await env.MAIL_QUEUE.list({ prefix, limit: 20 });
      const messages = [];
      for (const key of listed.keys) {
        const value = await env.MAIL_QUEUE.get(key.name);
        if (value) messages.push(JSON.parse(value));
      }
      return json({ ok: true, messages });
    }
    if (request.method === "POST" && url.pathname === "/ack") {
      const { ids = [] } = await request.json();
      await Promise.all(ids.filter((id) => String(id).startsWith(prefix)).map((id) => env.MAIL_QUEUE.delete(id)));
      return json({ ok: true });
    }
    if (request.method === "POST" && url.pathname === "/send") {
      // { to, raw } — raw is the finished MIME message (From: the mailbox), base64.
      const { to, raw } = await request.json();
      const recipient = lower(to);
      if (!box.owners.includes(recipient)) return json({ ok: false, error: "recipient not allowed" }, 403);
      // EmailMessage takes the MIME as a string (Python's SMTP policy keeps it 7-bit).
      const bytes = Uint8Array.from(atob(String(raw || "")), (c) => c.charCodeAt(0));
      try {
        await env.EMAIL.send(new EmailMessage(address, recipient, new TextDecoder().decode(bytes)));
      } catch (error) {
        return json({ ok: false, error: String((error && error.message) || error) }, 502);
      }
      return json({ ok: true });
    }
    return json({ ok: false, error: "not found" }, 404);
  },
};
