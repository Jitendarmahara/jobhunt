"""Job Finder execution: model-routed when available, deterministic when not."""

import json
import re
from typing import Any

from sqlalchemy.orm import Session

from app.model_client import ModelClient
from app.models import AgentSpec, Assessment, CostEvent, Domain, Job, JobStatus, SpecStatus
from app.services import CandidateProfileService


def _terms(value: object) -> set[str]:
    return {term for term in re.findall(r"[a-z][a-z0-9+#.]{1,}", str(value).lower()) if len(term) > 1}


class QualificationService:
    def assess(self, session: Session, job: Job, spec: AgentSpec, experiment_id: str | None = None) -> Assessment:
        if spec.domain != Domain.JOB_FINDER:
            raise ValueError("Qualification requires a Job Finder AgentSpec.")
        profile = CandidateProfileService.approved(session)
        if not profile:
            raise ValueError("An approved candidate profile is required before qualification.")
        verdict = self._model_verdict(job, profile.facts, spec)
        if verdict is None:
            verdict = self._heuristic_verdict(job, profile.facts)
        assessment = Assessment(
            job_id=job.id,
            agent_spec_id=spec.id,
            experiment_id=experiment_id,
            relevance_score=verdict["relevance_score"],
            rationale=verdict["rationale"],
            evidence=verdict["evidence"],
        )
        threshold = float(spec.config.get("thresholds", {}).get("minimum_relevance", 0.65))
        if assessment.relevance_score >= threshold:
            job.status = JobStatus.QUALIFIED
        session.add(assessment)
        session.commit()
        session.refresh(assessment)
        return assessment

    def default_spec(self, session: Session) -> AgentSpec:
        spec = session.query(AgentSpec).filter(AgentSpec.domain == Domain.JOB_FINDER, AgentSpec.status == SpecStatus.CHAMPION).first()
        if not spec:
            raise ValueError("No Job Finder champion exists.")
        return spec

    def _model_verdict(self, job: Job, facts: dict[str, Any], spec: AgentSpec) -> dict[str, Any] | None:
        client = ModelClient()
        try:
            result = client.complete(
                spec.config["system_instruction"],
                json.dumps(
                    {
                        "job": {"company": job.company, "title": job.title, "location": job.location, "description": job.description[:12_000]},
                        "candidate_facts": facts,
                        "output_schema": {"relevance_score": "number from 0 to 1", "rationale": "brief factual explanation", "evidence": "object"},
                    }
                ) + "\nReturn only JSON.",
                temperature=float(spec.config.get("model_parameters", {}).get("temperature", 0.2)),
            )
            if not result:
                return None
            verdict = json.loads(result.text.strip().removeprefix("```json").removesuffix("```").strip())
            score = min(1.0, max(0.0, float(verdict["relevance_score"])))
            return {"relevance_score": score, "rationale": str(verdict["rationale"]), "evidence": dict(verdict.get("evidence") or {})}
        except (ValueError, KeyError, json.JSONDecodeError):
            return None

    def _heuristic_verdict(self, job: Job, facts: dict[str, Any]) -> dict[str, Any]:
        candidate_terms = _terms(facts.get("skills", [])) | _terms(facts.get("summary", "")) | _terms(facts.get("projects", []))
        job_terms = _terms(job.title) | _terms(job.description)
        overlap = sorted(candidate_terms & job_terms)
        required_signal = max(1, min(12, len(job_terms)))
        score = min(0.95, 0.15 + (len(overlap) / required_signal) * 1.5)
        return {
            "relevance_score": round(score, 3),
            "rationale": "Deterministic baseline using explicit overlap between approved candidate evidence and the public job description.",
            "evidence": {"matching_terms": overlap[:20], "mode": "heuristic_fallback"},
        }
