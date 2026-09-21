# Career System v1 — Current State

_Last updated: 2026-09-22. This document maps the system exactly as it exists in
the repository today. It is descriptive, not aspirational — see
[`target-v1.md`](./target-v1.md) for where we are going and
[`v1-gap-analysis.md`](./v1-gap-analysis.md) for the delta._

## Summary

Career System v1 is a **clean, well-typed ~2,900 LOC prototype**. It is
**safe-by-default** (no external action fires until live policy is explicitly
armed) and it correctly keeps the LLM as a *bounded task executor*, not the
orchestrator.

Its defining characteristic today is that it is **operator-driven and
connector-based**: the job lifecycle advances only when a human calls an HTTP
endpoint for each stage. There is no durable state machine that carries a job
from discovery to application on its own. Temporal is present but used for three
narrow workflows only. Redis and MinIO are provisioned in Compose but are not
touched by application code.

## Runtime shape

Defined in [`compose.yaml`](../../compose.yaml):

| Service | Image / build | Role |
| --- | --- | --- |
| `api` | [`Dockerfile`](../../Dockerfile) | FastAPI control plane + operator API (port 8000) |
| `worker` | [`Dockerfile.worker`](../../Dockerfile.worker) | Temporal worker; adds Playwright + chromium |
| `postgres` | `postgres:16-alpine` | Authoritative business state |
| `redis` | `redis:7-alpine` | **Provisioned, unused by app code** |
| `minio` | `minio/minio` | **Provisioned, unused by app code** |
| `temporal` | `temporalio/auto-setup:1.25.2` | Durable workflow engine (port 7233) |
| `temporal-ui` | `temporalio/ui:2.26.2` | Workflow UI (port 8081) |
| `dashboard` | [`dashboard/`](../../dashboard) | React + nginx operator console (port 5173) |

Configuration is entirely environment-driven via
[`app/config.py`](../../app/config.py) (`pydantic-settings`), so each image is
independently deployable. The Helm chart in
[`deploy/helm/career-system`](../../deploy/helm/career-system) deploys
`api`/`worker`/`dashboard` and assumes managed Postgres/Redis/MinIO/Temporal.

## Control plane — [`app/main.py`](../../app/main.py) (498 LOC)

FastAPI app with CRUD + action endpoints. Prometheus is mounted at `/metrics`
with a **single** counter, `career_external_action_requests_total`
(labels: `action`, `result`).

The lifecycle is **HTTP-driven** — a human (or external script) drives every
stage one call at a time:

- `POST /api/sources/{id}/poll` — pull + normalize + dedup jobs
- `POST /api/jobs/{id}/qualify` — run qualification, produce an `Assessment`
- `POST /api/jobs/{id}/resumes` — render a tailored PDF
- `POST /api/jobs/{id}/projects` — write a project scaffold (optionally publish)
- `POST /api/jobs/{id}/applications` — create/queue an application
- `POST /api/jobs/{id}/outreach` — create/queue an outreach email
- `POST /api/outcomes/ingest-gmail` — poll Gmail threads for replies
- `POST /api/evolution/run`, `POST /api/agents/{id}/promote` — evolution
- `PUT /api/policy` — arm/disarm live actions and set daily caps

`create_application` embeds policy enforcement **and** Temporal workflow-start
logic directly in the endpoint (lines ~324–373). On workflow-start failure it
degrades the application to `ESCALATED` and writes an `Escalation` row — no
external action is performed, and the record stays recoverable.

## Durable state — [`app/models.py`](../../app/models.py) (259 LOC)

Postgres via SQLAlchemy 2.0 typed models. Tables:

- `candidate_profiles` — versioned JSON `facts`, `is_approved` flag; unique on `version`
- `job_sources` — provider + board URL (unique)
- `jobs` — normalized job; unique `fingerprint`; unique `(source_id, provider_job_id)`; `status` (`JobStatus`)
- `agent_specs` — champion/challenger policy genomes; immutable via unique `fingerprint`, `generation`, `parent_spec_id`
- `experiments` / `assignments` / `assessments` — A/B allocation + qualification verdicts + audit scores
- `artifacts` — resumes, project scaffolds; `uri` + `content_hash` + `provenance`
- `applications` — unique `idempotency_key`; `status` (`ApplicationStatus`); `browser_evidence_uri`
- `outreach_messages` — unique `provider_message_id`
- `outcomes` — lifecycle stage signals (`OutcomeStage`)
- `inbound_emails` — Gmail reply correlation; unique `provider_message_id`
- `escalations` — human-loop items (open/resolved)
- `cost_events` — token/model/USD capture (model exists; **rarely written**)
- `system_settings` — key/value; holds the live policy

Schema is created with `Base.metadata.create_all` in
[`app/db.py`](../../app/db.py) — **there are no migrations (no Alembic)**. The
engine is constructed at import time from settings.

### Status enums (the lifecycle vocabulary today)

