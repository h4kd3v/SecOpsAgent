# SecOps Agent

A ChatGPT-style web app for security analysts. It talks to a LiteLLM proxy,
runs tools on a remote SecOps MCP server on the model's behalf, and persists
every prompt, response and tool invocation to Postgres.

No login. ~20 analysts on a single Docker Compose stack, sharing one Google
service-account credential.

---

## Architecture

```
   Browser (React SPA)
        │   chat: SSE over fetch          sidebar: EventSource /api/events
        │   anonymous signed cookie
        ▼
   nginx  ──────────────►  FastAPI backend
   static assets            │
                            ├── agent loop ──► LiteLLM proxy
                            │              └─► MCP manager ──► SecOps MCP server
                            │                      ▲
                            │                      └── Google token manager (sa.json)
                            └── Postgres  (transcripts, audit trail, LISTEN/NOTIFY)
```

Three containers: `frontend` (nginx + built SPA), `backend` (FastAPI/uvicorn),
`postgres`. No Redis and no queue — Postgres already provides the pub/sub the
live sidebar needs. See [Scaling](#scaling).

### Identity: anonymous, but not untracked

There is no registration, no password, no user table. The first request mints
an `anon_sessions` row and returns a signed cookie holding its id. That cookie
is the only identity in the system, and it exists for exactly one reason: so a
visitor's sidebar shows *their* conversations rather than everybody's.

- Every conversation, message and tool invocation is attributed to a session id.
- Each session gets a short label (`Analyst 4f2a`) so audit rows are readable.
- Clearing cookies starts a fresh session. The old rows survive for audit; they
  just become unreachable from the UI.
- Because the cookie is signed with `SECRET_KEY`, one browser cannot claim
  another's history by editing a cookie value.

### Google service-account token lifecycle

One `sa.json`, one token, all analysts, 60-minute TTL
([google_auth.py](backend/app/services/google_auth.py)):

| Concern | Handling |
|---|---|
| Expiry | Refreshed `GOOGLE_TOKEN_REFRESH_SKEW` seconds (default 300) *before* expiry, so no request races the boundary |
| Stampede | A lock with a re-check inside it means twenty analysts hitting a cold cache produce **one** token exchange, not twenty |
| Long-lived sessions | `Authorization` is stamped per-HTTP-request by an httpx auth flow, never frozen into the MCP session at connect time — so a session outlives the token inside it |
| Belt and braces | The MCP transport session is recycled every `MCP_SESSION_MAX_AGE` (2700s), inside the token lifetime, for servers that pin a session to its opening credential |
| Bad credentials | Fetched once at startup, so a missing or malformed `sa.json` is logged loudly at boot rather than surfacing as a confusing tool error later |
| Observability | `/api/health/ready` reports `google_token_seconds_remaining`; it should sawtooth between ~3600 and ~300, never sit at 0 |

`GOOGLE_TOKEN_TYPE` picks what gets minted: `access_token` (OAuth2 bearer
scoped to `cloud-platform`, for the SecOps APIs) or `id_token` (signed JWT for
a `GOOGLE_ID_TOKEN_AUDIENCE`, for a Cloud Run-hosted MCP server).

### SecOps MCP headers

Every single MCP request carries all four required headers:

| Header | Source |
|---|---|
| `Authorization: Bearer …` | Token manager, injected per request (rotates hourly) |
| `Project-Id` | `SECOPS_PROJECT_ID` |
| `Region` | `SECOPS_REGION` |
| `Customer-Id` | `SECOPS_CUSTOMER_ID` |

The three static ones are attached to the transport's HTTP client
([mcp_manager.py](backend/app/services/mcp_manager.py)), so they apply to the
handshake, `tools/list` and every `tools/call` alike.

### The agent loop

[agent_loop.py](backend/app/services/agent_loop.py) is the heart of the system
and is deliberately free of FastAPI and HTTP concerns, so it can be tested
against a fake LLM and a fake MCP server.

```
user message
  └─► persist, then loop (max LLM_MAX_TOOL_ITERATIONS rounds):
        1. stream a completion with the cached MCP tool schemas
        2. persist the assistant turn as tokens arrive
        3. no tool calls?  -> done
        4. read-only tools -> execute now, persist results, loop
        5. write tools     -> park the turn, emit approval_required, stop
```

A parked turn resumes through `POST /api/conversations/{id}/approvals`. All
state lives in the database, so resuming is just "load the transcript and keep
going" — nothing is held in memory between requests.

**Invariant:** every `tool_call` in an assistant message must have a matching
`tool` message before the next completion, or the proxy rejects the request.
That's why an unapproved write parks the whole turn instead of skipping ahead.

### Tool safety

With anonymous access there is no role hierarchy, so the approval gate is a
**confirmation step, not an authorisation boundary**: any analyst can approve,
but nothing state-changing reaches SecOps without a human clicking approve.

| Layer | Behaviour |
|---|---|
| Classification | Server annotations (`read_only_hint`, `destructive_hint`) first, then operator regexes, then **fail closed** — unknown tools count as writes |
| Approval gate | Write tools park the turn and surface their arguments in the UI for review |
| Audit | Every invocation — approved, denied, failed — is a row in `tool_invocations` with arguments, result, latency, and the session that asked |

Set `REQUIRE_APPROVAL_FOR_WRITE=false` to disable the gate. Don't.

### MCP session handling

The session is long-lived and pinned to a dedicated task, not opened per
request. Two reasons:

1. A handshake plus `tools/list` on every message is pure latency tax.
2. The MCP SDK is anyio-based. Entering its context in a request task that then
   gets cancelled — exactly what a browser disconnect does mid-SSE — raises
   `Attempted to exit cancel scope in a different task`. Pinning the session to
   one owning task and talking to it over a queue removes that whole class of
   bug.

### Real-time sidebar

The sidebar updates the instant a conversation is created, retitled by the
auto-titler, touched by a new message, or archived — including in the
analyst's other tabs.

This runs on Postgres `LISTEN`/`NOTIFY`
([events.py](backend/app/services/events.py)), not an in-process event bus.
Postgres is already a required dependency and already has pub/sub, so it costs
no new infrastructure — and unlike an in-process bus it stays correct with more
than one uvicorn worker, since a NOTIFY reaches every backend process. Payloads
are published inside the caller's transaction, so they are delivered on COMMIT
and a client can never be told about a row that later rolls back.

The browser consumes it with `EventSource`, which handles reconnect-with-backoff
for free. Each connection opens with a `resync` frame so a reconnect after
downtime can't leave a stale list.

### Tool catalogue cache

Tool definitions change on the SecOps team's release cadence, not per request,
so they are persisted in `mcp_tool_catalog` and re-fetched only once older than
`TOOL_CACHE_TTL_HOURS` (default 24). Three properties make this safe:

- **Survives restarts and session recycles.** The MCP transport session is
  rebuilt every 45 minutes for token rotation; the catalogue is not.
- **Stale beats nothing.** If a refresh fails and a cached copy exists, the
  stale copy is served with `stale: true` and the reason, instead of failing.
  A brief SecOps outage stops nobody from working.
- **Filtering and classification happen on read.** Only the server's raw
  definitions are cached, so changing `TOOL_ALLOWLIST`, `TOOL_DENYLIST` or
  `TOOL_READONLY_PATTERNS` takes effect immediately with no refetch.

The cache is keyed by MCP server URL, so repointing `MCP_SERVER_URL` cannot
serve another server's tools. `POST /api/tools/refresh` forces a re-fetch, and
the **Refresh** button in the MCP tools panel does the same.

The agent loop reads tool definitions from the catalogue too, so a turn can
*plan* tool calls without a live MCP session — only executing them needs one.

**This does not reduce token cost.** Cached or not, the schemas are sent to the
model on every completion; against real Chronicle that is ~79k tokens for 70
tools. `TOOL_ALLOWLIST` is the lever for cost — narrowing to ten tools cuts it
by about 90%.

### Chat history and token accounting

**A conversation is created when the first message is sent, not when the "New
investigation" button is clicked.** The button only clears the view — the
sidebar shows an *unsaved* draft row — so clicking it repeatedly leaves nothing
behind. A conversation is a container for messages; an empty one is a row
nobody asked for. Any that do appear (a first turn that failed between create
and send) are swept at startup once older than
`EMPTY_CONVERSATION_TTL_HOURS`; the sweep keys on emptiness, never on age
alone, so a year-old thread with a transcript is never touched.

Every conversation is a row in `conversations` with its full transcript in
`messages`, so clicking any thread in the sidebar reloads it in place —
messages, tool cards with their arguments and output, and the token figures.
Nothing is held in browser memory; the sidebar click is a plain
`GET /api/conversations/{id}`. A thread scoped to another visitor's session
returns 404 rather than 403, so the id space isn't a confirmation oracle.

Under each assistant answer the UI shows the model the gateway actually served
and what the turn cost:

```
Claude Opus 4.6 · 1,394 tokens
```

- The **model** is whatever LiteLLM reports in the response, not the alias that
  was requested — so an aliased route records what really ran. Set
  `LLM_MODEL_DISPLAY_NAME` for a branded label; leave it empty to show the raw
  gateway id.
- **Usage** comes from `stream_options.include_usage`. Not every proxy honours
  that on streamed responses, and a blank figure looks like a bug, so the
  backend falls back to a ~4-chars-per-token estimate and marks it `estimated`.
  The UI then renders `~1,394 tokens`, never passing a guess off as a
  measurement. Hovering shows the in/out split.
- A running total for the whole thread sits above the composer, which is what
  you want for tracking spend across ~20 analysts.

A turn that uses tools makes several gateway calls, so it produces several
assistant rows — each carries its own usage, and they sum into the total.

### Data model

| Table | Purpose |
|---|---|
| `anon_sessions` | A browser, not a person. Label, first seen, last seen |
| `conversations` | Threads, archived rather than deleted |
| `messages` | Full transcript, 1:1 with the OpenAI wire format (`content` is nullable — a tool-only assistant turn has no text) |
| `tool_invocations` | The SOC audit trail: arguments, results, status, latency, requesting session |
| `audit_events` | Approval requests, denials, executions, archivals |

Rows are written as the turn progresses, not at the end, so a dropped
connection still leaves a complete and replayable record.

---

## Try it now, with no credentials

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml up --build
```

Open <http://localhost:8080>. No `.env`, no `sa.json`, no LiteLLM proxy, no
SecOps access. Ask it *"Any traffic from 10.14.7.22 today?"* to watch a
read-only tool run and the answer stream in, or *"Close case AL-4471"* to see
the approval gate hold a write until you click.

Only the LLM proxy and the MCP server are simulated
([demo.py](backend/app/services/demo.py)). Postgres, the migrations, SSE
streaming, the tool loop, the approval gate, the audit trail and the live
sidebar are all the real code — so this doubles as a smoke test of the stack
before you wire up the real services. Every answer is invented; never layer
this overlay on a real deployment.

## Testing against real services

Point the app at a real LLM and a real MCP server without a service-account
key. Create `.env` with just:

```bash
APP_ENV=dev
SECRET_KEY=anything-for-local-testing

# Any OpenAI-compatible endpoint, including OpenAI itself
LLM_PROXY_URL=https://api.openai.com/v1
LLM_API_KEY=sk-...
LLM_MODEL_NAME=gpt-4o

MCP_SERVER_URL=https://your-mcp-server/mcp
MCP_AUTH_MODE=none          # or static_token, or service_account
```

then `docker compose up --build` (no `-f docker-compose.demo.yml`).

**Check connectivity before touching the UI:**

```bash
docker compose exec backend python -m app.diagnose
```

It prints the loaded config, tries a real completion against the LLM proxy,
completes an MCP handshake, and lists every tool with its read/write
classification — with the root cause printed when a step fails. The
**MCP tools** button in the sidebar shows the same list in the UI.

`MCP_AUTH_MODE` decides how `Authorization` is produced:

| Mode | Use when |
|---|---|
| `service_account` | Production. Mints and rotates a Google token from `sa.json` |
| `static_token` | Testing. `MCP_STATIC_TOKEN` used verbatim — e.g. `gcloud auth print-access-token`, valid ~60 min, never refreshed |
| `none` | The server needs no bearer |

`Project-Id`, `Region` and `Customer-Id` are sent only when set, so an
unset one is omitted rather than sent blank. The real SecOps MCP server
rejects calls without them, so they warn at startup rather than blocking it —
that way the app can be pointed at any MCP server for a smoke test.

**If MCP is unreachable, chat still works.** The turn continues with no tools,
the UI shows an amber banner, and the model is told it has no tools so it says
so instead of inventing SecOps data. That means the LLM leg can be tested
before MCP is wired up.

## Running it for real

```bash
cp .env.example .env
$EDITOR .env                     # LiteLLM proxy, MCP URL, SecOps ids, secret key

cp /path/to/sa.json secrets/sa.json   # the folder is mounted at /run/secrets

docker compose up --build
```

Open <http://localhost:8080> and start typing. No sign-in step.

### What Docker sets up for you

Nothing to install by hand.

- **Postgres** comes from the `postgres:16-alpine` image. On *first* boot it
  finds an empty data directory and initialises a cluster, creating the user,
  password and database from `POSTGRES_USER` / `POSTGRES_PASSWORD` /
  `POSTGRES_DB`. That data lives in the named volume `pgdata`, so it survives
  `docker compose down`. Changing those variables later has **no effect** until
  you `docker compose down -v`, which destroys the volume and all chat history.
- **The port is not published.** Postgres is reachable only as the hostname
  `postgres` on the compose network. To inspect it:
  `docker compose exec postgres psql -U secops -d secops_chat`.
- **Migrations** run in [entrypoint.sh](backend/entrypoint.sh) (`alembic
  upgrade head`) before uvicorn starts, so the tables exist on first request.
- **Ordering** is enforced with a real `pg_isready` healthcheck, not a sleep:
  the backend waits for `service_healthy`, and the frontend waits for the
  backend's own healthcheck.
- **`./secrets` is mounted as a directory**, not as `sa.json` directly — a bind
  mount of a missing file silently creates a *directory* with that name and
  fails confusingly. An empty `secrets/` just means no key yet.

### Local development

```bash
# backend
cd backend
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload

# frontend (proxies /api to localhost:8000)
cd frontend && npm install && npm run dev
```

### Tests

```bash
cd backend
.venv/bin/pytest tests -q                      # unit tests only

docker run -d --rm --name pg -e POSTGRES_PASSWORD=test -e POSTGRES_USER=test \
    -e POSTGRES_DB=test -p 55433:5432 postgres:16-alpine
TEST_DATABASE_URL='postgresql+asyncpg://test:test@localhost:55433/test' \
    .venv/bin/pytest tests -q                  # + integration tests
```

144 tests covering the things that actually bite:

- **Streaming, through the real ASGI app over HTTP** — all 500 chunks of a long
  answer arrive (not just the first), content containing raw newlines, blank
  lines, `data:` prefixes and unicode survives SSE framing intact, the second
  completion *after* a tool call streams in full too, and the stored transcript
  matches byte-for-byte what was streamed live.
- **Tool fidelity** — a 200 KB MCP result reaches the model uncut.
- **Every round of a multi-tool turn** — the model writes, calls a tool, reads
  the result and writes again; all of it reaches the browser in arrival order,
  not just the first round and the final answer. Reasoning deltas, where the
  gateway exposes them, are streamed and stored but never replayed to the
  model, and the tool query streams as the model composes it.
- **Refreshing mid-turn** — the open thread is in the URL, so a reload reopens
  it; the reload is treated exactly as Stop, and the conversation comes back
  with the partial answer and a note saying it was cut short. A conversation id
  that no longer resolves falls back to a new chat rather than a dead screen.
- **Transcript size** — a thread with a 400 KB tool result reloads in ~3 KB;
  the full output is fetched only when an analyst opens it, and only for the
  conversation and session that owns it.
- **Capacity guards** — the rate limiter sheds idle keys instead of growing for
  the life of the process, readiness probes are cached so an unauthenticated
  endpoint cannot be used to hammer SecOps and the LLM gateway, and startup
  refuses to pretend the connection pool fits when workers x pool exceeds
  Postgres' max_connections.
- **Twenty analysts at once** — twenty turns genuinely concurrent over real
  cookies: no stream, transcript or sidebar carries another analyst's work,
  another session gets a 404 rather than a thread it does not own, the event
  bus fans out only to the owning session, and the single shared Chronicle
  session pairs each reply with its own caller when replies finish out of
  order.
- **Stopping a turn** — cancelling the response generator (what Starlette does
  on disconnect) cancels the completion and every in-flight MCP call, keeps the
  partial answer already on screen, and leaves no `tool_call` without a reply —
  an orphan there makes every *later* turn in that conversation fail.
- **Nothing goes unrecorded** — a one-word prompt is stored before the model is
  called; a failed turn keeps its reason, its partial text and its place in the
  sidebar; the raw provider payload lands in `audit_events` while the analyst
  sees a sentence. Failures are shown to the analyst but never replayed to the
  model.
- Streamed tool-call reassembly from fragmented deltas.
- Writes parking for approval; denials recorded without execution; hallucinated
  tool names returning an error to the model instead of crashing the turn.
- Single-flight token refresh under 20 concurrent callers, and
  refresh-before-expiry.
- A real LISTEN/NOTIFY round trip, including cross-session isolation and
  rollback publishing nothing.

---

## What is stored, and how long it survives

Everything the app knows lives in one Postgres database, in the Docker volume
`secops-agent_pgdata`. The compose file pins the project name, so the volume
name does not change if the directory is renamed.

### The tables

| Table | One row per | Holds |
|---|---|---|
| `anon_sessions` | browser | the label shown in the sidebar, user-agent, source IP, first and last seen. No name, no email, no password — there is no registration |
| `conversations` | thread | title, owning session, archived flag, created/updated, running token and cost totals, pinned flag, tags |
| `messages` | turn | role, who wrote it, the analyst's prompt or the model's answer, the model's reasoning, the tool calls it asked for, why a turn failed, token usage, model id, ordering |
| `tool_invocations` | MCP call | tool name, the exact arguments the model sent, the full result, error, status, whether it was a write, latency, and which session it belonged to |
| `audit_events` | security event | tool executed, tool denied, approval requested, conversation archived, completion failed (with the raw provider payload), MCP unreachable, tools refreshed |
| `message_feedback` | analyst, per answer | thumbs up or down and an optional note — one vote each, changeable |
| `mcp_tool_catalog` | MCP server | the cached tool definitions and when they were fetched |
| `alembic_version` | database | which migration the schema is at |

### One shared workspace

`SHARED_WORKSPACE=true` (the default) puts every analyst in one workspace:
every conversation appears in every sidebar, newest first, labelled with who
started it. A shift working one incident together beats twenty private
transcripts of the same investigation.

The trade is explicit: **there is no privacy between analysts.** Anything typed
into this app is visible to all twenty. Set `SHARED_WORKSPACE=false` for
per-browser history instead — the isolation tests cover that mode too.

Three rules make sharing safe rather than merely open:

* **Reading and contributing are shared; removing is not.** Anyone can add to
  any thread, but only the analyst who started one can rename or archive it.
  One mis-click should not take somebody's investigation out of nineteen
  sidebars.
* **Every question keeps its asker.** `messages.author_session_id` records who
  typed it, so a handover mid-thread stays legible.
* **One turn at a time per thread.** Two analysts sending into the same
  conversation at once would both claim the same position in the transcript;
  the second gets a 409 telling them to wait or start a new chat, rather than
  a 500.

### Prompts and tokens

Every prompt is a row in `messages` with `role = 'user'` and the text in
`content` — stored before the model is called, so a question survives even when
the turn that followed it failed.

Tokens are recorded twice, on purpose:

* **Per turn**, in `messages.token_usage` — `{prompt_tokens,
  completion_tokens, total_tokens, estimated?}`. This is what the UI shows
  under each answer.
* **Per thread**, in `conversations.prompt_tokens / completion_tokens /
  total_tokens`, folded in as each turn commits — the same transaction as the
  message it counts, so the two cannot drift.

### Cost

With `LLM_MODEL_PRICING` set — `gpt-4.1=2.00/8.00,claude-opus-5=5.00/25.00`,
in dollars per million tokens — each turn records what it cost, and the rate it
was charged at:

```json
{"prompt_tokens": 1840, "completion_tokens": 96,
 "input_rate_per_1m": 5.00, "output_rate_per_1m": 25.00}
```

Storing the rate is the point. Published prices change, and a cost recomputed
later from today's prices is not what was actually spent. A model with no
configured rate records no cost rather than a wrong one — a gap an operator can
see and fill beats a plausible number that is wrong. Versioned ids resolve to
their family (`gpt-4.1-2025-04-14` is billed at `gpt-4.1` rates), longest
prefix winning, so `gpt-4.1-mini` is never billed as `gpt-4.1`.

### Titles, pins and tags

A thread is named from its first prompt as that message is written — not from a
model call after the turn, which is why every thread whose first turn failed
used to stay "New conversation" for good. Any analyst can rename one, and the
new name is stored and broadcast to the other sidebars.

Pinned threads sort above the date groups; twenty analysts sharing one sidebar
bury the good investigations within a week. Tags are free-form labels, trimmed
and de-duplicated on write — an incident number is just a tag by convention
(`INC-4471`), which keeps one mechanism instead of two.

The rollup exists because the per-turn rows answer "what did this thread
cost?" but not "what did this analyst cost last month?" without reading every
message in the database:

```sql
SELECT s.label AS analyst,
       count(c.id) AS threads,
       sum(c.total_tokens) AS tokens
FROM anon_sessions s
JOIN conversations c ON c.session_id = s.id
WHERE c.created_at >= date_trunc('month', now())
GROUP BY s.label
ORDER BY tokens DESC;
```

A multi-round turn is counted per round, because that is how it is billed: the
prompt is re-sent each time the model calls a tool. A turn that failed before
reaching the model adds nothing. `usage_estimated` marks a thread whose total
includes an approximation, because some gateways omit usage on streamed
responses — those totals are a floor, not a measurement.

Two things are deliberately kept apart from `content`: `reasoning`, so the
model's working is shown as working rather than as the answer, and `error`, so
a failure is styled as a failure. Neither is replayed to the model.

Nothing is ever hard-deleted by normal use. Archiving sets a flag — the tool
invocations under a thread are an audit record and outlive the analyst's
interest in the chat. The only automatic deletion is the startup sweep of
conversations that never received a message.

### What survives what

Verified on this stack, not assumed:

| Event | Chats survive? |
|---|---|
| `docker compose restart` | yes |
| container killed (`docker kill`, OOM, crash) | yes |
| `docker compose down` then `up` | yes |
| `docker compose up --build`, image rebuilds | yes |
| host reboot | yes |
| **`docker compose down -v`** | **no — this deletes the volume** |
| `docker volume rm secops-agent_pgdata`, `docker volume prune` | no |
| disk failure, rebuilt machine | no |

Shutdown is clean: the Postgres image stops on `SIGINT`, which is a fast
shutdown with a checkpoint, so a normal stop does not leave the cluster in
recovery.

`down -v` is the one command that destroys chat history. There is no
confirmation prompt — it is `down` with two extra characters.

### Backups

A volume survives restarts; it does not survive `down -v`, a pruned volume, or
a dead disk. For that there are dumps:

```bash
./scripts/backup-db.sh              # timestamped dump into ./backups
./scripts/backup-db.sh /mnt/nas     # somewhere that is not this disk
./scripts/restore-db.sh backups/secops-chat-20260817-014744.sql.gz
```

The dump includes `alembic_version`, so a restore lands on a schema the app
agrees with. `backups/` is gitignored: those files contain every prompt, every
answer and every row SecOps returned.

Nightly, via cron:

```
0 2 * * * cd /path/to/SecOpsAgent && ./scripts/backup-db.sh >> backups/cron.log 2>&1
```

An untested restore is not a backup. `restore-db.sh` stops the backend, replays
the dump and starts it again; try it once against a copy before you need it.

## Operational notes

**Streaming.** SSE, not WebSockets — one-way token flow, survives corporate
proxies, reconnects for free. `proxy_buffering off` in nginx is required or
tokens arrive in one lump at the end.

**Health.** `/api/health/live` is process-only (used by the container
healthcheck, so a flaky MCP server can't restart-loop the backend).
`/api/health/ready` checks Postgres, the event bus, MCP, the LLM proxy, and
remaining token lifetime.

**Secrets.** `sa.json` is mounted read-only and never baked into the image.
`LLM_API_KEY` stays server-side and is never sent to the browser. Both `.env`
and `secrets/` are gitignored.

**Tool results reach the model in full.** `TOOL_RESULT_MAX_CHARS` defaults to
`0`, meaning no truncation: the model is the thing deciding which tools to call
and what the results mean, and it cannot reason about rows it was never shown.
A silently clipped result is how you get a confident answer drawn from half the
data. If you ever set a ceiling, the model is told explicitly that the result
was partial and to re-query with a narrower filter — it never has to guess.

**Context budget.** Replayed history is trimmed by two budgets:
`LLM_MAX_CONTEXT_MESSAGES` (count) and `LLM_MAX_CONTEXT_CHARS` (size), because
forty messages is not a meaningful limit when one of them is 500 KB. Trimming
drops the oldest end only, never orphans a `tool` message from its parent
assistant turn, and never touches the current turn's result — a big tool
payload only comes under pressure on *later* turns, by which point the model
has already read it.

**Truncated answers.** If a completion ends with `finish_reason: "length"` the
answer is genuinely incomplete, not a broken stream, and the UI says so in a
warning banner. `LLM_MAX_OUTPUT_TOKENS` is unset by default so the proxy's own
limit applies.

<a name="scaling"></a>
**Scaling.** The rate limiter is in-process, which is correct for a single
backend container (`UVICORN_WORKERS=2` by default). The event bus is *not* a
constraint — it works across workers and across containers. Before running
multiple backend replicas, move the rate limiter to Redis and add resumable
streams so a page refresh doesn't lose an in-flight turn.

---

## Known limitations

- **History is per browser.** Clearing cookies, or switching machines, means a
  new session and an empty sidebar. If analysts need history to follow them,
  that's the point at which you add real accounts (SSO) — the schema change is
  small: `anon_sessions` grows a nullable `user_id`.
- **One shared MCP identity.** SecOps sees every query as the service account,
  so its own RBAC and audit cannot distinguish analysts. The app's
  `tool_invocations` table is where per-analyst attribution lives.
- **Anyone can approve a write.** With no accounts there is nothing to check a
  role against. If tiered approval matters, it needs accounts first.

## Deliberately not built

RAG / vector search, multi-agent orchestration, a message queue, Kubernetes
manifests. Twenty analysts on one Compose stack is not a distributed systems
problem, and every one of these adds failure modes before it adds value.
