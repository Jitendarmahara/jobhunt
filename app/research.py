"""Response Builder research tools.

Real, visible research used by the Response Builder agent:
- ``WebResearcher`` browses a page with chromium (falls back to HTTP) and emits
  a chromium event for every action so it is visible in the live feed.
- ``research_company`` summarizes the company/product/domain and requirements.
- ``analyze_resume_gaps`` compares the job against the candidate's real facts.
- ``suggest_people`` proposes who to contact and an outreach angle.

No candidate facts are ever invented. When the model is not configured the
functions degrade to honest, deterministic summaries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.config import get_settings
from app.events import emit, step
from app.model_client import ModelClient


@dataclass
class PageContent:
    url: str
    title: str
    text: str
    method: str  # "chromium" | "http" | "unavailable"


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(html: str) -> str:
    text = _TAG_RE.sub(" ", html)
    return _WS_RE.sub(" ", text).strip()


class WebResearcher:
    async def fetch(self, url: str, *, max_chars: int = 6000) -> PageContent | None:
        settings = get_settings()
        if not settings.research_browsing_enabled:
            emit("Web research disabled by config", tool="chromium", level="warn")
            return None
        # Prefer real chromium so the human can watch the browser work.
        try:
            from playwright.async_api import async_playwright  # noqa: F401

            return await self._fetch_chromium(url, max_chars)
        except Exception as exc:
            emit(f"Chromium unavailable ({exc}); using HTTP fetch instead", tool="chromium", level="warn")
            return await self._fetch_http(url, max_chars)

    async def _fetch_chromium(self, url: str, max_chars: int) -> PageContent | None:
        from playwright.async_api import async_playwright

        emit("Chromium: launching for company research", tool="chromium", phase="start")
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(user_agent="CareerSystem/0.2 (research)")
            page = await context.new_page()
            try:
                emit(f"Chromium: opening {url}", tool="chromium", detail={"url": url})
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                title = await page.title()
                body = await page.locator("body").inner_text()
                emit(f"Chromium: read page '{title[:80]}' ({len(body)} chars)", tool="chromium")
                return PageContent(url=url, title=title, text=body[:max_chars], method="chromium")
            finally:
                await context.close()
                await browser.close()

    async def _fetch_http(self, url: str, max_chars: int) -> PageContent | None:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "CareerSystem/0.2"}) as client:
                emit(f"HTTP: fetching {url}", tool="http", detail={"url": url})
                resp = await client.get(url)
                resp.raise_for_status()
            text = _strip_html(resp.text)[:max_chars]
            emit(f"HTTP: fetched {len(text)} chars", tool="http")
            return PageContent(url=url, title=url, text=text, method="http")
        except Exception as exc:
            emit(f"Could not fetch page: {exc}", tool="http", level="warn")
            return None


def _model() -> ModelClient:
    return ModelClient()


async def research_company(job: dict, page: PageContent | None) -> dict:
    """Summarize the company/product/domain and the role's top requirements."""
    context = {
        "company": job["company"],
        "title": job["title"],
        "job_description": job["description"][:8000],
        "page_title": page.title if page else None,
        "page_text": page.text[:6000] if page else None,
    }
    prompt = (
        "You research a company for a job application. Using ONLY the provided job "
        "description and page text, produce a factual briefing. Do not invent facts. "
        "Return JSON with keys: company_summary (2-3 sentences), product, domain, "
        "key_technologies (array), top_requirements (array of the role's must-haves).\n\n"
        + str(context)
    )
    try:
        with step("Response Builder: summarizing company & role (AI)", tool="deepseek"):
            result = _model().complete_json("You are a precise company/role research assistant.", prompt)
    except Exception as exc:
        emit(f"Company research model call failed: {exc}", level="warn")
        result = None
    if not result:
        # Honest deterministic fallback: no model available.
        return {
            "company_summary": f"{job['company']} — see job posting; automated summary unavailable (no model).",
            "product": None,
            "domain": None,
            "key_technologies": [],
            "top_requirements": [],
            "source": page.method if page else "description_only",
        }
    result["source"] = page.method if page else "description_only"
    return result


async def analyze_resume_gaps(job: dict, candidate_facts: dict) -> dict:
    """Compare the role against the candidate's real evidence — strengths and gaps."""
    prompt = (
        "Compare this job against the candidate's real profile. Be honest and specific. "
        "Do NOT invent candidate experience. Return JSON with keys: strengths (array of "
        "the candidate's genuinely matching evidence), gaps (array of missing/weak areas "
        "for this role), project_recommended (boolean: would a small demonstrable project "
        "meaningfully close a gap?), project_idea (one concrete idea or empty string).\n\n"
        + str({"job": {"title": job["title"], "company": job["company"], "description": job["description"][:8000]},
               "candidate_facts": candidate_facts})
    )
    try:
        with step("Response Builder: finding resume strengths & gaps (AI)", tool="deepseek"):
            result = _model().complete_json("You are a rigorous, honest resume gap analyst.", prompt)
    except Exception as exc:
        emit(f"Gap analysis model call failed: {exc}", level="warn")
        result = None
    if not result:
        return {"strengths": [], "gaps": [], "project_recommended": False, "project_idea": "", "mode": "unavailable"}
    result.setdefault("project_recommended", False)
    return result


async def suggest_people(job: dict, company_research: dict) -> dict:
    """Suggest who to contact and an outreach angle (target roles, not fabricated names)."""
    prompt = (
        "For this job, suggest who the candidate should reach out to. Return JSON with keys: "
        "target_roles (array of role titles worth contacting, e.g. 'Engineering Manager', "
        "'Recruiter'), outreach_angle (one factual sentence on how to connect this candidate "
        "to the role), search_queries (array of search strings to find these people). "
        "Do NOT fabricate real names or emails.\n\n"
        + str({"company": job["company"], "title": job["title"],
               "domain": company_research.get("domain"),
               "top_requirements": company_research.get("top_requirements")})
    )
    try:
        with step("Response Builder: identifying people to contact (AI)", tool="deepseek"):
            result = _model().complete_json("You are a thoughtful outreach strategist.", prompt)
    except Exception as exc:
        emit(f"People suggestion model call failed: {exc}", level="warn")
        result = None
    if not result:
        return {"target_roles": ["Recruiter", "Hiring Manager"], "outreach_angle": "", "search_queries": [], "mode": "unavailable"}
    return result
