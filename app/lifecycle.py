"""Durable, idempotent job lifecycle state machine.

The orchestration workflow (``JobLifecycleWorkflow``) owns control flow; this
module owns the *rules*: which transitions are legal, how they are recorded, and
how the fine-grained ``LifecycleState`` projects onto the coarse ``JobStatus``
kept for the dashboard and legacy endpoints.

Every transition is:
- **validated** against ``ALLOWED_TRANSITIONS`` (illegal moves raise),
- **atomic** (one row appended to ``job_transitions`` + the job updated), and
- **idempotent** (a repeated ``idempotency_key`` is a no-op that returns the
  original transition, and a move to the current state is a no-op).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job, JobStatus, JobTransition, LifecycleState

S = LifecycleState

# Legal forward edges. Absent keys (SKIPPED, FAILED) are terminal.
ALLOWED_TRANSITIONS: dict[LifecycleState, set[LifecycleState]] = {
    S.DISCOVERED: {S.NORMALIZED, S.SKIPPED, S.FAILED},
    S.NORMALIZED: {S.QUALIFYING, S.SKIPPED, S.FAILED},
    S.QUALIFYING: {S.ANALYZED, S.SKIPPED, S.FAILED},
    S.ANALYZED: {S.COMPANY_RESEARCHED, S.CANDIDATE_MATCHED, S.SKIPPED, S.FAILED},
    S.COMPANY_RESEARCHED: {S.CANDIDATE_MATCHED, S.SKIPPED, S.FAILED},
    S.CANDIDATE_MATCHED: {S.EVIDENCE_ANALYZED, S.SKIPPED, S.FAILED},
    S.EVIDENCE_ANALYZED: {S.APPLICATION_PREPARATION, S.PROJECT_PLANNING, S.SKIPPED, S.FAILED},
    S.PROJECT_PLANNING: {S.PROJECT_BUILDING, S.FAILED},
    S.PROJECT_BUILDING: {S.PROJECT_TESTING, S.FAILED},
    S.PROJECT_TESTING: {S.PROJECT_REVIEW, S.FAILED},
    S.PROJECT_REVIEW: {S.PROJECT_PUBLISHED, S.FAILED},
    S.PROJECT_PUBLISHED: {S.EVIDENCE_UPDATED, S.FAILED},
    S.EVIDENCE_UPDATED: {S.APPLICATION_PREPARATION, S.FAILED},
    S.APPLICATION_PREPARATION: {S.RESUME_GENERATED, S.FAILED},
    S.RESUME_GENERATED: {S.APPLICATION_READY, S.FAILED},
    S.APPLICATION_READY: {S.APPLICATION_EXECUTING, S.FAILED},
    S.APPLICATION_EXECUTING: {S.APPLICATION_VERIFIED, S.APPLICATION_AMBIGUOUS, S.FAILED},
    S.APPLICATION_VERIFIED: {S.OUTREACH_EXECUTING, S.TRACKING},
    S.APPLICATION_AMBIGUOUS: {S.TRACKING},
    S.OUTREACH_EXECUTING: {S.TRACKING, S.FAILED},
    S.TRACKING: {S.RESPONSE_RECEIVED},
    S.RESPONSE_RECEIVED: {S.RESPONSE_ANALYZED},
    S.RESPONSE_ANALYZED: {S.ACTION_PLANNED, S.TRACKING},
    S.ACTION_PLANNED: {S.ACTION_EXECUTED},
    S.ACTION_EXECUTED: {S.TRACKING},
    S.SKIPPED: set(),
    S.FAILED: set(),
}

TERMINAL_STATES: frozenset[LifecycleState] = frozenset({S.SKIPPED, S.FAILED})

# Fine-grained state -> coarse JobStatus, so the dashboard and legacy endpoints
# keep working without knowing the full state machine.
COARSE_STATUS: dict[LifecycleState, JobStatus] = {
    S.DISCOVERED: JobStatus.DISCOVERED,
    S.NORMALIZED: JobStatus.DISCOVERED,
    S.QUALIFYING: JobStatus.DISCOVERED,
    S.ANALYZED: JobStatus.QUALIFIED,
    S.COMPANY_RESEARCHED: JobStatus.QUALIFIED,
    S.CANDIDATE_MATCHED: JobStatus.QUALIFIED,
    S.EVIDENCE_ANALYZED: JobStatus.QUALIFIED,
    S.PROJECT_PLANNING: JobStatus.PREPARED,
    S.PROJECT_BUILDING: JobStatus.PREPARED,
    S.PROJECT_TESTING: JobStatus.PREPARED,
    S.PROJECT_REVIEW: JobStatus.PREPARED,
    S.PROJECT_PUBLISHED: JobStatus.PREPARED,
    S.EVIDENCE_UPDATED: JobStatus.PREPARED,
    S.APPLICATION_PREPARATION: JobStatus.PREPARED,
    S.RESUME_GENERATED: JobStatus.PREPARED,
    S.APPLICATION_READY: JobStatus.PREPARED,
    S.APPLICATION_EXECUTING: JobStatus.PREPARED,
    S.APPLICATION_VERIFIED: JobStatus.APPLIED,
    S.APPLICATION_AMBIGUOUS: JobStatus.PREPARED,
    S.OUTREACH_EXECUTING: JobStatus.APPLIED,
    S.TRACKING: JobStatus.APPLIED,
    S.RESPONSE_RECEIVED: JobStatus.APPLIED,
    S.RESPONSE_ANALYZED: JobStatus.APPLIED,
    S.ACTION_PLANNED: JobStatus.APPLIED,
    S.ACTION_EXECUTED: JobStatus.APPLIED,
    S.SKIPPED: JobStatus.SKIPPED,
    S.FAILED: JobStatus.CLOSED,
}


class InvalidTransition(ValueError):
    """Raised when a transition is not permitted from the job's current state."""


def transition_exists(session: Session, idempotency_key: str) -> bool:
    """True if a transition with this idempotency key was already recorded."""
    return bool(
        session.scalar(select(JobTransition.id).where(JobTransition.idempotency_key == idempotency_key))
    )


def record_transition(
    session: Session,
    job: Job,
    to_state: LifecycleState,
    *,
    actor: str,
    reason: str | None = None,
    idempotency_key: str | None = None,
) -> JobTransition | None:
    """Move ``job`` to ``to_state`` durably and idempotently.

    Returns the ``JobTransition`` created (or the existing one for a repeated
    key). Returns ``None`` when the job is already in ``to_state`` (no-op).
    Raises ``InvalidTransition`` for an illegal move.
    """
    if idempotency_key:
        existing = session.scalar(
            select(JobTransition).where(JobTransition.idempotency_key == idempotency_key)
        )
        if existing:
            return existing

    current = job.lifecycle_state
    if current == to_state:
        return None
    if to_state not in ALLOWED_TRANSITIONS.get(current, set()):
        raise InvalidTransition(f"{current.value} -> {to_state.value} is not an allowed transition")

    transition = JobTransition(
        job_id=job.id,
        from_state=current,
        to_state=to_state,
        actor=actor,
        reason=reason,
        idempotency_key=idempotency_key,
    )
    job.lifecycle_state = to_state
    coarse = COARSE_STATUS.get(to_state)
    if coarse is not None:
        job.status = coarse
    session.add(transition)
    session.commit()
    session.refresh(job)
    return transition
