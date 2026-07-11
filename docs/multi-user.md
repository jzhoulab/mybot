# Multi-user operation (team deployment)

mybot is a **collaboration tool for any team** — a research lab, an engineering team, a
startup. Each team member runs their **own** mybot instance over their own trajectories.
The bot is its owner's **assistant and representative**: a plain, factual information
source over the owner's work that lets teammates get answers about it quickly ("what did
Alex do with the platform2 run?") without pinging the owner. Together the instances form a
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
  trajectory history through the bot: that is the collaboration feature. Whoever the
  Discord channel allowlist admits can read what the bot can read, so gate exposure
  with `DISCORD_ALLOWED_CHANNEL_IDS` and the scope/visibility controls at `/gui`.
  Set `GUEST_OWNER_ACCESS=false` to restrict trajectory lookup to the owner: guests
  then fall back to shared team memory + their own private memories + the current
  conversation (endpoints enforce 403 regardless of what the prompt advertises), and
  `!ask @owner <question>` remains the explicit owner-scope valve.

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

## Slack later

The server API is transport-neutral: a Slack bridge only needs to map
`slack_user_id → actor_id`, `channel → session_key`/`channel_label`, send
`actor_display_name`, call `/sessions/observe` for non-addressed channel messages,
and `/chat` when addressed. One caveat to solve then: `!ask @owner` should map the
Slack mention format, and the owner's `actor_id` will differ per transport — either
give the owner a canonical actor id at import time (current setup uses the Discord
id) or add an alias map in config.

## New/changed endpoints & fields

- `POST /chat` (and `/chat/stream`): `actor_display_name`, `channel_kind`,
  `channel_label`, `author_label` (defaults to display name).
- `POST /sessions/observe`: `{actor_id, actor_display_name, author_label, user,
  session_key, message}` → appends an attributed user turn, updates the person
  registry, returns `{ok, session_key}`.
- Env: `OWNER_DISPLAY_NAME`, `TEAM_NAME`, `GUEST_OWNER_ACCESS`,
  `DISCORD_GROUP_SESSIONS`, `DISCORD_OBSERVE_CHANNELS`.
