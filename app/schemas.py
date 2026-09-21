from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.models import ApplicationStatus, Domain, JobStatus, LifecycleState, OutcomeStage, SpecStatus


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class CandidateProfileCreate(BaseModel):
    facts: dict[str, Any] = Field(default_factory=dict)
    approve: bool = False


class CandidateProfileRead(ORMModel):
    id: str
    version: int
    is_approved: bool
    facts: dict[str, Any]
    created_at: datetime
    approved_at: datetime | None


class JobSourceCreate(BaseModel):
    provider: str = Field(pattern="^(greenhouse|lever)$")
    name: str = Field(min_length=1, max_length=200)
    board_url: HttpUrl
    enabled: bool = True


class JobSourceRead(ORMModel):
    id: str
    provider: str
    name: str
    board_url: str
    enabled: bool
    last_polled_at: datetime | None


class JobRead(ORMModel):
    id: str
    source_id: str
    url: str
    company: str
    title: str
    location: str | None
    status: JobStatus
    lifecycle_state: LifecycleState
    first_seen_at: datetime
    last_seen_at: datetime


class JobTransitionRead(ORMModel):
    id: str
    job_id: str
    from_state: LifecycleState | None
    to_state: LifecycleState
    actor: str
    reason: str | None
    created_at: datetime


class AgentGenome(BaseModel):
    """Typed, serializable policy configuration; mutable deployments are forbidden."""

    system_instruction: str = Field(min_length=10)
    workflow_version: str = "v1"
    model_route: str = "default"
    model_parameters: dict[str, Any] = Field(default_factory=lambda: {"temperature": 0.2})
    enabled_tools: list[str] = Field(default_factory=list)
    tool_order: list[str] = Field(default_factory=list)
    thresholds: dict[str, float] = Field(default_factory=dict)
    retry_policy: dict[str, Any] = Field(default_factory=dict)
    specialization: dict[str, Any] = Field(default_factory=dict)


class AgentSpecCreate(BaseModel):
    domain: Domain
    name: str = Field(min_length=1, max_length=200)
    status: SpecStatus = SpecStatus.CHALLENGER
    config: AgentGenome
    parent_spec_id: str | None = None
    mutation_summary: str | None = None


class AgentSpecRead(ORMModel):
    id: str
    domain: Domain
    name: str
    status: SpecStatus
    generation: int
    parent_spec_id: str | None
    config: dict[str, Any]
    fingerprint: str
    mutation_summary: str | None
    created_at: datetime


class ExperimentCreate(BaseModel):
    domain: Domain
    name: str = Field(min_length=1, max_length=200)
    agent_spec_ids: list[str] = Field(min_length=2)
    holdout_fraction: float = Field(default=0.2, ge=0, le=0.5)


class ExperimentRead(ORMModel):
    id: str
    domain: Domain
    name: str
    status: str
    allocation: dict[str, float]
    holdout_policy: dict[str, Any]
    created_at: datetime


class AssessmentCreate(BaseModel):
    agent_spec_id: str
    experiment_id: str | None = None
    relevance_score: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1)
    evidence: dict[str, Any] = Field(default_factory=dict)


class QualificationRun(BaseModel):
    agent_spec_id: str | None = None
    experiment_id: str | None = None


class AssessmentRead(ORMModel):
    id: str
    job_id: str
    agent_spec_id: str
    experiment_id: str | None
    relevance_score: float
    rationale: str
    evidence: dict[str, Any]
    audit_score: float | None
    audit_notes: str | None
    created_at: datetime


class AuditCreate(BaseModel):
    score: float = Field(ge=0, le=1)
    notes: str = ""


class ApplicationCreate(BaseModel):
    agent_spec_id: str
    resume_artifact_id: str | None = None
    idempotency_key: str = Field(min_length=8, max_length=255)
    submit: bool = False


class ApplicationRead(ORMModel):
    id: str
    job_id: str
    agent_spec_id: str
    resume_artifact_id: str | None
    idempotency_key: str
    status: ApplicationStatus
    provider_submission_id: str | None
    browser_evidence_uri: str | None
    submitted_at: datetime | None
    created_at: datetime


class ProjectCreate(BaseModel):
    agent_spec_id: str
    publish_private_repository: bool = False


class ResumeCreate(BaseModel):
    agent_spec_id: str


class ArtifactRead(ORMModel):
    id: str
    job_id: str | None
    kind: str
    uri: str
    content_hash: str
    provenance: dict[str, Any]
    created_at: datetime


class OutreachCreate(BaseModel):
    agent_spec_id: str
    recipient: str = Field(min_length=3, max_length=500)
    subject: str = Field(min_length=1, max_length=1000)
    body: str = Field(min_length=1)
    send: bool = False


class OutcomeCreate(BaseModel):
    application_id: str | None = None
    stage: OutcomeStage
    source: str = Field(min_length=1, max_length=64)
    evidence: dict[str, Any] = Field(default_factory=dict)


class EscalationResolve(BaseModel):
    resolution: str = Field(min_length=1)


class LivePolicyUpdate(BaseModel):
    live_actions_enabled: bool
    daily_application_limit: int = Field(ge=0, le=100)
    daily_outreach_limit: int = Field(ge=0, le=100)


class EvolutionRunRequest(BaseModel):
    domain: Domain
    minimum_audits: int = Field(default=3, ge=1, le=100)
