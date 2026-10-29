# Multi-user operation (team deployment)

mybot is a **collaboration tool for any team** — a research lab, an engineering team, a
startup. Each team member runs their **own** mybot instance over their own trajectories.
The bot is its owner's **assistant and representative**: a plain, factual information
source over the owner's work that lets teammates get answers about it quickly ("what did
Alex do with the nightly training run?") without pinging the owner. Together the instances form a
mesh where everyone can query everyone else's work through their bots. An instance has
one *owner* and any number of *guests* (teammates talking to it over Discord — later
Slack). Identity is transport-agnostic: every request carries an `actor_id` (Discord
user id today; a Slack user id maps the same way).

Reference resolution: the owner saying "what did I do" and anyone else saying "what
did <owner name> do" both mean the owner's work. A guest's own first-person
references ("my notes") mean the guest.

## Who the bot serves vs who is asking

- **Owner** — the actor id is `MEMORY_IMPORTED_OWNER_ID`; the *name* is discovered,
  not configured. **Onboarding**: on server start, if `state/owner_identity.json`
  doesn't exist (and the index has content), a background **Sherlock session** runs
  automatically — the agent deduces the owner's name and handles from the
  trajectories themselves (git author lines, cluster usernames, self-references),
  cross-checking independent evidence — and saves the result as unconfirmed. The
  menu app shows the flow live: a "Getting to know you…" card while investigating,
  then "I did some digging — you're <name>, right?" with **That's me** /
  **Not quite…** (inline correction). Once confirmed, the header reads
  "Serving <name>"; right-click the bunny avatar to re-run discovery. The bot also
  confirms conversationally (the prompt nudges it to ask once), and `!iam <name>`
  on Discord confirms/corrects any time (owner-only; guests get 403). Manual
  trigger: `POST /owner/identity {"action":"investigate","save":true}` (add
  `"background":true` for async; `{"action":"get"}` reports `investigating`).
  `OWNER_DISPLAY_NAME` env remains only as a fallback. `TEAM_NAME` optionally names
  the team. The fuller owner profile in `profiles/default/USER.md` (regenerable via
  `/owner/profile`) tells the bot whose shorthand and "my/our" references it is
  grounding.
- **Asker** — every `/chat` carries `actor_id`, `actor_display_name`, `channel_kind`
  (`dm`|`group`), `channel_label`. The system prompt gets a `## Who Is Asking` section:
  owner → full trust; guest → "a teammate of <owner>, not the owner", with scope rules.
- **Access** — with `GUEST_OWNER_ACCESS=true` (default), guests query the owner's
  trajectory history through the bot: that is the collaboration feature — limited to
  projects the owner shared (see "Privacy model" below). Whoever the channel
  allowlist admits can read what is shared, so gate exposure with
  `DISCORD_ALLOWED_CHANNEL_IDS` / `SLACK_ALLOWED_CHANNEL_IDS`.
  Set `GUEST_OWNER_ACCESS=false` to restrict trajectory lookup to the owner: guests
  then fall back to shared team memory + their own private memories + the current
  conversation (endpoints enforce 403 regardless of what the prompt advertises), and
  `!ask @owner <question>` remains the explicit owner-scope valve.

## Privacy model

What a teammate can learn through the owner's bot, and what enforces it. Prompt
rules (the conduct policy) shape answers; everything below holds even if a model
is talked into ignoring them.

- **Team sharing.** With `TEAM_SHARING=reviewed` (default) a teammate's answer can
  draw only on projects the owner set to *team*; new and unreviewed projects stay
  owner-only. Single sessions can be made owner-only inside a shared project. An
  agent (`SHARING_REVIEW_MODEL`, default `sonnet`) reads each project's session
  titles and the owner's typed requests and suggests *team / only me / exclude*
  with a reason, flagging personal sessions (tenure files, taxes, HR, confidential
  peer review…) in work folders. It runs after startup and hourly for new or grown
  projects, and never decides: the owner confirms in the menu app's **Team
  sharing** view. Decisions live in `state/sharing.json`; `TEAM_SHARING=open`
  restores folder-only rules.
- **Owner-private folders** (`private_workdirs`, `private_workdir_classes`) are never
  served to anyone else, whatever the sharing level.
