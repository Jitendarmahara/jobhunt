from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from temporalio import activity

from app.browser import PlaywrightApplicationBrowser
from app.db import SessionLocal
from app.events import emit, set_context, step
from app.integrations.gmail import GmailClient, GmailConfigurationError
from app.lifecycle import LifecycleState, record_transition, transition_exists
from app.models import (
    AgentSpec,
    Application,
    ApplicationStatus,
    Artifact,
    Domain,
    Escalation,
    Job,
    JobSource,
    JobStatus,
    OutreachMessage,
    SpecStatus,
)
from app.qualification import QualificationService
from app.research import WebResearcher, analyze_resume_gaps, research_company, suggest_people
from app.resumes import ResumeService
from app.safety import PolicyBlockedError, enforce_application_policy, enforce_outreach_policy
from app.services import CandidateProfileService, PolicyService, SourceService, application_for_key, stable_hash


@activity.defn
async def poll_source_activity(source_id: str) -> dict:
    with SessionLocal() as session:
        source = session.get(JobSource, source_id)
        if not source or not source.enabled:
            return {"status": "skipped", "reason": "source not found or disabled"}
        return {"status": "completed", **SourceService.poll(session, source)}


@activity.defn
async def prepare_application_activity(application_id: str) -> dict:
    with SessionLocal() as session:
        application = session.get(Application, application_id)
        if not application:
            return {"status": "failed", "reason": "application not found"}
        profile = CandidateProfileService.approved(session)
        try:
            if not profile:
                raise PolicyBlockedError("No approved candidate profile exists.")
            enforce_application_policy(session, PolicyService.get(session))
        except PolicyBlockedError as exc:
            application.status = ApplicationStatus.BLOCKED
            session.commit()
            return {"status": "blocked", "reason": str(exc)}
        application.status = ApplicationStatus.READY
        session.commit()
        return {"status": "ready"}


@activity.defn
async def execute_application_browser_activity(application_id: str) -> dict:
    with SessionLocal() as session:
        application = session.get(Application, application_id)
        if not application:
            return {"status": "failed", "reason": "application not found"}
        job = session.get(Job, application.job_id)
        profile = CandidateProfileService.approved(session)
        if not job or not profile:
            return {"status": "failed", "reason": "job or approved candidate profile not found"}
        browser_facts = dict(profile.facts)
        if application.resume_artifact_id:
            resume = session.get(Artifact, application.resume_artifact_id)
            if resume and resume.uri.startswith("file://"):
                browser_facts["resume_path"] = resume.uri.removeprefix("file://")
        result = await PlaywrightApplicationBrowser().inspect_and_submit(job.url, browser_facts, application.id)
        application.browser_evidence_uri = result.evidence_uri
        if result.status == "submitted":
            application.status = ApplicationStatus.SUBMITTED
            application.submitted_at = datetime.now(timezone.utc)
            job.status = JobStatus.APPLIED
        elif result.status in ("needs_escalation", "unavailable"):
            application.status = ApplicationStatus.ESCALATED
            session.add(
                Escalation(
                    job_id=job.id,
                    workflow_id=f"application-{application.id}",
                    category="application_form",
                    question="; ".join(result.unknown_questions) if result.unknown_questions else (result.detail or "Browser worker is unavailable"),
                    context={"application_id": application.id, "url": job.url, "evidence_uri": result.evidence_uri},
                )
            )
        else:
            application.status = ApplicationStatus.FAILED
        session.commit()
        return {"status": result.status, "evidence_uri": result.evidence_uri, "detail": result.detail}


@activity.defn
async def send_outreach_activity(message_id: str) -> dict:
    with SessionLocal() as session:
        message = session.get(OutreachMessage, message_id)
        if not message:
            return {"status": "failed", "reason": "outreach message not found"}
        try:
            enforce_outreach_policy(session, PolicyService.get(session))
            provider_message_id, thread_id = GmailClient().send(message.recipient, message.subject, message.body)
            message.provider_message_id = provider_message_id
            message.status = "sent"
            message.sent_at = datetime.now(timezone.utc)
            session.commit()
            return {"status": "sent", "provider_message_id": provider_message_id, "thread_id": thread_id}
        except (PolicyBlockedError, GmailConfigurationError, httpx.HTTPError) as exc:
            message.status = "blocked" if isinstance(exc, PolicyBlockedError) else "failed"
            session.add(
                Escalation(
                    job_id=message.job_id,
                    workflow_id=f"outreach-{message.id}",
                    category="outreach",
                    question=str(exc),
                    context={"outreach_id": message.id, "recipient": message.recipient},
                )
            )
            session.commit()
            return {"status": message.status, "reason": str(exc)}


