from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentSpec, Domain, SpecStatus
from app.services import AgentSpecService


DEFAULT_POLICIES: dict[Domain, list[tuple[str, str]]] = {
    Domain.JOB_FINDER: [
        ("Evidence-first finder", "Evaluate role fit using only explicit job requirements and approved candidate facts. Prefer high-signal general SWE roles; explain every score with evidence."),
        ("Skill-overlap finder", "Rank general software roles by required-skill overlap, engineering depth, and candidate evidence. Penalize missing must-have requirements and weak descriptions."),
        ("Trajectory finder", "Find roles where the approved candidate can credibly grow. Prefer strong teams, clear scope, and transferable software-engineering work; do not overstate fit."),
    ],
    Domain.RESPONSE_BUILDER: [
        ("Truthful response builder", "Select concrete approved achievements for each role. Tailor wording but never invent experience, ownership, metrics, or credentials."),
        ("Evidence-mapping response builder", "Map each required skill to a specific approved evidence item. Surface gaps honestly and recommend a small demonstrable project only when material."),
        ("Concise response builder", "Produce a focused, factual application package. Emphasize directly relevant software-engineering evidence and avoid unsupported claims."),
    ],
    Domain.APPLICATION: [
        ("Safe form interpreter", "Map visible form questions only to approved candidate facts. Escalate unknown questions, CAPTCHA, ambiguous consent, or unverifiable submit status."),
        ("Evidence-aware form interpreter", "Complete only fields supported by the approved profile and artifacts. Preserve page evidence and avoid duplicate submissions."),
        ("Conservative application interpreter", "Prioritize accuracy over completion. Stop and create a human escalation whenever a mandatory answer cannot be grounded in candidate facts."),
    ],
}


def bootstrap_agent_specs(session: Session) -> None:
    if session.scalar(select(AgentSpec.id).limit(1)):
        return
    for domain, policies in DEFAULT_POLICIES.items():
        for position, (name, instruction) in enumerate(policies):
            AgentSpecService.create(
                session,
                domain=domain,
                name=name,
                status=SpecStatus.CHAMPION if position == 0 else SpecStatus.CHALLENGER,
                config={
                    "system_instruction": instruction,
                    "workflow_version": "v1",
                    "model_route": "default",
                    "model_parameters": {"temperature": 0.2},
                    "enabled_tools": [],
                    "tool_order": [],
                    "thresholds": {"minimum_relevance": 0.65},
                    "retry_policy": {"max_attempts": 3},
                    "specialization": {},
                },
            )

