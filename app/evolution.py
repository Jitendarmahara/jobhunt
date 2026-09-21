"""Trace-aware evolutionary proposal and conservative champion promotion.

The runner deliberately owns experiment lineage and safety gates. GEPA is used
as a proposal engine only; it can never mutate deployed policies in-place.
"""

from copy import deepcopy
from statistics import mean

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.model_client import ModelClient
from app.models import AgentSpec, Assessment, Domain, SpecStatus
from app.services import AgentSpecService


class InsufficientEvidenceError(ValueError):
    pass


def _audited_assessments(session: Session, spec_id: str) -> list[Assessment]:
    return session.scalars(
        select(Assessment).where(Assessment.agent_spec_id == spec_id, Assessment.audit_score.is_not(None))
    ).all()


class EvolutionService:
    def run(self, session: Session, domain: Domain, minimum_audits: int) -> AgentSpec:
        champion = session.scalar(
            select(AgentSpec).where(AgentSpec.domain == domain, AgentSpec.status == SpecStatus.CHAMPION).limit(1)
        )
        if not champion:
            raise InsufficientEvidenceError(f"No champion exists for {domain.value}.")
        evidence = _audited_assessments(session, champion.id)
        if len(evidence) < minimum_audits:
            raise InsufficientEvidenceError(
                f"{domain.value} champion has {len(evidence)} audited assessments; {minimum_audits} are required."
            )
        feedback = [
            {
                "score": assessment.audit_score,
                "agent_score": assessment.relevance_score,
                "rationale": assessment.rationale,
                "audit_notes": assessment.audit_notes or "",
                "evidence": assessment.evidence,
            }
            for assessment in evidence[-20:]
        ]
        candidate_config = deepcopy(champion.config)
        proposal = self._propose_text_mutation(champion.config, feedback)
        if proposal:
            candidate_config["system_instruction"] = proposal["system_instruction"]
            summary = proposal["summary"]
        else:
            # Deterministic fallback retains v1 operability without an LLM while
            # preserving the exact same immutable lineage contract.
            candidate_config.setdefault("specialization", {})["audit_focus"] = "calibrated relevance with explicit evidence"
            candidate_config.setdefault("model_parameters", {})["temperature"] = max(
                0.0, float(candidate_config.get("model_parameters", {}).get("temperature", 0.2)) - 0.05
            )
            summary = "Trace-aware fallback mutation: reduce variance and require explicit factual evidence."
        return AgentSpecService.create(
            session,
            domain=domain,
            name=f"{champion.name} · GEPA challenger g{champion.generation + 1}",
            config=candidate_config,
            parent_spec_id=champion.id,
            mutation_summary=summary,
        )

    def _propose_text_mutation(self, config: dict, feedback: list[dict]) -> dict | None:
        prompt = {
            "current_system_instruction": config["system_instruction"],
            "traces": feedback,
            "constraints": [
                "Change only the system instruction.",
                "Do not add claims that are not supported by candidate facts.",
                "Keep the instruction under 1800 characters.",
                "Return keys system_instruction and summary.",
            ],
        }
        try:
            proposal = ModelClient().complete_json(
                "You are a GEPA-style reflective optimizer for a job-search policy. Use audit feedback to make one narrow, testable improvement.",
                str(prompt),
            )
        except (httpx.HTTPError, ValueError, KeyError):
            return None
        if not proposal or not isinstance(proposal.get("system_instruction"), str):
            return None
        return {"system_instruction": proposal["system_instruction"][:1800], "summary": str(proposal.get("summary", "LLM reflective mutation"))[:800]}

    def promote(self, session: Session, spec_id: str, minimum_audits: int = 3) -> AgentSpec:
        candidate = session.get(AgentSpec, spec_id)
        if not candidate:
            raise ValueError("AgentSpec was not found.")
        if candidate.status != SpecStatus.CHALLENGER:
            raise ValueError("Only a challenger can be promoted.")
        challenger_evidence = _audited_assessments(session, candidate.id)
        champion = session.scalar(
            select(AgentSpec).where(AgentSpec.domain == candidate.domain, AgentSpec.status == SpecStatus.CHAMPION).limit(1)
        )
        champion_evidence = _audited_assessments(session, champion.id) if champion else []
        if len(challenger_evidence) < minimum_audits:
            raise InsufficientEvidenceError("Promotion requires more audited challenger assessments.")
        challenger_score = mean(item.audit_score for item in challenger_evidence if item.audit_score is not None)
        champion_score = mean(item.audit_score for item in champion_evidence if item.audit_score is not None) if champion_evidence else 0.0
        if challenger_score < champion_score:
            raise InsufficientEvidenceError("Challenger regressed against champion audit score; it cannot be promoted.")
        if champion:
            champion.status = SpecStatus.ARCHIVED
        candidate.status = SpecStatus.CHAMPION
        session.commit()
        session.refresh(candidate)
        return candidate