# --------------------------------------------------------------------------- #
# Autonomous lifecycle activities                                             #
# Each is a bounded task: it does one stage, records the durable transition,  #
# and returns a small structured decision. They are idempotent under Temporal #
# retries. The LLM (via ModelClient) decides only *within* a task.            #
# --------------------------------------------------------------------------- #


def _champion(session, domain: Domain) -> AgentSpec | None:
    return (
        session.query(AgentSpec)
        .filter(AgentSpec.domain == domain, AgentSpec.status == SpecStatus.CHAMPION)
        .first()
    )


def _begin(agent: str, job_id: str | None) -> None:
    """Set the trace context so every emitted event is tagged with this agent/job."""
    trace_id = None
    try:
        trace_id = activity.info().workflow_id
    except Exception:
        trace_id = None
    set_context(agent=agent, job_id=job_id, trace_id=trace_id)


def _job_dict(job: Job) -> dict:
    return {"company": job.company, "title": job.title, "location": job.location, "url": job.url, "description": job.description}


def _save_artifact(session, job_id: str, kind: str, data: dict) -> str:
    """Upsert an inline research artifact (idempotent by job_id+kind)."""
    existing = session.scalar(select(Artifact).where(Artifact.job_id == job_id, Artifact.kind == kind))
    if existing:
        existing.provenance = data
        existing.content_hash = stable_hash(data)
        session.commit()
        return existing.id
    artifact = Artifact(
        job_id=job_id,
        kind=kind,
        uri=f"inline://{kind}/{job_id}",
        content_hash=stable_hash(data),
        provenance=data,
    )
    session.add(artifact)
    session.commit()
    session.refresh(artifact)
    return artifact.id


@activity.defn
async def list_enabled_sources_activity() -> list[str]:
    with SessionLocal() as session:
        return list(session.scalars(select(JobSource.id).where(JobSource.enabled.is_(True))).all())


@activity.defn
async def mark_normalized_activity(job_id: str) -> dict:
    _begin("job_finder", job_id)
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if not job:
            emit("Job not found", level="error", phase="error")
            return {"status": "failed", "reason": "job not found"}
        emit(f"Job Finder: picked up '{job.title}' @ {job.company}", detail={"url": job.url})
        record_transition(
            session, job, LifecycleState.NORMALIZED, actor="discovery", idempotency_key=f"{job_id}:normalized"
        )
        return {"status": "ok", "state": job.lifecycle_state.value}


@activity.defn
async def qualify_job_activity(job_id: str) -> dict:
    _begin("job_finder", job_id)
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if not job:
            return {"status": "failed", "reason": "job not found"}
        # Idempotent short-circuit: a completed qualification is not repeated.
        if transition_exists(session, f"{job_id}:analyzed"):
            return {"status": "ok", "qualified": True, "score": None, "cached": True}
        if transition_exists(session, f"{job_id}:skipped"):
            return {"status": "ok", "qualified": False, "score": None, "cached": True}

        record_transition(
            session, job, LifecycleState.QUALIFYING, actor="lifecycle", idempotency_key=f"{job_id}:qualifying"
        )
        spec = _champion(session, Domain.JOB_FINDER)
        if not spec:
            record_transition(session, job, LifecycleState.FAILED, actor="lifecycle", reason="no job_finder champion")
            return {"status": "failed", "reason": "no job_finder champion"}
        try:
            with step("Job Finder: scoring fit against your profile", tool="deepseek"):
                assessment = QualificationService().assess(session, job, spec)
        except ValueError as exc:
            record_transition(session, job, LifecycleState.FAILED, actor="job_finder", reason=str(exc))
            emit(f"Qualification failed: {exc}", level="error", phase="error")
            return {"status": "failed", "reason": str(exc)}
        threshold = float(spec.config.get("thresholds", {}).get("minimum_relevance", 0.65))
        qualified = assessment.relevance_score >= threshold
        emit(
            f"Fit score {assessment.relevance_score:.2f} (threshold {threshold:.2f}) → {'PURSUE' if qualified else 'SKIP'}",
            level="info" if qualified else "warn",
            detail={"rationale": assessment.rationale[:300], "evidence": assessment.evidence},
        )
        if qualified:
            record_transition(
                session,
                job,
                LifecycleState.ANALYZED,
                actor="job_finder",
                reason=f"relevance={assessment.relevance_score:.3f} >= {threshold:.3f}",
                idempotency_key=f"{job_id}:analyzed",
            )
        else:
            record_transition(
                session,
                job,
                LifecycleState.SKIPPED,
                actor="job_finder",
                reason=f"relevance={assessment.relevance_score:.3f} < {threshold:.3f}",
                idempotency_key=f"{job_id}:skipped",
            )
        return {"status": "ok", "qualified": qualified, "score": assessment.relevance_score}


