import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def uuid_str() -> str:
    return str(uuid.uuid4())


class Domain(str, enum.Enum):
    JOB_FINDER = "job_finder"
    RESPONSE_BUILDER = "response_builder"
    APPLICATION = "application"


class SpecStatus(str, enum.Enum):
    CHAMPION = "champion"
    CHALLENGER = "challenger"
    ARCHIVED = "archived"


class JobStatus(str, enum.Enum):
    DISCOVERED = "discovered"
    QUALIFIED = "qualified"
    SKIPPED = "skipped"
    PREPARED = "prepared"
    APPLIED = "applied"
    CLOSED = "closed"


class LifecycleState(str, enum.Enum):
    """Fine-grained, durable lifecycle owned by the orchestration workflow.

    ``JobStatus`` is retained as a coarse projection for the dashboard and the
    existing endpoints; ``LifecycleState`` is the authoritative per-job state the
    autonomous workflow advances through. See ``app/lifecycle.py`` for the
    allowed transitions and the coarse-status projection.
    """

    DISCOVERED = "discovered"
    NORMALIZED = "normalized"
    QUALIFYING = "qualifying"
    ANALYZED = "analyzed"
    COMPANY_RESEARCHED = "company_researched"
    CANDIDATE_MATCHED = "candidate_matched"
    EVIDENCE_ANALYZED = "evidence_analyzed"
    PROJECT_PLANNING = "project_planning"
    PROJECT_BUILDING = "project_building"
    PROJECT_TESTING = "project_testing"
    PROJECT_REVIEW = "project_review"
    PROJECT_PUBLISHED = "project_published"
    EVIDENCE_UPDATED = "evidence_updated"
    APPLICATION_PREPARATION = "application_preparation"
    RESUME_GENERATED = "resume_generated"
    APPLICATION_READY = "application_ready"
    APPLICATION_EXECUTING = "application_executing"
    APPLICATION_VERIFIED = "application_verified"
    APPLICATION_AMBIGUOUS = "application_ambiguous"
    OUTREACH_EXECUTING = "outreach_executing"
    TRACKING = "tracking"
    RESPONSE_RECEIVED = "response_received"
    RESPONSE_ANALYZED = "response_analyzed"
    ACTION_PLANNED = "action_planned"
    ACTION_EXECUTED = "action_executed"
    SKIPPED = "skipped"
    FAILED = "failed"


class ApplicationStatus(str, enum.Enum):
    DRAFT = "draft"
    BLOCKED = "blocked"
    READY = "ready"
    SUBMITTED = "submitted"
    FAILED = "failed"
    ESCALATED = "escalated"


class OutcomeStage(str, enum.Enum):
    UNKNOWN = "unknown"
    REPLIED = "replied"
    POSITIVE_REPLY = "positive_reply"
    INTERVIEW = "interview"
    REJECTED = "rejected"
    OFFER = "offer"


class CandidateProfile(Base):
    __tablename__ = "candidate_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    is_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    facts: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("version", name="uq_candidate_profile_version"),)


class JobSource(Base):
    __tablename__ = "job_sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    board_url: Mapped[str] = mapped_column(String(1000), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    source_id: Mapped[str] = mapped_column(ForeignKey("job_sources.id"), nullable=False, index=True)
    provider_job_id: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(2000), nullable=False)
    company: Mapped[str] = mapped_column(String(300), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    location: Mapped[str | None] = mapped_column(String(500), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, nullable=False, default=dict)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), nullable=False, default=JobStatus.DISCOVERED)
    lifecycle_state: Mapped[LifecycleState] = mapped_column(
        Enum(LifecycleState), nullable=False, default=LifecycleState.DISCOVERED, index=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("source_id", "provider_job_id", name="uq_source_provider_job"),)


class JobTransition(Base):
    """Append-only audit of every durable lifecycle transition."""

    __tablename__ = "job_transitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    from_state: Mapped[LifecycleState | None] = mapped_column(Enum(LifecycleState), nullable=True)
    to_state: Mapped[LifecycleState] = mapped_column(Enum(LifecycleState), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentSpec(Base):
    __tablename__ = "agent_specs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    domain: Mapped[Domain] = mapped_column(Enum(Domain), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[SpecStatus] = mapped_column(Enum(SpecStatus), nullable=False, default=SpecStatus.CHALLENGER)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parent_spec_id: Mapped[str | None] = mapped_column(ForeignKey("agent_specs.id"), nullable=True)
    config: Mapped[dict] = mapped_column(JSON, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    mutation_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Experiment(Base):
    __tablename__ = "experiments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    domain: Mapped[Domain] = mapped_column(Enum(Domain), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    allocation: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    holdout_policy: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Assignment(Base):
    __tablename__ = "assignments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    agent_spec_id: Mapped[str] = mapped_column(ForeignKey("agent_specs.id"), nullable=False, index=True)
    propensity: Mapped[float] = mapped_column(Float, nullable=False)
    stratum: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("experiment_id", "job_id", name="uq_experiment_job_assignment"),)


class Assessment(Base):
    __tablename__ = "assessments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    agent_spec_id: Mapped[str] = mapped_column(ForeignKey("agent_specs.id"), nullable=False, index=True)
    experiment_id: Mapped[str | None] = mapped_column(ForeignKey("experiments.id"), nullable=True)
    relevance_score: Mapped[float] = mapped_column(Float, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    audit_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    audit_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    uri: Mapped[str] = mapped_column(String(2000), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provenance: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    agent_spec_id: Mapped[str] = mapped_column(ForeignKey("agent_specs.id"), nullable=False)
    resume_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    status: Mapped[ApplicationStatus] = mapped_column(Enum(ApplicationStatus), nullable=False, default=ApplicationStatus.DRAFT)
    provider_submission_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    browser_evidence_uri: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OutreachMessage(Base):
    __tablename__ = "outreach_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    agent_spec_id: Mapped[str] = mapped_column(ForeignKey("agent_specs.id"), nullable=False)
    recipient: Mapped[str] = mapped_column(String(500), nullable=False)
    subject: Mapped[str] = mapped_column(String(1000), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(500), nullable=True, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Outcome(Base):
    __tablename__ = "outcomes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    application_id: Mapped[str | None] = mapped_column(ForeignKey("applications.id"), nullable=True)
    stage: Mapped[OutcomeStage] = mapped_column(Enum(OutcomeStage), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InboundEmail(Base):
    __tablename__ = "inbound_emails"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    outreach_message_id: Mapped[str] = mapped_column(ForeignKey("outreach_messages.id"), nullable=False, index=True)
    provider_message_id: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    sender: Mapped[str] = mapped_column(String(500), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    snippet: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Escalation(Base):
    __tablename__ = "escalations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), nullable=True, index=True)
    workflow_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    category: Mapped[str] = mapped_column(String(128), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CostEvent(Base):
    __tablename__ = "cost_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    agent_spec_id: Mapped[str | None] = mapped_column(ForeignKey("agent_specs.id"), nullable=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    amount_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AgentEvent(Base):
    """Append-only, high-resolution trace of everything the agents do.

    One row per meaningful step or tool call (LLM call, chromium navigation,
    GitHub push, Gmail send, ...). Powers the live activity view and cost/token
    observability. Integer PK so the live stream can cursor cheaply by ``id``.
    """

    __tablename__ = "agent_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    agent: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tool: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phase: Mapped[str] = mapped_column(String(16), nullable=False, default="info")  # start|end|info|error
    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="info")  # info|warn|error
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
