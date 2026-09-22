# RESUME — Autonomous Career-Agent build (for the scheduled cloud agent)

## STANDING DIRECTIVES (highest priority — owner's explicit ask)
1. **Build the FULL system to completion**, production-ready — all four agents
   (job finder, response builder, job applier, response analyzer) plus project
   builder and observability. Do not stop at partial; keep going through every
   item until the whole diagram is implemented and tested.
2. **Make the flow DIAGRAMMATIC.** `app/console.html` (served at `/`) is now a
   real inline-SVG node-and-edge diagram (see "Already built" below) — keep it
   that way as new stages/branches are added: every new lifecycle behavior
   should map onto a node (or a documented reason it doesn't need one), not
   silently regress back to a flat strip. Keep `/live` too.
3. **Self-perpetuation:** work is on branch/commits pushed to this repo. If you
   run low on capacity, commit + push what you have and leave a clear updated
   RESUME so the next run continues. (Re-arming the scheduled cloud routine is
   done by the owner's LOCAL Claude Code session, which has the scheduler; this
   cloud run cannot reschedule itself — just push and summarize.)

You are continuing an in-progress build. Read this fully, then continue the work,
commit, and push. You run in an **isolated cloud checkout** — there is NO local
Docker/Temporal, no DeepSeek key, and no local database here. So: build code and
**unit tests** (they run on SQLite without Temporal), commit, and push. Do not add
secrets. Keep the system **safe-by-default** (no real external submissions).

**IMPORTANT safety note from the last run:** this sandbox's own process
environment has an ambient `GITHUB_TOKEN` (used by Claude Code's own git/gh
tooling against this repo) — it is **not** an operator opt-in to the app's
GitHub-publishing feature, but `app/config.py`'s `Settings.github_token` reads
`GITHUB_TOKEN` generically, so it *will* pick it up. Any new test that arms
`LivePolicy(enabled=True, ...)` **must** patch `get_settings()` (see
`tests/test_project_builder.py`'s `setUp`) so it never depends on that ambient
value — one such test made a real (fortunately 403'd) call to
`api.github.com` before this was caught. Never assume `GITHUB_TOKEN` is unset
in this environment.

## What this project is
An autonomous career agent from the owner's diagram: **job finder → response builder
→ (project builder, if a gap is found) → job applier (apply + outreach) → response
analyzer**, plus **observability** (evolution/self-improvement of agent specs
already exists per-domain in `app/evolution.py` but is not the current focus).
Stack: FastAPI + Temporal + SQLAlchemy (Postgres/SQLite) + Playwright/chromium.
Principle: **Temporal/state-machine owns control flow; the LLM only does bounded
tasks.** Never fabricate candidate facts or project/contact history.

## Already built and working (tests pass: 32, all offline/SQLite)
- Durable lifecycle: `app/lifecycle.py` (`LifecycleState` — all 27 states,
  `ALLOWED_TRANSITIONS`, idempotent `record_transition`), `job_transitions` table.
  `JobLifecycleWorkflow` in `app/workflows.py` drives the full happy path:
  normalize → qualify (DeepSeek fit) → research company (chromium) → match →
  gaps → people → **project builder (conditional)** → tailor resume → create
  application → (if live) submit → **outreach draft** → stops safely at
  `application_ready` / `tracking` depending on whether live actions are armed.
- **Project Builder** (`build_project_activity` in `app/activities.py`): when
  the `resume_gaps` artifact says `project_recommended`, walks
  `PROJECT_PLANNING → PROJECT_BUILDING → PROJECT_TESTING → PROJECT_REVIEW →
  PROJECT_PUBLISHED → EVIDENCE_UPDATED` using the real file scaffold in
  `app/projects.py`. GitHub push requires **both** `GITHUB_TOKEN` and live
  actions (see safety note above); otherwise it's a local file scaffold only.
  Tests: `tests/test_project_builder.py`.
- **Job Applier — outreach** (`prepare_outreach_activity`): once an
  application is `APPLICATION_VERIFIED`, drafts an outreach message from the
  `contacts` artifact. The recipient is always the literal string
  `"pending-human-verification"` — contacts never include a real fabricated
  name/email, so this activity never sends anything itself; `attempt_send` in
  its result just reports whether the outreach policy *would* currently allow
  a send, for operator visibility. Actual sending stays behind the pre-existing
  `send_outreach_activity` (manual/API-triggered). Tests: `tests/test_outreach.py`.
- **Response Analyzer scaffold** (`app/response_analyzer.py`): normalizes
  Gmail replies → classifies intent (`_stage_from_reply`) → persists a durable
  `ConversationState` (one row per job, in `app/models.py`) → drives
  `TRACKING → RESPONSE_RECEIVED → RESPONSE_ANALYZED` and, only for an
  actionable intent (interview/positive-reply/offer), on through
  `ACTION_PLANNED → ACTION_EXECUTED → TRACKING`, raising an `Escalation` for a
  human rather than ever auto-replying. `app/integrations/channels.py` defines
  a `MessagingChannel` protocol plus LinkedIn/X/Twilio-SMS stubs that fail
  loudly with exactly what credentials they'd need — only Gmail is real.
  Tests: `tests/test_response_analyzer.py`.
- Observability: `app/events.py` (`agent_events`, `emit()`/`step()`, cost
  estimate); **diagrammatic** live UI at `GET /` (`app/console.html` — an
  inline-SVG node-and-edge diagram: Job Finder → Response Builder →
  [branch: Project Builder] → Tailor & Prepare → Job Applier → Response
  Analyzer, with live per-node counts, pulsing "active" nodes, click-to-filter,
  and a per-agent recent-activity panel) + `GET /live` (`app/live.html`,
  unchanged raw event feed); endpoints `/api/system/status`,
  `/api/events[/stream]`, `/api/jobs/{id}/detail` (now also returns `project`,
  `outreach`, and `conversation`).
- Response Builder research: `app/research.py` (chromium `WebResearcher` +
  DeepSeek company research, resume gaps, people-to-contact) saved as
  `Artifact` kinds `company_research` / `resume_gaps` / `contacts`.

## What to build next (in priority order) — with unit tests for each
The four-agents + project-builder + observability diagram from the owner's
brief is now implemented end-to-end at the scaffold level with tests. What's
left is depth, not structure:

1. **Actually execute the scaffolded project's tests** as part of
   `PROJECT_TESTING` (currently that transition is recorded but nothing is run
   — see the docstring on `build_project_activity`). Would need `pytest` added
   as a dependency and a subprocess call against the scaffolded repo; keep it
   optional/best-effort so a failure doesn't crash the activity, and unit-test
   both the pass and fail paths.
2. **A resolution path for `Escalation`s the Response Analyzer raises** —
   today `POST /api/escalations/{id}/resolve` exists generically, but there's
   no guided "what should I do about this positive reply" UI/flow. Consider a
   dedicated response on the console's Response Analyzer node-detail panel
   listing open `response_action` escalations for jobs currently at that node.
3. **A real contact-resolution step** so outreach can ever leave "draft":
   something that turns `contacts.target_roles` + `search_queries` into an
   operator-verified real recipient (a human types it in via a small
   `PATCH /api/outreach/{id}` endpoint, most likely — never auto-search/guess
   a real email). Only after that does it make sense to actually wire
   `prepare_outreach_activity` → `send_outreach_activity`.
4. **LinkedIn/X/Twilio-SMS**: real implementations behind the
   `MessagingChannel` protocol in `app/integrations/channels.py`, gated behind
   their own credentials exactly like Gmail — needs real API credentials the
   cloud sandbox doesn't have, so this is blocked until the owner provides them.
5. Wire `build_project_activity` and `prepare_outreach_activity` into
   `app/worker.py`'s Temporal registration in a real cluster and confirm the
   full `JobLifecycleWorkflow` replay against a live Temporal server (only
   possible outside this sandbox — there is no local Temporal here).

## How to work
- Make a branch `auto/overnight-<date>`, implement one item at a time, keep tests green.
- Run tests: `python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`
  then `MODEL_BASE_URL= MODEL_NAME= MODEL_API_KEY= DATABASE_URL=sqlite+pysqlite:///:memory:
  python -m unittest discover -s tests -q` (empty MODEL_* keeps tests offline/deterministic).
- Follow existing style: typed, small modules, idempotent activities, structured events,
  `@activity.defn` on every activity, register new activities in `app/worker.py` and
  import them in `app/workflows.py`.
- Any test that arms `LivePolicy(enabled=True, ...)` must also neutralize
  `get_settings()` (patch it, per `tests/test_project_builder.py`) rather than
  relying on env vars being unset — see the safety note above.
- If you change `app/console.html`'s `NODES`/`EDGES`, sanity-check it with a
  quick local `uvicorn` + seeded SQLite DB + Playwright screenshot before
  committing (see the last run's approach: seed a few jobs across different
  lifecycle states, load `/`, click a node, expand a job's detail panel,
  check for console errors). Don't ship a diagram change unverified.
- Commit each item separately and **push**. Open a PR to `master` if possible; otherwise
  push the branch. Summarize what you did in the final message and in the PR body.
- If blocked (missing creds/service), implement the code + tests behind a flag and
  clearly note the blocker instead of stopping.

Full state notes mirror the owner's local memory; this file is the source of truth for
the cloud agent.