- **Shared channels.** An answer posted in a channel is read by everyone there, so
  it uses teammate-level data even when the owner asks.
- **Authenticated API.** Every endpoint but `/health` and sync needs the local API
  key (`~/.mybot/api-key`, created on first start). The bridges, menu app, control
  page and the owner's own tools send it. Answer agents never see it: each turn
  gets a grant bound to the asker's identity and scope, good only for the search
  and read endpoints, so a guest who talks the agent into calling the API directly
  still gets exactly the guest's view.
- **Agent isolation.** Answer agents run in an OS sandbox (home unreadable except
  their workspace, network to localhost only), with an allowlisted environment
  (no bot tokens or API keys), none of the owner's Claude settings, CLAUDE.md,
  connectors, MCP servers or auto-memory, and a tool restored before every turn.
- **Credentials.** Keys, tokens, passwords and private keys found in transcripts
  are masked in every response and streamed event (`app/redact.py`).
- **Identity.** Owner status is decided by platform id (`MEMORY_IMPORTED_OWNER_ID`
  plus `MYBOT_OWNER_ACTOR_IDS` for other platforms), never by display name.
  Shared-channel turns are recorded as `[Name · id]` with forged brackets removed.
  A sibling bot's relay can never claim the owner: its asker is namespaced under
  that bot. Shared notes (`!remember-shared`) are owner-only.

## Remembering people over time

`state/people.json` (`PersonRegistry`) records per actor: display name, first/last
seen, chat count, observed-message count, and the last 10 chat topics. The prompt
includes "Prior contact: N chats since <date>. Recent topics: …" whenever a known
person returns, on any channel. Guests can also build their own long-term memory via
`!remember <note>` (private to them) and `!remember-shared <note>` (team-wide).

## Group channels

- With `DISCORD_GROUP_SESSIONS=true` (default), a guild channel/thread has **one
  shared session** (`discord-guild-<g>-channel-<c>`) instead of one per user. User
  turns are stored and shown to the model as `[Display Name] message`, so the
  transcript reads as a multi-party conversation and the bot addresses the current
  asker.
- With `DISCORD_OBSERVE_CHANNELS=true` (default), messages in **explicitly listed**
  channels (`DISCORD_ALLOWED_CHANNEL_IDS` / `DISCORD_AUTO_REPLY_CHANNEL_IDS`) that
  don't address the bot are recorded into the channel session via
  `POST /sessions/observe` (no model call, no reply). When someone later asks "what
  did Bob say about the run?", the answer comes from that observed context.
  Observation never applies to channels that are merely implicitly allowed (empty
  allowlist = reply anywhere, observe nowhere).
- Raw `<@id>` mentions are resolved to `@DisplayName` before storage.

## Team channel: bots talking in public

Each teammate's `<handle>-mybot` joins the team's shared Discord and Slack
channels. In the menu app's **Channel** tab the owner adds any number of them
(the bridges report every channel they can post in, on both platforms) and sees
them Slack-style: a sidebar grouped by Discord server or Slack workspace, with
unread counts, and for each channel its people, bots, replies and
"alex-mybot is answering…". Typing `@` offers that channel's members. Every
connected channel is mirrored, and in every one of them your bot answers
relayed asks.

- **@ a teammate's bot**: the message is posted by your bot as
  "**Jian** (via jian-mybot): @alex-mybot …" and carries a relayed *ask*. Alex's
  bot answers in the channel, as a reply.
- **@ your own bot**: it answers in the channel. That's public, so it answers with
  what teammates may see, not your private view.
- **No mention**: just a message to the channel, posted by your bot for you.

The bridge mirrors the channel to the server (`POST /channel/feed`, history
backfilled on connect), so the tab can show it and your bot keeps other bots'
answers as context.

### Relay protocol (`app/relay_protocol.py`)

A relayed message is readable text plus one machine-readable trailer line
(Discord renders `-#` as small grey text; Slack gets `_italics_`):

```
**Jian** (via jian-mybot): @alex-mybot did the nightly run finish?
-# ⟦mybot ask id=r3f2a actor=<owner's platform id> name=Jian⟧

(reply) The nightly run finished at 04:10 …
-# ⟦mybot ans id=r3f2a⟧
```

