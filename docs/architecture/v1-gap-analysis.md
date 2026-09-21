# Career System v1 — Gap Analysis

_Last updated: 2026-09-22. Reads against [`current-state.md`](./current-state.md)
and the product brief. Organized into the six required buckets: EXISTING,
MISSING, NEEDS REFACTOR, KEEP, REMOVE, FUTURE._

The single most important finding: the system is **operator-driven**, while the
brief mandates **autonomous-by-default** (`agent → policy → execution`, no human
approval step in the normal path). Closing that gap is the definition of
"correct v1" — see [`target-v1.md`](./target-v1.md).

---

## EXISTING (works today)

- Connector discovery for Greenhouse + Lever (`connectors/ats.py`)
- Job normalization + `fingerprint` deduplication (`services.py`)
- Qualification with LLM verdict **and** deterministic heuristic fallback
  (`qualification.py`)
- Truthful reportlab resume rendering (`resumes.py`)
- Static project scaffold + optional GitHub private-repo publish (`projects.py`)
- Playwright application submit with escalation-on-uncertainty (`browser.py`)
- Gmail send + thread-scoped reply reading (`integrations/gmail.py`)
- Keyword-based response/outcome tagging (`response_analyzer.py`)
- Champion/challenger evolution with **conservative, audit-gated promotion**
  (`evolution.py`, `bootstrap.py`)
- A/B experiments with reproducible assignment + propensity (`services.py`)
- Safety-by-default: live-action gate + 2 daily caps (`safety.py`)
- Idempotency keys: job `fingerprint`, application `idempotency_key`, artifact
  `content_hash`, outreach `provider_message_id`
- Single OpenAI-compatible model boundary with usage capture (`model_client.py`)
- Prometheus `/metrics` mount (one counter)
- Docker Compose + starter Helm chart

---

## MISSING (highest impact first)

1. **Autonomous orchestrator / durable lifecycle workflow.** No single Temporal
   workflow carries a job `DISCOVERED → … → TRACKING` with policy evaluation
   between stages. Today each stage is a manual HTTP call. **This is the core
   violation of the brief and the primary target of v1.**
2. **Rich lifecycle state + append-only transition/event log.** `JobStatus` has
   6 values vs. the brief's ~25-state machine; transitions are not recorded, so
   there is no durable audit of *how* a job moved.
3. **Policy engine.** Only `enabled` + 2 daily caps exist. The brief requires a
   configured candidate policy: target roles/locations/salary/experience/tech/
   industries, companies-to-avoid, daily application/outreach/project budgets,
   max concurrent browser sessions and coding jobs, allowed channels.
4. **Web-wide discovery** (seeds → search → company discovery → URL frontier →
   crawler → extraction → detection → normalize → dedup). _For v1 we define the
   interfaces and defer the engine (see FUTURE)._
5. **Company research, people/contact research, and a candidate evidence graph.**
   The profile is a single JSON blob; there is no requirement→evidence mapping
   or gap analysis feeding the "build a project" decision.
6. **Real project builder** (planner → architect → decomposer → coding workers →
   tests → security/quality review → publish) running in **isolated execution**
   (K8s Jobs). Current `projects.py` is a static template with no build/test.
7. **Idempotent external-submit execution record + ambiguous-outcome state.**
   The brief's exact scenario — browser crashes *after* clicking Submit — is not
   durably guarded before the click. Confirmation is verified post-hoc, but no
   pre-submit "attempted" record prevents a double submit on retry.
8. **Notifications** (Telegram + Email), durable and retryable, "alert only on
   trouble". Escalations are DB rows with no delivery mechanism.
9. **Observability depth.** OTEL deps are in `requirements.txt` but unwired; no
   traces; no per-agent / per-stage / cost / queue-depth / worker-health metrics.
10. **Redis + MinIO integration.** Both provisioned, neither used. Needed for
    locks, rate limits, resource budgets (Redis) and artifact storage (MinIO).
11. **Scheduled / continuous operation.** No cron or scheduled workflow; source
    polling and lifecycle progression are manual.
12. **Response conversation engine.** No intent taxonomy, no persisted
    conversation state, no action planner/executor beyond outcome tagging.
13. **Migrations.** `Base.metadata.create_all` only; schema change management is
    absent (add Alembic).

---

## NEEDS REFACTOR (keep the code, change the shape)

- **Orchestration boundary.** Introduce a durable `JobLifecycleWorkflow` that
  owns progression; make `main.py` endpoints thin (record intent → workflow
  advances the job). Move policy/Temporal-start logic out of
  `create_application`.
- **State vocabulary.** Expand `JobStatus` into the full lifecycle enum and add
  `job_transitions` / `events` tables written on every transition.
- **Async correctness.** Offload blocking `httpx`/Playwright out of the asyncio
  event loop (thread executor or async clients) in activities — a real problem
  once more than one job runs concurrently.
- **Artifact storage.** Replace local-FS `file://` writes with MinIO/S3 `s3://`
  via the already-configured object-store settings.
- **Engine construction.** `db.py` builds the engine at import time from
  settings; acceptable short-term but brittle for testing/config reload.

---

## KEEP (do not rewrite — these are the good bones)

- SQLAlchemy models (`models.py`) and Pydantic schemas (`schemas.py`)
- Every idempotency pattern already in place
- AgentSpec immutable lineage + champion/challenger + **conservative promotion**
  — this already matches the brief's `observe → propose → … → promote` intent
- Safety-by-default policy gates (`safety.py`)
- The single `ModelClient` boundary (`model_client.py`)
- Browser safety semantics (escalate on unknown / unverifiable, never fabricate)
- The entire stack: Temporal, Postgres, Redis, MinIO, Compose, Helm — reuse,
  do not replace

---

## REMOVE / AVOID

Nothing warrants deletion. Explicitly **avoid**:

- Expanding the static project scaffold toward **fabricated history** (fake
  commits, backdated activity, fake accomplishments) — forbidden by the brief.
- Introducing a **second orchestration framework** alongside Temporal.
- Making the **LLM own control flow**.
- Running **generated/untrusted code inside the api or worker pods** (must be
  isolated K8s Jobs).

---

## FUTURE (designed now as interfaces, built post-v1)

- Web-wide crawler + URL frontier (robots/rate-limit/canonicalization/incremental
  recrawl)
- Company research + people/contact research agents
- Candidate evidence graph with requirement→evidence gap analysis
- Full multi-agent project builder with K8s-Job isolation, resource limits,
  network restrictions, temporary credentials, cleanup
- GPU worker pool + dedicated browser worker pool
- Evolution canary/sandbox rollout (`propose → evaluate → sandbox/canary →
  verify → promote`) instead of direct challenger promotion
- Full tracing + rich observability dashboards
- Thousands of concurrent durable workflows at scale

---

## v1 scope decision (agreed)

- **Discovery:** keep/extend ATS connectors as the working path; **define**
  crawler/frontier interfaces but **defer** the web-wide engine to FUTURE.
- **Definition of done:** one job flows autonomously through the durable,
  policy-gated lifecycle with no per-stage human approval; **live actions stay
  OFF by default**.
- **Deliverable of this pass:** these three docs only; no code changes.
