from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import Counter, make_asgi_app
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.bootstrap import bootstrap_agent_specs
from app.db import SessionLocal, get_session, initialize_database
from app.evolution import EvolutionService, InsufficientEvidenceError
from app.models import (
    AgentSpec,
    Application,
    ApplicationStatus,
    Assessment,
    Escalation,
    Job,
    JobSource,
    JobStatus,
    Outcome,
    OutreachMessage,
)
from app.safety import PolicyBlockedError, enforce_application_policy, enforce_outreach_policy
from app.schemas import (
    AgentSpecCreate,
    AgentSpecRead,
    ApplicationCreate,
    ApplicationRead,
    ArtifactRead,
    AssessmentCreate,
    AssessmentRead,
    AuditCreate,
    CandidateProfileCreate,
    CandidateProfileRead,
    EscalationResolve,
    EvolutionRunRequest,
    ExperimentCreate,
    ExperimentRead,
    JobRead,
    JobSourceCreate,
    JobSourceRead,
    LivePolicyUpdate,
    OutcomeCreate,
    OutreachCreate,
    ProjectCreate,
    QualificationRun,
    ResumeCreate,
)
from app.services import (
    AgentSpecService,
    CandidateProfileService,
    ExperimentService,
    PolicyService,
    SourceService,
    application_for_key,
)
from app.safety import LivePolicy
from app.projects import ProjectService
from app.resumes import ResumeService
from app.qualification import QualificationService
from app.response_analyzer import ResponseAnalyzer


EXTERNAL_ACTIONS = Counter("career_external_action_requests_total", "External action requests", ["action", "result"])


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    with SessionLocal() as session:
        bootstrap_agent_specs(session)
    yield