- **Trust:** an ask is answered only from a bot in a connected team channel
  (unless the owner switched it off in the channel's member list) or one listed
  in `MYBOT_SIBLING_BOT_IDS`, and only when it @mentions the receiver. On Slack
  the bot must also belong to the home workspace. A trusted bot gets the same
  teammate view any human in that channel already gets.
- **Identity:** the claimed asker is namespaced under the sending bot
  (`relay-<bot>-<actor>`), so a relay can never claim the owner, and it gets its
  own relay session.
- **Loop guards:** only asks are answered and an answer is never treated as an
  ask. A bot never asks on its own, only for a human. The trailer counts only as
  the last line of a message the bridge composed: model output has relay markers
  neutralized before posting. At most 6 asks per sending bot per minute.
- **Old form:** `<@id> ⟦mybot ask …⟧ question` is still accepted.

`scripts/run_test_sibling_bridge.sh` runs a second Discord bot against the same
server for a live round trip (with `MYBOT_TEAM_CHANNEL=false`, so only the
primary bridge drives the Channel tab).

## Email: a mailbox per mybot

Each teammate's mybot can have its own address (`mybot-<name>@<team domain>`).
The owner writes to it, or replies to anything it sent, and the answer comes back
in the same thread — the owner's own view, like the menu app. It is a channel
for the owner only: nobody else's mail is answered, and the bot can email no
one but its owner.

- **One Worker for the team** (`mail-worker/`, Cloudflare Email Routing). Each
  mailbox is a KV entry with its owners and the hash of that mybot's token.
  The Worker queues only mail whose From is the mailbox's owner; each mybot
  polls its own queue over HTTPS, so nothing on anyone's Mac is exposed.
- **Trust is checked again locally.** From lines are easy to forge, so mybot
  answers only mail carrying a valid DKIM signature from the sender's own
  domain, and skips auto-generated mail (no reply loops).
- **Sending** goes through the Worker's `send_email` binding: Cloudflare sends
  only to verified destination addresses, and the Worker only to that
  mailbox's owners.
- **Attachments** are saved to `~/Downloads` (or matched to the identical file
  already there) and referred to by path rather than sent back.
- `BRIEFING_EMAIL=true` also emails the morning pulse, figures inline.

Adding someone (needs wrangler on the account that owns the domain):

```bash
python scripts/mail_mailbox.py add mybot-alex@example.org --owner alex@gmail.com
```

This stores the mailbox, routes the address to the Worker, registers the
owner's address (Cloudflare sends them a one-time verification link) and
prints the four `MYBOT_MAIL_*` lines for their `.env`. Re-running rotates the
token.

## Connecting a chat platform (guided setup)

The Discord/Slack app setup is fiddly, so there's a validating doctor:

```
python scripts/mybot_admin.py connect discord   # or: connect slack
```

It prints the exact click-path, takes the pasted token(s), **validates them
live** (Discord `GET /users/@me`; Slack `auth.test` + `apps.connections.open`),
writes `.discord.env` / `.slack.env` (0600; won't clobber without `--force`,
which backs up first), and prints the next command. Non-interactive:
`--bot-token`, `--app-token`, `--channels`, `--force`.

## Naming: `<handle>-mybot`

Each teammate runs their own instance, so in a shared server they must be
distinguishable. The convention is **`<owner-handle>-mybot`** (e.g.
`alex-mybot`), derived from the discovered owner identity (shortest
username-like alias, else a display-name slug). `/health` exposes `bot_name` /
`bot_handle`; the Discord bridge auto-sets its per-guild nickname to it on ready
(`DISCORD_SET_GUILD_NICKNAME`, needs the Change Nickname permission). Slack can't
rename at runtime, so the doctor prefills `<handle>-mybot` as the app name at
creation. Override with `BOT_HANDLE` or reshape with `BOT_NAME_TEMPLATE`
(default `{handle}-mybot`).

## Context management

Conversation context is a short, disposable working set; durable memory lives in
the trajectory index, the person registry, and promoted `!remember` notes. The two
are kept separate so sessions can reset without losing continuity.

- **Session routing** (`SessionStore.resolve_active_session`): a stable *logical*
  key per surface (DM / channel / thread / `menuapp`) points at a rolling *active*
  storage key via `state/session_pointers.json`. When a conversation goes idle the
  active session rolls over to a fresh one, and a one-line **carry-forward** (the
  prior summary, or a gist of recent turns) seeds the new session so continuity
  survives (`## Continuing From Earlier` in the prompt).
- **Idle vs topic-shift** (DM/menu): a follow-up query tolerates the full idle
  window (`SESSION_IDLE_ROLLOVER_SECONDS`, 6h); a topic-shift (not a follow-up by
  `looks_like_followup`) rolls after the shorter grace period
  (`SESSION_TOPIC_SHIFT_SECONDS`, 15m). So rapid multi-part questions cohere, but an
  unrelated question after a break starts clean. This replaces the old perpetual
  `menuapp` blob.
- **Threads get their own session** on every surface (Discord threads, Slack
  `thread_ts`), which is the natural conversation atom.
- **Durable promotion**: on compaction, single-actor sessions promote the rolling
  gist into the person registry (`conversation_gist`), surfaced next time as "What
  you've discussed with them before". Long-term memory no longer depends on an
  unbounded transcript.
- **Group channels stay bounded**: no idle rollover (channels are ongoing), no
  rolling summary (it would conflate parallel threads) — only the last
  `GROUP_CONTEXT_WINDOW_SECONDS` (12h) of messages is loaded, and the observe log
  rotates past `GROUP_OBSERVE_MAX_MESSAGES` (400). Deep channel history is not the
  durable memory.

## Slack

Built as a Socket Mode bridge (`app/slack_bridge.py`, launcher
`run_slack_chatbot.sh`, tokens in `.slack.env` — see `.slack.env.example`). It is a
transport-neutral twin of the Discord bridge and needs **no server changes**:

- `slack_user_id → actor_id`; `slack-user-<id>` user key.
- DM → `slack-dm-user-<id>`; channel → `slack-<team>-channel-<id>`; thread →
  `slack-<team>-thread-<thread_ts>` (thread-as-session). Channel replies post in a
  thread to keep channels tidy.
- `channel_kind` = `dm` for IMs, `group` for channels/groups/mpim; `channel_label`
  from `conversations.info`; `actor_display_name` from `users_info` (both cached).
- Addressed (DM, or bot mentioned) → `/chat`; otherwise non-addressed channel
  messages → `/sessions/observe` (when `SLACK_OBSERVE_CHANNELS=true`). `<@id>`
  mentions resolve to `@Name`; the bot's own mention is stripped.
- Commands mirror Discord: `!iam <name>`, `!remember`, `!remember-shared` (owner
  only), `!new`/`!reset` (DMs only — a channel's conversation is shared). An `eyes`
  reaction acknowledges receipt while the answer runs.
- Who it talks to: DMs, plus only the channels in `SLACK_ALLOWED_CHANNEL_IDS`
  (empty = DMs only; being invited to a channel is not consent to answer it) and
  the team channel connected in the menu app, and
  only full members of its own workspace — no Slack Connect users from other
  organizations, no guest accounts unless `SLACK_ALLOW_GUEST_ACCOUNTS=true`. It
  observes only allowed channels.
- Runs `App(bot_token)` + `SocketModeHandler(app_token)` — no public URL. The
  launcher shares the chat server (won't start a second one if healthy) and uses its
  own single-instance lock, so Discord and Slack can run side by side.

The owner's `actor_id` differs per platform. Add the owner's Slack user id to
`MYBOT_OWNER_ACTOR_IDS` (comma-separated) so the same person is the owner on
Slack too — otherwise a Slack owner is treated as a teammate.

## New/changed endpoints & fields

- `POST /chat` (and `/chat/stream`): `actor_display_name`, `channel_kind`,
  `channel_label`, `author_label` (defaults to display name).
- `POST /sessions/observe`: `{actor_id, actor_display_name, author_label, user,
  session_key, message}` → appends an attributed user turn, updates the person
  registry, returns `{ok, session_key}`.
- Env: `OWNER_DISPLAY_NAME`, `TEAM_NAME`, `GUEST_OWNER_ACCESS`,
  `DISCORD_GROUP_SESSIONS`, `DISCORD_OBSERVE_CHANNELS`.
