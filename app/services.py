import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.connectors import NormalizedJob, fetch_board_jobs
from app.models import (
    AgentSpec,
    Application,
    ApplicationStatus,
    Assignment,
    CandidateProfile,
    Domain,
    Experiment,
    Job,
    JobSource,
    JobStatus,
    SpecStatus,
    SystemSetting,
)
from app.safety import LivePolicy


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def fingerprint_job(job: NormalizedJob) -> str:
    return stable_hash({"url": job.url.lower().rstrip("/"), "company": job.company.lower(), "title": job.title.lower()})


class CandidateProfileService:
    @staticmethod
    def create(session: Session, facts: dict, approve: bool) -> CandidateProfile:
        last_version = session.scalar(select(CandidateProfile.version).order_by(CandidateProfile.version.desc()).limit(1)) or 0
        profile = CandidateProfile(
            version=last_version + 1,
            facts=facts,
            is_approved=approve,
            approved_at=datetime.now(timezone.utc) if approve else None,
        )
        session.add(profile)
        session.commit()
        session.refresh(profile)
        return profile

    @staticmethod
    def approved(session: Session) -> CandidateProfile | None:
        return session.scalar(
            select(CandidateProfile).where(CandidateProfile.is_approved.is_(True)).order_by(CandidateProfile.version.desc()).limit(1)
        )


class SourceService:
    @staticmethod
    def poll(session: Session, source: JobSource) -> dict:
        discovered = fetch_board_jobs(source.provider, source.board_url)
        new_jobs: list[Job] = []
        updated = 0
        now = datetime.now(timezone.utc)
        for remote_job in discovered:
            fingerprint = fingerprint_job(remote_job)
            job = session.scalar(select(Job).where(Job.fingerprint == fingerprint))
            if job:
                job.last_seen_at = now
                job.description = remote_job.description
                job.metadata_json = remote_job.metadata
                updated += 1
                continue
            job = Job(
                source_id=source.id,
                provider_job_id=remote_job.provider_job_id,
                url=remote_job.url,
                company=remote_job.company,
                title=remote_job.title,
                location=remote_job.location,
                description=remote_job.description,
                metadata_json=remote_job.metadata,
                fingerprint=fingerprint,
            )
            session.add(job)
            new_jobs.append(job)
        source.last_polled_at = now
        session.commit()
        new_job_ids = [job.id for job in new_jobs]
        return {"fetched": len(discovered), "created": len(new_job_ids), "updated": updated, "new_job_ids": new_job_ids}


class AgentSpecService:
    @staticmethod
    def create(
        session: Session,
        domain: Domain,
        name: str,
        config: dict,
        status: SpecStatus = SpecStatus.CHALLENGER,
        parent_spec_id: str | None = None,
        mutation_summary: str | None = None,
    ) -> AgentSpec:
        parent = session.get(AgentSpec, parent_spec_id) if parent_spec_id else None
        if parent and parent.domain != domain:
            raise ValueError("A child AgentSpec must remain in the same domain as its parent.")
        generation = (parent.generation + 1) if parent else 0
        fingerprint = stable_hash({"domain": domain.value, "config": config, "parent": parent_spec_id})
        existing = session.scalar(select(AgentSpec).where(AgentSpec.fingerprint == fingerprint))
        if existing:
            return existing
        if status == SpecStatus.CHAMPION:
            session.query(AgentSpec).filter(AgentSpec.domain == domain, AgentSpec.status == SpecStatus.CHAMPION).update(
                {AgentSpec.status: SpecStatus.ARCHIVED}, synchronize_session=False
            )
        spec = AgentSpec(
            domain=domain,
            name=name,
            status=status,
            generation=generation,
            parent_spec_id=parent_spec_id,
            config=config,
            fingerprint=fingerprint,
            mutation_summary=mutation_summary,
        )
        session.add(spec)
        session.commit()
        session.refresh(spec)
        return spec


class ExperimentService:
    @staticmethod
    def create(session: Session, domain: Domain, name: str, spec_ids: list[str], holdout_fraction: float) -> Experiment:
        specs = session.scalars(select(AgentSpec).where(AgentSpec.id.in_(spec_ids))).all()
        if len(specs) != len(set(spec_ids)) or any(spec.domain != domain for spec in specs):
            raise ValueError("Every experiment AgentSpec must exist and match the experiment domain.")
        allocation = {spec_id: round(1 / len(spec_ids), 6) for spec_id in sorted(spec_ids)}
        experiment = Experiment(
            domain=domain,
            name=name,
            allocation=allocation,
            holdout_policy={"fraction": holdout_fraction, "assignment": "sha256(job_id, experiment_id)"},
        )
        session.add(experiment)
        session.commit()
        session.refresh(experiment)
        return experiment

    @staticmethod
    def assign(session: Session, experiment: Experiment, job: Job) -> Assignment:
        existing = session.scalar(select(Assignment).where(Assignment.experiment_id == experiment.id, Assignment.job_id == job.id))
        if existing:
            return existing
        choices = sorted(experiment.allocation)
        digest = int(stable_hash({"job": job.id, "experiment": experiment.id})[:16], 16)
        chosen = choices[digest % len(choices)]
        assignment = Assignment(
            experiment_id=experiment.id,
            job_id=job.id,
            agent_spec_id=chosen,
            propensity=experiment.allocation[chosen],
            stratum=f"{job.company.lower()}::{(job.location or 'unknown').lower()}",
        )
        session.add(assignment)
        session.commit()
        session.refresh(assignment)
        return assignment


class PolicyService:
    KEY = "live_policy"

    @classmethod
    def get(cls, session: Session) -> LivePolicy:
        setting = session.get(SystemSetting, cls.KEY)
        if setting:
            value = setting.value
            return LivePolicy(
                enabled=bool(value.get("live_actions_enabled", False)),
                daily_application_limit=int(value.get("daily_application_limit", 0)),
                daily_outreach_limit=int(value.get("daily_outreach_limit", 0)),
            )
        env = get_settings()
        return LivePolicy(env.live_actions_enabled, env.daily_application_limit, env.daily_outreach_limit)

    @classmethod
    def update(cls, session: Session, policy: LivePolicy) -> LivePolicy:
        setting = session.get(SystemSetting, cls.KEY)
        value = {
            "live_actions_enabled": policy.enabled,
            "daily_application_limit": policy.daily_application_limit,
            "daily_outreach_limit": policy.daily_outreach_limit,
        }
        if setting:
            setting.value = value
        else:
            session.add(SystemSetting(key=cls.KEY, value=value))
        session.commit()
        return policy


def application_for_key(session: Session, key: str) -> Application | None:
    return session.scalar(select(Application).where(Application.idempotency_key == key))


def mark_job_prepared(session: Session, job: Job) -> None:
    if job.status == JobStatus.DISCOVERED:
        job.status = JobStatus.QUALIFIED
    session.commit()

