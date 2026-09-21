# RESUME — Autonomous Career-Agent build (for the scheduled cloud agent)

You are continuing an in-progress build. Read this fully, then continue the work,
commit, and push. You run in an **isolated cloud checkout** — there is NO local
Docker/Temporal, no DeepSeek key, and no local database here. So: build code and
**unit tests** (they run on SQLite without Temporal), commit, and push. Do not add
secrets. Keep the system **safe-by-default** (no real external submissions).

## What this project is
An autonomous career agent from the owner's diagram: **job finder → response builder
→ job applier → response analyzer**, plus **observability** (evolution is deferred).
Stack: FastAPI + Temporal + SQLAlchemy (Postgres/SQLite) + Playwright/chromium.
Principle: **Temporal/state-machine owns control flow; the LLM only does bounded
tasks.** Never fabricate candidate facts or project history.

## Already built and working (tests pass: 17)
- Durable lifecycle: `app/lifecycle.py` (`LifecycleState`, allowed transitions,
  idempotent `record_transition`), `job_transitions` table. `JobLifecycleWorkflow`
  in `app/workflows.py` drives: normalize → qualify (DeepSeek fit) → research company
  (chromium) → match → gaps → people → resume → create application (stops safely at
  `application_ready` when live actions off).
- Observability: `app/events.py` (`agent_events`, `emit()`/`step()`, cost estimate);
  live UI `GET /` (`app/console.html`) + `GET /live` (`app/live.html`); endpoints
  `/api/system/status`, `/api/events[/stream]`, `/api/jobs/{id}/detail`.
- Response Builder research: `app/research.py` (chromium `WebResearcher` + DeepSeek
  company research, resume gaps, people-to-contact) saved as `Artifact` kinds
  `company_research` / `resume_gaps` / `contacts`.

## What to build next (in priority order) — with unit tests for each
1. **Project builder wired into the lifecycle**: when `resume_gaps.project_recommended`
   is true, add lifecycle states PROJECT_PLANNING→…→EVIDENCE_UPDATED using the
   existing `app/projects.py` scaffold (real files, real content, no fake history);
   GitHub push stays behind `GITHUB_TOKEN` + live-actions (do NOT hardcode tokens).
   Add an activity + workflow branch + a unit test (SQLite) asserting the artifact
   and transitions. Emit `app/events` events for visibility.
2. **Job Applier — outreach** wired into the flow (draft outreach messages from
   `contacts`, gated by policy, off by default). Unit test the gating + records.
3. **Response Analyzer** scaffold: normalize inbound messages → intent → conversation
   state (persist a model), reusing `app/response_analyzer.py`; unit-test the intent
   classification and state transitions. X/LinkedIn/Twilio remain interfaces (need
   creds) — build the structure + Gmail path only.

## How to work
- Make a branch `auto/overnight-<date>`, implement one item at a time, keep tests green.
- Run tests: `python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`
  then `MODEL_BASE_URL= MODEL_NAME= MODEL_API_KEY= DATABASE_URL=sqlite+pysqlite:///:memory:
  python -m unittest discover -s tests -q` (empty MODEL_* keeps tests offline/deterministic).
- Follow existing style: typed, small modules, idempotent activities, structured events,
  `@activity.defn` on every activity, register new activities in `app/worker.py` and
  import them in `app/workflows.py`.
- Commit each item separately and **push**. Open a PR to `master` if possible; otherwise
  push the branch. Summarize what you did in the final message and in the PR body.
- If blocked (missing creds/service), implement the code + tests behind a flag and
  clearly note the blocker instead of stopping.

Full state notes mirror the owner's local memory; this file is the source of truth for
the cloud agent.
