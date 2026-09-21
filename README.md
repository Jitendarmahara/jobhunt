# Career System v1

An autonomous, evidence-first job-search system with durable workflows and
evolutionary agent policies. It is deliberately safe-by-default: discovery and
planning can run immediately, while external applications and email remain
disabled until live policy caps are explicitly enabled.

## Run locally

```bash
cp .env.example .env
docker compose up --build
```

Open the operator dashboard at `http://localhost:5173`, the API docs at
`http://localhost:8000/docs`, and Temporal UI at `http://localhost:8081`.

1. Create an approved candidate profile with factual skills, work history,
   projects, links, and application-answer facts.
2. Add Greenhouse or Lever board URLs in **Sources**.
3. Trigger a source poll. Jobs are normalized and deduplicated before policy
   evaluation.
4. Review policy assignments and audited scores in the dashboard.
5. Use the API to render a factual, job-specific PDF resume or generate a
   truthful project scaffold; both return immutable artifact records.
6. Only after testing, set daily caps and enable live actions in **Policy**.

## Safety boundary

The system refuses to invent candidate facts, bypass CAPTCHAs, retry an
unverified external submission, or send/submit while either live actions or the
relevant daily cap is disabled. Unknown mandatory answers become an email
escalation and a durable Temporal wait.

## Runtime shape

- `api`: FastAPI control plane and operator API.
- `worker`: Temporal workers for polling, qualification, application, outreach,
  outcome ingestion, and evolution workflows.
- `postgres`: authoritative business state and lineage.
- `redis`: only short-lived cache/locks/rate-limit state.
- `minio`: screenshots, resumes, project artifacts, and immutable evidence.
- `dashboard`: a small TypeScript operator console.

The code is configured exclusively with environment variables and each service
is independently containerized, so Kubernetes deployment can use the same
images, Secrets, and service configuration.

The starter Helm chart is in `deploy/helm/career-system`. It assumes managed or
separately deployed Postgres, Redis, object storage, and Temporal; provide their
endpoints through chart values and credentials through `existingSecret`.

## Key API actions

- `POST /api/jobs/{job_id}/resumes` creates a PDF from the approved candidate
  profile. It does not synthesize facts.
- `POST /api/jobs/{job_id}/projects` creates an editable project scaffold;
  `publish_private_repository: true` requires live actions plus `GITHUB_TOKEN`.
- `POST /api/jobs/{job_id}/applications` queues the durable browser workflow
  only when `submit: true`, a profile exists, and policy permits it.
- `POST /api/jobs/{job_id}/outreach` queues Gmail only under the same explicit
  live policy boundary.