@activity.defn
async def research_company_activity(job_id: str) -> dict:
    """Response Builder: research the company/product/domain (chromium + AI). Best-effort."""
    _begin("response_builder", job_id)
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if not job:
            return {"status": "failed", "reason": "job not found"}
        jd = _job_dict(job)
    emit(f"Response Builder: researching {jd['company']}", detail={"url": jd["url"]})
    page = None
    try:
        page = await WebResearcher().fetch(jd["url"])
    except Exception as exc:
        emit(f"Company page fetch skipped: {exc}", tool="chromium", level="warn")
    research = await research_company(jd, page)
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        _save_artifact(session, job_id, "company_research", research)
        record_transition(
            session, job, LifecycleState.COMPANY_RESEARCHED, actor="response_builder",
            reason=f"source={research.get('source')}", idempotency_key=f"{job_id}:researched",
        )
    emit(
        "Company research done",
        detail={"summary": (research.get("company_summary") or "")[:300],
                "tech": research.get("key_technologies"), "source": research.get("source")},
    )
    return {"status": "ok", "research": research}


@activity.defn
async def analyze_gaps_activity(job_id: str) -> dict:
    """Response Builder: find resume strengths & gaps vs the role (AI). Annotation only."""
    _begin("response_builder", job_id)
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if not job:
            return {"status": "failed", "reason": "job not found"}
        profile = CandidateProfileService.approved(session)
        jd = _job_dict(job)
    gaps = await analyze_resume_gaps(jd, profile.facts if profile else {})
    with SessionLocal() as session:
        _save_artifact(session, job_id, "resume_gaps", gaps)
    emit(
        f"Gap analysis: {len(gaps.get('strengths', []))} strengths, {len(gaps.get('gaps', []))} gaps"
        + (" · project recommended" if gaps.get("project_recommended") else ""),
        detail=gaps,
        level="warn" if gaps.get("gaps") else "info",
    )
    return {"status": "ok", "gaps": gaps}


@activity.defn
async def find_people_activity(job_id: str) -> dict:
    """Response Builder: suggest people to contact + outreach angle (AI). Annotation only."""
    _begin("response_builder", job_id)
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if not job:
            return {"status": "failed", "reason": "job not found"}
        research_art = session.scalar(select(Artifact).where(Artifact.job_id == job_id, Artifact.kind == "company_research"))
        research = research_art.provenance if research_art else {}
        jd = _job_dict(job)
    people = await suggest_people(jd, research)
    with SessionLocal() as session:
        _save_artifact(session, job_id, "contacts", people)
    emit(
        f"People to contact: {', '.join(people.get('target_roles', [])) or 'none suggested'}",
        detail=people,
    )
    return {"status": "ok", "people": people}


@activity.defn
async def match_candidate_activity(job_id: str) -> dict:
    """v1: candidate/evidence matching is a pass-through decision point.

    It records the CANDIDATE_MATCHED -> EVIDENCE_ANALYZED transitions and, for
    v1, always reports evidence sufficient (the project-generation branch is a
    designed hook implemented in a later phase). No facts are invented.
    """
    _begin("response_builder", job_id)
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if not job:
            return {"status": "failed", "reason": "job not found"}
        emit("Response Builder: matching job requirements against your evidence")
        record_transition(
            session, job, LifecycleState.CANDIDATE_MATCHED, actor="lifecycle", idempotency_key=f"{job_id}:matched"
        )
        record_transition(
            session, job, LifecycleState.EVIDENCE_ANALYZED, actor="lifecycle", idempotency_key=f"{job_id}:evidence"
        )
        emit("Evidence sufficient — no project needed (v1)", detail={"note": "project-builder branch arrives in a later phase"})
        return {"status": "ok", "evidence_sufficient": True}


@activity.defn
async def generate_resume_activity(job_id: str) -> dict:
    _begin("response_builder", job_id)
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if not job:
            return {"status": "failed", "reason": "job not found"}
        record_transition(
            session,
            job,
            LifecycleState.APPLICATION_PREPARATION,
            actor="lifecycle",
            idempotency_key=f"{job_id}:app_prep",
        )
        # Idempotent: reuse the existing resume artifact on retry.
        existing = session.scalar(
            select(Artifact).where(Artifact.job_id == job_id, Artifact.kind == "tailored_resume_pdf")
        )
        if existing:
            emit("Reusing already-generated resume", tool="pdf", detail={"artifact_id": existing.id})
            artifact_id = existing.id
        else:
            spec = _champion(session, Domain.RESPONSE_BUILDER)
            if not spec:
                record_transition(session, job, LifecycleState.FAILED, actor="lifecycle", reason="no response_builder champion")
                return {"status": "failed", "reason": "no response_builder champion"}
            try:
                with step("Response Builder: writing tailored resume (PDF)", tool="pdf"):
                    artifact = ResumeService().render(session, job, spec)
            except ValueError as exc:
                record_transition(session, job, LifecycleState.FAILED, actor="resume", reason=str(exc))
                emit(f"Resume failed: {exc}", level="error", phase="error")
                return {"status": "failed", "reason": str(exc)}
            emit("Resume written", tool="pdf", detail={"uri": artifact.uri, "artifact_id": artifact.id})
            artifact_id = artifact.id
        record_transition(
            session, job, LifecycleState.RESUME_GENERATED, actor="resume", idempotency_key=f"{job_id}:resume"
        )
        return {"status": "ok", "resume_artifact_id": artifact_id}


