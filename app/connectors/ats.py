"""Public Direct-ATS readers. They only read published job board data."""

from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx


class UnsupportedBoardUrl(ValueError):
    pass


@dataclass(frozen=True)
class NormalizedJob:
    provider_job_id: str
    url: str
    company: str
    title: str
    location: str | None
    description: str
    metadata: dict = field(default_factory=dict)


def _board_token(board_url: str, provider: str) -> str:
    parsed = urlparse(board_url.rstrip("/"))
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        raise UnsupportedBoardUrl(f"Could not find {provider} board token in {board_url}")
    return parts[-1]


def _greenhouse(board_url: str, client: httpx.Client) -> list[NormalizedJob]:
    token = _board_token(board_url, "Greenhouse")
    response = client.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", params={"content": "true"})
    response.raise_for_status()
    payload = response.json()
    jobs: list[NormalizedJob] = []
    for job in payload.get("jobs", []):
        location = (job.get("location") or {}).get("name")
        jobs.append(
            NormalizedJob(
                provider_job_id=str(job["id"]),
                url=job["absolute_url"],
                company=token,
                title=job["title"],
                location=location,
                description=job.get("content") or "",
                metadata={"departments": job.get("departments", []), "offices": job.get("offices", [])},
            )
        )
    return jobs


def _lever(board_url: str, client: httpx.Client) -> list[NormalizedJob]:
    token = _board_token(board_url, "Lever")
    response = client.get(f"https://api.lever.co/v0/postings/{token}", params={"mode": "json"})
    response.raise_for_status()
    jobs: list[NormalizedJob] = []
    for job in response.json():
        categories = job.get("categories") or {}
        jobs.append(
            NormalizedJob(
                provider_job_id=job["id"],
                url=job["hostedUrl"],
                company=token,
                title=job["text"],
                location=categories.get("location"),
                description=job.get("descriptionPlain") or job.get("description") or "",
                metadata={"team": categories.get("team"), "commitment": categories.get("commitment")},
            )
        )
    return jobs


def fetch_board_jobs(provider: str, board_url: str, timeout_seconds: float = 20) -> list[NormalizedJob]:
    with httpx.Client(timeout=timeout_seconds, headers={"User-Agent": "CareerSystem/0.1 (public-job-indexer)"}) as client:
        if provider == "greenhouse":
            return _greenhouse(board_url, client)
        if provider == "lever":
            return _lever(board_url, client)
    raise UnsupportedBoardUrl(f"Provider '{provider}' is not supported")

