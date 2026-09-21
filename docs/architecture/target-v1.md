# Career System v1 — Target Architecture

_Last updated: 2026-09-22. Builds on [`current-state.md`](./current-state.md) and
[`v1-gap-analysis.md`](./v1-gap-analysis.md). This is the architecture we are
steering v1 toward, achieved by **minimally extending the existing stack** — no
new frameworks._

## Definition of done for v1

> A single job flows **autonomously** through a **durable, idempotent,
> policy-gated Temporal lifecycle** — with **no per-stage human approval** — and
> **live external actions remain OFF by default** until explicitly armed.

Scaling to thousands of concurrent jobs is a later phase; the architecture must
not preclude it, but v1 is proven with **one job, full lifecycle, in safe mode**.

## Guiding principles (from the brief, non-negotiable)

1. **Autonomous by default.** Normal path is `agent → policy evaluation →
   execution`. The human configures policy once; there is no approval step in
   the happy path. Humans are notified only for genuine trouble.
2. **Temporal/state machine owns control flow.** The LLM makes decisions
   *within bounded activities* and returns structured results. It never owns
   orchestration, retries, concurrency, or budgets.
3. **Factual only.** Never fabricate candidate facts or project history. A
   project created today is represented as created today, real and runnable.
4. **Idempotent external actions.** Every outward action has an idempotency key
   and a durable execution record; ambiguous outcomes are a first-class state.
5. **Safe-by-default.** Live actions and caps default to OFF/0; unknown answers
   and unverifiable submissions escalate rather than guess.

## Layering (clean separation; stack unchanged)

```
┌─────────────────────────── CONTROL PLANE ───────────────────────────┐
│ JobLifecycleWorkflow (Temporal): owns state, transitions, retries,   │
│ timeouts, cancellation, idempotency, concurrency, budgets, policy.   │
│ FastAPI = thin intent + observation API (no business flow).          │
└──────────────────────────────────────────────────────────────────────┘
        │ dispatches bounded activities (structured in / structured out)
        ▼
┌─────────────────────────── AGENT WORKERS ───────────────────────────┐
│ discovery · qualification · company-research(stub) · candidate/      │
│ evidence-match · project-builder · resume · application · outreach ·  │
│ response · evolution                                                  │
└──────────────────────────────────────────────────────────────────────┘
        │ uses
        ▼
┌───── TOOLS ─────┐   ┌───────────────── DATA ─────────────────┐
│ ATS connectors  │   │ Postgres = authoritative state          │
│ browser         │   │ Redis    = locks / rate-limits / budgets│
│ GitHub · Gmail  │   │ MinIO/S3 = artifacts                     │
│ ModelClient     │   └─────────────────────────────────────────┘
│ crawler(FUTURE) │
└─────────────────┘
        │ emits
        ▼
 OBSERVABILITY (logs, Prometheus per-stage/agent/cost metrics, OTEL traces)
 NOTIFICATIONS (Telegram + Email, durable + retryable, alert-only-on-trouble)
```

## The lifecycle state machine (durable, idempotent transitions)

```
DISCOVERED → NORMALIZED → QUALIFYING → ANALYZED → COMPANY_RESEARCHED
→ CANDIDATE_MATCHED → EVIDENCE_ANALYZED
   ├─ evidence sufficient ────────────► APPLICATION_PREPARATION
   └─ evidence gap & project justified ► PROJECT_PLANNING → PROJECT_BUILDING
                                         → PROJECT_TESTING → PROJECT_REVIEW
                                         → PROJECT_PUBLISHED → EVIDENCE_UPDATED
                                         → APPLICATION_PREPARATION
APPLICATION_PREPARATION → RESUME_GENERATED → APPLICATION_READY
→ APPLICATION_EXECUTING → APPLICATION_VERIFIED → OUTREACH_EXECUTING → TRACKING
TRACKING ← RESPONSE_RECEIVED → RESPONSE_ANALYZED → ACTION_PLANNED
→ ACTION_EXECUTED → TRACKING
```

Rules:
- Each state is durable in Postgres and each transition is **atomic +
  idempotent** (re-running a transition is a no-op).
- Every transition appends a row to `job_transitions` / `events`:
  `(job_id, from_state, to_state, actor, reason, idempotency_key, at)`.
- Terminal/branch decisions (evidence sufficient? project justified?) are made by
  the **policy evaluator** between stages, not by the LLM directly.
- `APPLICATION_EXECUTING` uses the ambiguous-outcome protocol below.

### Enum evolution
`JobStatus` (6 values today) is superseded by a `LifecycleState` enum covering
the states above. The existing values map forward (`discovered→DISCOVERED`,
`qualified→ANALYZED`, `applied→APPLICATION_VERIFIED`, etc.) so no data is lost.

## Idempotency (formalized per brief)

| Action | Idempotency key |
| --- | --- |
| Application | `candidate_id + job_id` |
| Project | `candidate_id + company_id + project_spec_hash` |
| Outreach | `candidate_id + contact_id + message_hash` |
| Job | `canonical_url + content_hash` |
| Notification | `event_id + channel` |