@activity.defn
async def create_application_activity(job_id: str, resume_artifact_id: str | None) -> dict:
    """Create the durable application record and decide whether live submit is allowed.

    Idempotency key is candidate_profile_version + job_id (per the brief's
    ``candidate_id + job_id``). Stops safely at APPLICATION_READY when live
    actions are disabled or the daily cap is exhausted.
    """
    _begin("job_applier", job_id)
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if not job:
            return {"status": "failed", "reason": "job not found"}
        emit("Job Applier: preparing application record")
        profile = CandidateProfileService.approved(session)
        spec = _champion(session, Domain.APPLICATION)
        if not profile or not spec:
            record_transition(session, job, LifecycleState.FAILED, actor="lifecycle", reason="missing profile or application champion")
            return {"status": "failed", "reason": "missing approved profile or application champion"}
        key = f"auto:{profile.version}:{job_id}"
        application = application_for_key(session, key)
        if not application:
            application = Application(
                job_id=job_id,
                agent_spec_id=spec.id,
                resume_artifact_id=resume_artifact_id,
                idempotency_key=key,
                status=ApplicationStatus.DRAFT,
            )
            session.add(application)
            session.commit()
            session.refresh(application)
        live_submit = True
        try:
            enforce_application_policy(session, PolicyService.get(session))
            application.status = ApplicationStatus.READY
        except PolicyBlockedError as exc:
            live_submit = False
            application.status = ApplicationStatus.BLOCKED
            emit(f"Live applying is OFF — holding at 'ready' (safe): {exc}", level="warn")
        session.commit()
        record_transition(
            session, job, LifecycleState.APPLICATION_READY, actor="lifecycle", idempotency_key=f"{job_id}:app_ready"
        )
        if live_submit:
            emit("Application ready and live applying is ON — will submit", detail={"application_id": application.id})
        return {"status": "ok", "application_id": application.id, "live_submit": live_submit}


@activity.defn
async def lifecycle_submit_application_activity(job_id: str, application_id: str) -> dict:
    """Execute the external submission with an ambiguous-outcome guard.

    APPLICATION_EXECUTING is recorded *before* the browser acts, so a retry that
    finds the job already EXECUTING treats the outcome as AMBIGUOUS rather than
    re-submitting (the brief's "crashed after Submit" case).
    """
    _begin("job_applier", job_id)
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if not job:
            return {"status": "failed", "reason": "job not found"}
        if job.lifecycle_state == LifecycleState.APPLICATION_EXECUTING:
            emit("Retry detected after an in-flight submit — NOT resubmitting (marking ambiguous)", level="warn")
            record_transition(
                session, job, LifecycleState.APPLICATION_AMBIGUOUS, actor="lifecycle",
                reason="retry after in-flight submission; not resubmitting",
            )
            record_transition(session, job, LifecycleState.TRACKING, actor="lifecycle")
            return {"status": "ambiguous", "reason": "retry after in-flight submission"}
        emit("Job Applier: recording submit attempt (durable guard) then opening browser")
        record_transition(
            session, job, LifecycleState.APPLICATION_EXECUTING, actor="lifecycle",
            idempotency_key=f"{application_id}:executing",
        )

    result = await execute_application_browser_activity(application_id)

    with SessionLocal() as session:
        job = session.get(Job, job_id)
        outcome = result.get("status")
        if outcome == "submitted":
            record_transition(session, job, LifecycleState.APPLICATION_VERIFIED, actor="browser")
            record_transition(session, job, LifecycleState.TRACKING, actor="lifecycle")
        elif outcome == "needs_escalation":
            record_transition(session, job, LifecycleState.APPLICATION_AMBIGUOUS, actor="browser", reason=outcome)
            record_transition(session, job, LifecycleState.TRACKING, actor="lifecycle")
        else:
            record_transition(session, job, LifecycleState.FAILED, actor="browser", reason=str(result))
        return result
