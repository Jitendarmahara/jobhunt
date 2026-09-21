"""Truthful project scaffolds for material portfolio gaps.

This is intentionally an artifact-producing service, not a fabricated-experience
generator. It creates a real editable repository skeleton and records exactly
which job, policy, and candidate-profile version motivated it.
"""

import base64
import re
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AgentSpec, Artifact, Job
from app.safety import LivePolicy, PolicyBlockedError
from app.services import CandidateProfileService, stable_hash


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:48]


def _focus_areas(description: str) -> list[str]:
    vocabulary = ("python", "typescript", "react", "kubernetes", "docker", "postgres", "redis", "aws", "gcp", "java", "go", "llm", "machine learning")
    text = description.lower()
    return [item for item in vocabulary if item in text][:5] or ["software engineering"]


class GitHubPublisher:
    def publish_private(self, name: str, files: dict[str, str]) -> str:
        settings = get_settings()
        if not settings.github_token:
            raise PolicyBlockedError("GITHUB_TOKEN is required before a private repository can be published.")
        headers = {"Authorization": f"Bearer {settings.github_token}", "Accept": "application/vnd.github+json"}
        endpoint = f"https://api.github.com/orgs/{settings.github_org}/repos" if settings.github_org else "https://api.github.com/user/repos"
        response = httpx.post(endpoint, headers=headers, json={"name": name, "private": True, "auto_init": False}, timeout=30)
        response.raise_for_status()
        repository = response.json()
        for path, content in files.items():
            upload = httpx.put(
                f"https://api.github.com/repos/{repository['full_name']}/contents/{path}",
                headers=headers,
                json={"message": f"Initial project scaffold: {path}", "content": base64.b64encode(content.encode()).decode()},
                timeout=30,
            )
            upload.raise_for_status()
        return repository["html_url"]


class ProjectService:
    def scaffold(self, session: Session, job: Job, spec: AgentSpec, publish_private_repository: bool, live_policy: LivePolicy) -> Artifact:
        profile = CandidateProfileService.approved(session)
        if not profile:
            raise PolicyBlockedError("An approved candidate profile is required before creating a portfolio artifact.")
        if publish_private_repository and not live_policy.enabled:
            raise PolicyBlockedError("Live actions must be enabled before publishing a private GitHub repository.")
        focus = _focus_areas(job.description)
        project_name = _slug(f"{job.company}-{job.title}-demonstrator")
        root = Path(get_settings().artifact_dir) / "projects" / project_name
        root.mkdir(parents=True, exist_ok=True)
        files = {
            "README.md": f"""# {job.company} {job.title} Demonstrator

This is a truthful portfolio project scaffold created to demonstrate skills relevant to a specific role. It is not a claim of prior employment or production ownership.

## Role-driven focus

{chr(10).join(f'- {area}' for area in focus)}

## Proposed build

1. Define a small reproducible problem and acceptance metrics.
2. Implement the core service in `src/demonstrator`.
3. Add tests, a container build, and a benchmark/report before publishing publicly.

## Provenance

- Job: {job.title} at {job.company}
- Job URL: {job.url}
- Candidate profile version: {profile.version}
- Response policy: {spec.fingerprint}
""",
            "pyproject.toml": f"""[project]
name = \"{project_name}\"
version = \"0.1.0\"
description = \"Role-driven portfolio demonstrator\"
requires-python = \">=3.11\"

[tool.pytest.ini_options]
pythonpath = [\"src\"]
""",
            "src/demonstrator/__init__.py": "from .evidence import Requirement, coverage_report\n\n__all__ = ['Requirement', 'coverage_report']\n",
            "src/demonstrator/evidence.py": """from dataclasses import dataclass\n\n\n@dataclass(frozen=True)\nclass Requirement:\n    name: str\n    demonstrated: bool\n    evidence: str\n\n\ndef coverage_report(requirements: list[Requirement]) -> dict[str, object]:\n    \"\"\"A small, testable core to evolve into the role-specific project.\"\"\"\n    total = len(requirements)\n    covered = sum(item.demonstrated for item in requirements)\n    return {\"covered\": covered, \"total\": total, \"ratio\": covered / total if total else 0.0}\n""",
            "tests/test_evidence.py": """from demonstrator.evidence import Requirement, coverage_report\n\n\ndef test_coverage_report_is_truthful():\n    report = coverage_report([Requirement(\"Python\", True, \"test\"), Requirement(\"Kubernetes\", False, \"not yet built\")])\n    assert report == {\"covered\": 1, \"total\": 2, \"ratio\": 0.5}\n""",
            ".github/workflows/test.yml": """name: test\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n        with: {python-version: '3.12'}\n      - run: pip install pytest\n      - run: pytest\n""",
        }
        for relative_path, content in files.items():
            destination = root / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content)
        uri = root.as_uri()
        if publish_private_repository:
            uri = GitHubPublisher().publish_private(project_name, files)
        artifact = Artifact(
            job_id=job.id,
            kind="project_scaffold",
            uri=uri,
            content_hash=stable_hash(files),
            provenance={
                "agent_spec_id": spec.id,
                "agent_fingerprint": spec.fingerprint,
                "candidate_profile_version": profile.version,
                "focus_areas": focus,
                "published_private": publish_private_repository,
            },
        )
        session.add(artifact)
        session.commit()
        session.refresh(artifact)
        return artifact
