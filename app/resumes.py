"""Factual, reproducible resume rendering from the approved candidate profile."""

import re
from pathlib import Path
from textwrap import wrap

from reportlab.lib.pagesizes import LETTER
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AgentSpec, Artifact, Job
from app.services import CandidateProfileService, stable_hash


def _items(value) -> list[str]:
    if isinstance(value, list):
        return [str(item) if not isinstance(item, dict) else str(item.get("name") or item.get("title") or item) for item in value]
    if isinstance(value, dict):
        return [str(key) for key, present in value.items() if present]
    return []


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:48]


class ResumeService:
    def render(self, session: Session, job: Job, spec: AgentSpec) -> Artifact:
        profile = CandidateProfileService.approved(session)
        if not profile:
            raise ValueError("An approved candidate profile is required before rendering a resume.")
        facts = profile.facts
        output_dir = Path(get_settings().artifact_dir) / "resumes"
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"{_slug(facts.get('name', 'candidate'))}-{_slug(job.company)}-{job.id[:8]}.pdf"
        self._draw(path, facts, job)
        artifact = Artifact(
            job_id=job.id,
            kind="tailored_resume_pdf",
            uri=path.as_uri(),
            content_hash=stable_hash({"candidate_profile": profile.version, "job": job.fingerprint, "response_policy": spec.fingerprint}),
            provenance={"candidate_profile_version": profile.version, "agent_spec_id": spec.id, "job_fingerprint": job.fingerprint},
        )
        session.add(artifact)
        session.commit()
        session.refresh(artifact)
        return artifact

    def _draw(self, path: Path, facts: dict, job: Job) -> None:
        document = canvas.Canvas(str(path), pagesize=LETTER)
        width, height = LETTER
        y = height - 48

        def line(text: str, size: int = 9, bold: bool = False, gap: int | None = None):
            nonlocal y
            document.setFont("Helvetica-Bold" if bold else "Helvetica", size)
            for segment in wrap(text, width=max(45, int(104 / (size / 9)))) or [""]:
                document.drawString(48, y, segment)
                y -= gap or (size + 4)
                if y < 48:
                    document.showPage()
                    y = height - 48
                    document.setFont("Helvetica-Bold" if bold else "Helvetica", size)

        line(str(facts.get("name", "Candidate")), 18, True, 24)
        contact = " · ".join(str(facts[key]) for key in ("email", "phone", "location", "linkedin", "github", "website") if facts.get(key))
        if contact:
            line(contact, 8, False, 15)
        line("SUMMARY", 10, True, 16)
        line(str(facts.get("summary") or f"Software engineer applying for {job.title} at {job.company}."), 9)
        skills = _items(facts.get("skills"))
        if skills:
            line("SKILLS", 10, True, 16)
            line(", ".join(skills), 9)
        experience = facts.get("experience", [])
        if experience:
            line("EXPERIENCE", 10, True, 16)
            for item in experience:
                if isinstance(item, dict):
                    line(" · ".join(filter(None, [str(item.get("title", "")), str(item.get("company", "")), str(item.get("dates", ""))])), 9, True)
                    for bullet in item.get("bullets", []):
                        line(f"• {bullet}", 9)
                else:
                    line(str(item), 9)
        projects = facts.get("projects", [])
        if projects:
            line("PROJECTS", 10, True, 16)
            for item in projects:
                if isinstance(item, dict):
                    line(str(item.get("name", "Project")), 9, True)
                    line(str(item.get("description", "")), 9)
                else:
                    line(str(item), 9)
        document.save()
