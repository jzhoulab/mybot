<!--
  Who the assistant is working for. Injected into the system prompt so answers
  land in the right context — the owner's field, systems, active projects, and
  how they like to be answered.

  You do not have to write this by hand. Once the index has content, mybot can
  draft it from your own trajectories:

      curl -sS -X POST http://127.0.0.1:8788/owner/profile \
        -H 'Content-Type: application/json' -d '{"action": "generate"}'

  Review the draft, then save it with {"action": "save", "text": "..."} — or
  just edit this file. Keep it to 4-8 short bullets; it is prompt budget, not
  documentation. Treat it as private: it describes a real person.
-->

- **Role and domain** — what the owner does, and the field they work in.
- **Systems they work on** — the main codebases, services, or models, including
  the naming conventions that show up in their sessions.
- **Active projects and tools** — what is in flight right now, and the tooling
  they reach for.
- **Environments** — where work actually runs (laptop, a cluster, cloud), since
  questions often turn on which one.
- **Working style** — how they want answers: level of detail, tolerance for
  hedging, what counts as evidence.