app = FastAPI(title="Career System Control Plane", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/metrics", make_asgi_app())


def not_found(entity: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{entity} was not found")


def policy_payload(session: Session) -> dict:
    policy = PolicyService.get(session)
    return {
        "live_actions_enabled": policy.enabled,
        "daily_application_limit": policy.daily_application_limit,
        "daily_outreach_limit": policy.daily_outreach_limit,
    }


@app.get("/api/health")
def health(session: Session = Depends(get_session)) -> dict:
    session.execute(select(1))
    return {"status": "ok", "service": "career-control-plane", "live_policy": policy_payload(session)}


@app.get("/api/dashboard")
def dashboard(session: Session = Depends(get_session)) -> dict:
    def count(model) -> int:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)

    agent_rows = session.execute(
        select(AgentSpec.domain, AgentSpec.status, func.count()).group_by(AgentSpec.domain, AgentSpec.status)
    ).all()
    agent_population: dict[str, dict[str, int]] = {}
    for domain, spec_status, total in agent_rows:
        agent_population.setdefault(domain.value, {})[spec_status.value] = total
    return {
        "now": datetime.now(timezone.utc),
        "live_policy": policy_payload(session),
        "counts": {
            "sources": count(JobSource),
            "jobs": count(Job),
            "applications": count(Application),
            "outreach_messages": count(OutreachMessage),
            "open_escalations": int(session.scalar(select(func.count()).select_from(Escalation).where(Escalation.status == "open")) or 0),
        },
        "agent_population": agent_population,
        "recent_jobs": session.scalars(select(Job).order_by(Job.first_seen_at.desc()).limit(8)).all(),
        "recent_escalations": session.scalars(select(Escalation).where(Escalation.status == "open").order_by(Escalation.created_at.desc()).limit(8)).all(),
    }


@app.post("/api/candidate-profiles", response_model=CandidateProfileRead, status_code=status.HTTP_201_CREATED)
def create_candidate_profile(payload: CandidateProfileCreate, session: Session = Depends(get_session)):
    return CandidateProfileService.create(session, payload.facts, payload.approve)


@app.get("/api/candidate-profiles/current", response_model=CandidateProfileRead)
def current_candidate_profile(session: Session = Depends(get_session)):
    profile = CandidateProfileService.approved(session)
    if not profile:
        raise not_found("Approved candidate profile")
    return profile


@app.get("/api/sources", response_model=list[JobSourceRead])
def list_sources(session: Session = Depends(get_session)):
    return session.scalars(select(JobSource).order_by(JobSource.created_at.desc())).all()


@app.post("/api/sources", response_model=JobSourceRead, status_code=status.HTTP_201_CREATED)
def create_source(payload: JobSourceCreate, session: Session = Depends(get_session)):
    if session.scalar(select(JobSource).where(JobSource.board_url == str(payload.board_url))):
        raise HTTPException(status_code=409, detail="This board URL already exists.")
    source = JobSource(provider=payload.provider, name=payload.name, board_url=str(payload.board_url), enabled=payload.enabled)
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


@app.post("/api/sources/{source_id}/poll")
def poll_source(source_id: str, session: Session = Depends(get_session)):
    source = session.get(JobSource, source_id)
    if not source:
        raise not_found("Job source")
    if not source.enabled:
        raise HTTPException(status_code=409, detail="This source is disabled.")
    try:
        return SourceService.poll(session, source)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Source poll failed: {exc}") from exc


@app.get("/api/jobs", response_model=list[JobRead])
def list_jobs(
    job_status: JobStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
):
    statement = select(Job).order_by(Job.first_seen_at.desc()).limit(limit)
    if job_status:
        statement = statement.where(Job.status == job_status)
    return session.scalars(statement).all()


@app.post("/api/jobs/{job_id}/assessments", response_model=AssessmentRead, status_code=status.HTTP_201_CREATED)
def create_assessment(job_id: str, payload: AssessmentCreate, session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    spec = session.get(AgentSpec, payload.agent_spec_id)
    if not job:
        raise not_found("Job")
    if not spec:
        raise not_found("AgentSpec")
    assessment = Assessment(
        job_id=job.id,
        agent_spec_id=spec.id,
        experiment_id=payload.experiment_id,
        relevance_score=payload.relevance_score,
        rationale=payload.rationale,
        evidence=payload.evidence,
    )
    if payload.relevance_score >= float(spec.config.get("thresholds", {}).get("minimum_relevance", 0.65)):
        job.status = JobStatus.QUALIFIED
    session.add(assessment)
    session.commit()
    session.refresh(assessment)
    return assessment


@app.get("/api/jobs/{job_id}/assessments", response_model=list[AssessmentRead])
def list_assessments(job_id: str, session: Session = Depends(get_session)):
    if not session.get(Job, job_id):
        raise not_found("Job")
    return session.scalars(select(Assessment).where(Assessment.job_id == job_id).order_by(Assessment.created_at.desc())).all()


@app.post("/api/jobs/{job_id}/qualify", response_model=AssessmentRead, status_code=status.HTTP_201_CREATED)
def qualify_job(job_id: str, payload: QualificationRun, session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    if not job:
        raise not_found("Job")
    service = QualificationService()
    spec = session.get(AgentSpec, payload.agent_spec_id) if payload.agent_spec_id else service.default_spec(session)
    if not spec:
        raise not_found("AgentSpec")
    try:
        return service.assess(session, job, spec, payload.experiment_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/assessments/{assessment_id}/audit", response_model=AssessmentRead)
def audit_assessment(assessment_id: str, payload: AuditCreate, session: Session = Depends(get_session)):
    assessment = session.get(Assessment, assessment_id)
    if not assessment:
        raise not_found("Assessment")
    assessment.audit_score = payload.score
    assessment.audit_notes = payload.notes
    session.commit()
    session.refresh(assessment)
    return assessment


@app.get("/api/agents", response_model=list[AgentSpecRead])
def list_agents(session: Session = Depends(get_session)):
    return session.scalars(select(AgentSpec).order_by(AgentSpec.domain, AgentSpec.created_at.desc())).all()


@app.post("/api/agents", response_model=AgentSpecRead, status_code=status.HTTP_201_CREATED)
def create_agent(payload: AgentSpecCreate, session: Session = Depends(get_session)):
    try:
        return AgentSpecService.create(
            session,
            domain=payload.domain,
            name=payload.name,
            config=payload.config.model_dump(),
            status=payload.status,
            parent_spec_id=payload.parent_spec_id,
            mutation_summary=payload.mutation_summary,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/agents/{spec_id}/promote", response_model=AgentSpecRead)
def promote_agent(spec_id: str, session: Session = Depends(get_session)):
    try:
        return EvolutionService().promote(session, spec_id)
    except InsufficientEvidenceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/experiments", response_model=list[ExperimentRead])
def list_experiments(session: Session = Depends(get_session)):
    from app.models import Experiment

    return session.scalars(select(Experiment).order_by(Experiment.created_at.desc())).all()


@app.post("/api/experiments", response_model=ExperimentRead, status_code=status.HTTP_201_CREATED)
def create_experiment(payload: ExperimentCreate, session: Session = Depends(get_session)):
    try:
        return ExperimentService.create(session, payload.domain, payload.name, payload.agent_spec_ids, payload.holdout_fraction)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/experiments/{experiment_id}/jobs/{job_id}/assign")
def assign_job(experiment_id: str, job_id: str, session: Session = Depends(get_session)):
    from app.models import Experiment

    experiment = session.get(Experiment, experiment_id)
    job = session.get(Job, job_id)
    if not experiment:
        raise not_found("Experiment")
    if not job:
        raise not_found("Job")
    assignment = ExperimentService.assign(session, experiment, job)
    return {"id": assignment.id, "agent_spec_id": assignment.agent_spec_id, "propensity": assignment.propensity, "stratum": assignment.stratum}


@app.get("/api/policy")
def get_policy(session: Session = Depends(get_session)):
    return policy_payload(session)


@app.put("/api/policy")
def update_policy(payload: LivePolicyUpdate, session: Session = Depends(get_session)):
    policy = PolicyService.update(
        session,
        LivePolicy(payload.live_actions_enabled, payload.daily_application_limit, payload.daily_outreach_limit),
    )
    return {
        "live_actions_enabled": policy.enabled,
        "daily_application_limit": policy.daily_application_limit,
        "daily_outreach_limit": policy.daily_outreach_limit,
    }


@app.post("/api/jobs/{job_id}/applications", response_model=ApplicationRead, status_code=status.HTTP_201_CREATED)
async def create_application(job_id: str, payload: ApplicationCreate, session: Session = Depends(get_session)):
    existing = application_for_key(session, payload.idempotency_key)
    if existing:
        return existing
    job = session.get(Job, job_id)
    spec = session.get(AgentSpec, payload.agent_spec_id)
    if not job:
        raise not_found("Job")
    if not spec:
        raise not_found("AgentSpec")
    application = Application(job_id=job_id, agent_spec_id=spec.id, resume_artifact_id=payload.resume_artifact_id, idempotency_key=payload.idempotency_key)
    if payload.submit:
        profile = CandidateProfileService.approved(session)
        try:
            if not profile:
                raise PolicyBlockedError("An approved candidate profile is required before application submission.")
            enforce_application_policy(session, PolicyService.get(session))
            application.status = ApplicationStatus.READY
            application.browser_evidence_uri = f"workflow://application/{application.id}"
            EXTERNAL_ACTIONS.labels("application", "queued").inc()
        except PolicyBlockedError as exc:
            application.status = ApplicationStatus.BLOCKED
            application.browser_evidence_uri = f"policy://blocked/{exc}"
            EXTERNAL_ACTIONS.labels("application", "blocked").inc()
    session.add(application)
    session.commit()
    session.refresh(application)
    if application.status == ApplicationStatus.READY:
        try:
            from app.temporal_client import start_application

            workflow_id = await start_application(application.id)
            application.browser_evidence_uri = f"temporal://{workflow_id}"
            session.commit()
            session.refresh(application)
        except Exception as exc:  # The record remains visible and recoverable; no external action was performed.
            application.status = ApplicationStatus.ESCALATED
            session.add(
                Escalation(
                    job_id=job.id,
                    workflow_id=f"application-{application.id}",
                    category="workflow_start",
                    question=f"Application workflow could not be started: {exc}",
                    context={"application_id": application.id},
                )
            )
            session.commit()
            session.refresh(application)
    return application


@app.get("/api/applications", response_model=list[ApplicationRead])
def list_applications(session: Session = Depends(get_session)):
    return session.scalars(select(Application).order_by(Application.created_at.desc())).all()


@app.post("/api/jobs/{job_id}/projects", response_model=ArtifactRead, status_code=status.HTTP_201_CREATED)
def create_project_scaffold(job_id: str, payload: ProjectCreate, session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    spec = session.get(AgentSpec, payload.agent_spec_id)
    if not job:
        raise not_found("Job")
    if not spec:
        raise not_found("AgentSpec")
    try:
        return ProjectService().scaffold(session, job, spec, payload.publish_private_repository, PolicyService.get(session))
    except (PolicyBlockedError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/resumes", response_model=ArtifactRead, status_code=status.HTTP_201_CREATED)
def create_tailored_resume(job_id: str, payload: ResumeCreate, session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    spec = session.get(AgentSpec, payload.agent_spec_id)
    if not job:
        raise not_found("Job")
    if not spec:
        raise not_found("AgentSpec")
    try:
        return ResumeService().render(session, job, spec)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/outreach")
async def create_outreach(job_id: str, payload: OutreachCreate, session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    spec = session.get(AgentSpec, payload.agent_spec_id)
    if not job:
        raise not_found("Job")
    if not spec:
        raise not_found("AgentSpec")
    message = OutreachMessage(job_id=job.id, agent_spec_id=spec.id, recipient=payload.recipient, subject=payload.subject, body=payload.body)
    if payload.send:
        try:
            enforce_outreach_policy(session, PolicyService.get(session))
            message.status = "queued"
            EXTERNAL_ACTIONS.labels("outreach", "queued").inc()
        except PolicyBlockedError as exc:
            message.status = "blocked"
            EXTERNAL_ACTIONS.labels("outreach", "blocked").inc()
    session.add(message)
    session.commit()
    session.refresh(message)
    if message.status == "queued":
        try:
            from app.temporal_client import start_outreach

            await start_outreach(message.id)
        except Exception as exc:
            message.status = "failed"
            session.add(
                Escalation(
                    job_id=job.id,
                    workflow_id=f"outreach-{message.id}",
                    category="workflow_start",
                    question=f"Outreach workflow could not be started: {exc}",
                    context={"outreach_id": message.id},
                )
            )
            session.commit()
    return {"id": message.id, "status": message.status}


@app.post("/api/jobs/{job_id}/outcomes", status_code=status.HTTP_201_CREATED)
def create_outcome(job_id: str, payload: OutcomeCreate, session: Session = Depends(get_session)):
    if not session.get(Job, job_id):
        raise not_found("Job")
    outcome = Outcome(job_id=job_id, application_id=payload.application_id, stage=payload.stage, source=payload.source, evidence=payload.evidence)
    session.add(outcome)
    session.commit()
    return {"id": outcome.id, "stage": outcome.stage}


@app.post("/api/outcomes/ingest-gmail")
def ingest_gmail_outcomes(session: Session = Depends(get_session)):
    try:
        return ResponseAnalyzer().ingest_gmail(session)
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=f"Gmail ingestion failed: {exc}") from exc


@app.get("/api/escalations")
def list_escalations(session: Session = Depends(get_session)):
    return session.scalars(select(Escalation).order_by(Escalation.created_at.desc())).all()


@app.post("/api/escalations/{escalation_id}/resolve")
async def resolve_escalation(escalation_id: str, payload: EscalationResolve, session: Session = Depends(get_session)):
    escalation = session.get(Escalation, escalation_id)
    if not escalation:
        raise not_found("Escalation")
    escalation.status = "resolved"
    escalation.resolution = payload.resolution
    escalation.resolved_at = datetime.now(timezone.utc)
    session.commit()
    application_id = escalation.context.get("application_id") if escalation.context else None
    if application_id:
        try:
            from app.temporal_client import resolve_application

            await resolve_application(application_id, payload.resolution)
        except Exception:
            # Resolution persists even if a worker is restarting; the operator can retry signal delivery.
            pass
    return {"id": escalation.id, "status": escalation.status}


@app.post("/api/evolution/run", response_model=AgentSpecRead, status_code=status.HTTP_201_CREATED)
def run_evolution(payload: EvolutionRunRequest, session: Session = Depends(get_session)):
    try:
        return EvolutionService().run(session, payload.domain, payload.minimum_audits)
    except InsufficientEvidenceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