**Ambiguous-outcome protocol for external submit** (the "crash after Submit"
case):
1. Before clicking Submit, write a durable `submission_attempt` record
   (`status = attempted`) keyed by the application idempotency key.
2. Click Submit; verify confirmation.
3. On verified confirmation → `SUBMITTED`. On verifiable failure → `FAILED`
   (safe to retry). On **inability to determine** (crash, timeout, no
   confirmation) → `AMBIGUOUS` — **never auto-retry**; require human/verification
   step. This preserves the current escalate-on-uncertainty behavior in
   `browser.py` and makes it durable.

## Policy engine (`LivePolicy` → `CandidatePolicy`)

Configured once by the human; evaluated by the workflow between stages:

- **Targeting:** roles, locations, salary range, experience range, technologies,
  industries, companies-to-avoid.
- **Budgets:** daily application / outreach / project-generation budgets.
- **Resource caps:** max concurrent browser sessions, max concurrent coding
  jobs, allowed communication channels.
- **Live gates:** `live_actions_enabled` + per-action daily caps, **default
  OFF/0** (existing `safety.py` behavior preserved and extended).

Redis backs the concurrency counters, rate limits, and per-day budget windows.

## Discovery (v1 = connectors; interfaces defined)

- **v1 working path:** keep Greenhouse + Lever; introduce an `AtsConnector`
  interface so more boards drop in without touching the pipeline.
- **Defined but deferred** (typed interfaces only, implemented in FUTURE):
  `Seeds → Search → CompanyDiscovery → URLFrontier → Crawler → Extraction →
  Detection → Normalize → Dedup`, with domain crawl budgets, robots/terms
  awareness, rate limiting, canonical URLs, content hashing, incremental
  recrawl. Browser automation used only when HTTP extraction is insufficient.

## Deferred-but-designed subsystems

Company/people research, candidate evidence graph (requirement→evidence gap
analysis), multi-agent project builder with **isolated K8s-Job execution**
(CPU/memory limits, timeout, filesystem isolation, network restrictions,
temporary credentials, cleanup), GPU + browser worker pools, evolution
canary/sandbox rollout, and full tracing dashboards are all specified as target
interfaces now and built in later phases.

## Observability & notifications (target)

- **Metrics:** per-stage counts + latency, per-agent success/failure/latency,
  token + cost, queue depth, retries, worker/browser/crawler health,
  application & project build success/failure.
- **Traces:** OTEL spans across workflow → activity → tool → model.
- **Notifications:** durable, retryable Telegram + Email; fire only on infra
  failure, persistent workflow failure, expired credentials, auth required,
  account restriction, unexpected application state, security issue, evolution
  rollout failure, or system-wide degradation — **not** on normal success.

## Evolution (target safety upgrade)

Keep champion/challenger + immutable lineage. Upgrade promotion from direct
audit-gated promotion to the full `observe → propose → evaluate →
sandbox/canary → verify → promote` pipeline. The LLM proposes; it never mutates
deployed policy/config in place. A Global Evolution Manager coordinates per-agent
evolution managers.

## Proposed implementation sequence (foundations first)

Each phase must leave the system runnable.

| # | Phase | Notes |
| --- | --- | --- |
| 1 | Contracts / domain models | Extend enums; typed `CandidatePolicy` |
| 2 | Durable state machine | `job_transitions` / `events`; atomic transitions |
| 3 | `JobLifecycleWorkflow` + thin API | The autonomy centerpiece |
| 4 | Idempotency | Submission-attempt record + ambiguous-outcome state |
| 5 | Event model / audit stream | Append-only events on every transition |
| 6 | Resource budgets | Redis locks / rate-limits / budget windows |
| 7 | Observability | Per-stage/agent/cost metrics + OTEL traces |
| 8 | Discovery connector interface | `AtsConnector`; define crawler interfaces |
| 9 | Job intelligence hardening | Structured analysis output |
| 10 | Candidate / evidence graph | Requirement→evidence gap analysis |
| 11 | Project builder | Isolated K8s-Job execution |
| 12 | Resume builder integration | Driven by workflow, artifacts in MinIO |
| 13 | Application browser workers | Dedicated pool |
| 14 | Outreach | Policy-gated, budgeted |
| 15 | Response ingestion + conversation state | Intent taxonomy + planner |
| 16 | Evolution safety | canary/sandbox rollout |
| 17 | K8s production deploy | Worker pools, GPU |
| 18 | Notification / recovery | Telegram + Email, durable |

**v1 acceptance = phases 1–7 complete + one job driven end-to-end through the
lifecycle in safe mode.** Phases 8–18 extend breadth on top of the same durable
foundation.

## Recommended first code phase

Phases **1–3** together: extend the domain contracts, add the durable transition
log, and stand up `JobLifecycleWorkflow` with a thin API and policy evaluation
between stages. This directly converts the system from operator-driven to
autonomous-by-default while keeping every existing safety guarantee and leaving
the app runnable.