- `JobStatus`: `discovered → qualified → skipped → prepared → applied → closed` (6 values)
- `ApplicationStatus`: `draft → blocked → ready → submitted → failed → escalated`
- `OutcomeStage`: `unknown → replied → positive_reply → interview → rejected → offer`
- `Domain`: `job_finder`, `response_builder`, `application` (agent taxonomy)

## Temporal — workflows, activities, worker, client

- [`app/workflows.py`](../../app/workflows.py) — three thin workflows:
  - `PollJobSourceWorkflow` → 1 activity
  - `ApplicationWorkflow` → `prepare` → `browser-submit`; on
    `needs_escalation`/`unavailable` it **waits for a human `resolve` signal**
  - `OutreachWorkflow` → 1 activity
- [`app/activities.py`](../../app/activities.py) — four activities:
  `poll_source_activity`, `prepare_application_activity`,
  `execute_application_browser_activity`, `send_outreach_activity`. They are
  `async def` but call **blocking sync `httpx`** (connectors) and sync-style
  Playwright work, which blocks the event loop under load.
- [`app/worker.py`](../../app/worker.py) — single task queue `career-system`
  for all workflows and activities.
- [`app/temporal_client.py`](../../app/temporal_client.py) — start/signal
  helpers used by the API.

There is **no orchestrating workflow** that owns a job's full lifecycle; each
workflow is a leaf action started on demand from an HTTP endpoint.

## Agents / tools (bounded — LLM as task, not orchestrator)

| Concern | File | Behavior |
| --- | --- | --- |
| Discovery | [`connectors/ats.py`](../../app/connectors/ats.py) | Greenhouse + Lever REST readers only → `NormalizedJob` |
| Dedup | [`services.py`](../../app/services.py) `fingerprint_job` | sha256(url+company+title) |
| Qualification | [`qualification.py`](../../app/qualification.py) | Model verdict via `ModelClient`; deterministic term-overlap fallback |
| Resume | [`resumes.py`](../../app/resumes.py) | reportlab PDF from approved facts; truthful |
| Project | [`projects.py`](../../app/projects.py) | Writes a **static** file skeleton; optional GitHub private-repo publish |
| Browser | [`browser.py`](../../app/browser.py) | Playwright fill+submit; escalates on unknown required field / missing file / unverifiable confirmation |
| Outreach | [`integrations/gmail.py`](../../app/integrations/gmail.py) | Gmail send + thread-scoped reply read |
| Response | [`response_analyzer.py`](../../app/response_analyzer.py) | Keyword → `OutcomeStage`; correlates by thread |
| Evolution | [`evolution.py`](../../app/evolution.py), [`bootstrap.py`](../../app/bootstrap.py) | GEPA-style LLM proposal + deterministic fallback; audit-gated promotion |
| Model | [`model_client.py`](../../app/model_client.py) | One OpenAI-compatible boundary; captures token usage |
| Safety | [`safety.py`](../../app/safety.py) | `LivePolicy` (enabled + 2 daily caps) gates every external action |

### Notable safety semantics (working well today)
- `enforce_application_policy` / `enforce_outreach_policy` block when live
  actions are off **or** the relevant daily cap is 0 or exhausted.
- The browser never invents answers: unknown required fields → escalation; a
  submit whose confirmation cannot be verified → escalation (not a retry).
- Project scaffolds are explicitly labeled as newly created, not prior
  employment — no fake history.

## Data reality

- **MinIO is unused.** Artifacts are written to the local `artifact_dir`
  filesystem and referenced as `file://…` URIs (see `resumes.py`,
  `projects.py`, `browser.py`). Object-store settings exist in `config.py` but
  nothing reads them.
- **Redis is unused.** No cache, lock, rate-limit, or budget code exists despite
  the service being provisioned.
- **Cost accounting is thin.** `cost_events` and `ModelClient` usage capture
  exist, but token usage is not consistently persisted.

## Frontend

[`dashboard/src/main.tsx`](../../dashboard/src/main.tsx) — a single-file React
console: add/poll sources, edit live policy, view recent jobs, view open
escalations, and run/inspect the evolution population. It calls the API through
an nginx `/api/` proxy ([`dashboard/nginx.conf`](../../dashboard/nginx.conf)).

## Tests

- [`tests/test_core.py`](../../tests/test_core.py) — 6 cases: bootstrap
  population, reproducible experiment assignment, policy blocking, evolution
  requires audits, heuristic qualification, resume PDF rendering.
- [`tests/test_response_analyzer.py`](../../tests/test_response_analyzer.py) —
  1 case: conservative outcome classification.

Both run against in-memory SQLite. Coverage is sound for the primitives but
shallow relative to the surface area (no workflow, browser, connector, or API
integration tests).

## One-line assessment

Good primitives, honest safety posture, correct LLM boundary — but the
**orchestration layer that would make it autonomous does not yet exist**, and
two of its provisioned data stores (Redis, MinIO) are not wired in.
