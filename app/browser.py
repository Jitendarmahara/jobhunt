"""Reusable browser execution primitive for direct ATS application workers.

The browser never decides facts. It receives only resolved values from the
Application Agent and stops when required fields have no factual answer.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import get_settings


@dataclass(frozen=True)
class BrowserResult:
    status: str
    evidence_uri: str | None = None
    unknown_questions: tuple[str, ...] = ()
    provider_submission_id: str | None = None
    detail: str | None = None


def _normalise_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _answer_map(facts: dict[str, Any]) -> dict[str, str]:
    answers = {_normalise_label(key): str(value) for key, value in facts.get("application_answers", {}).items() if value not in (None, "")}
    for key in ("first_name", "last_name", "name", "email", "phone", "location", "linkedin", "github", "website"):
        if facts.get(key) not in (None, ""):
            answers[key] = str(facts[key])
    return answers


class PlaywrightApplicationBrowser:
    async def inspect_and_submit(self, application_url: str, candidate_facts: dict[str, Any], application_id: str) -> BrowserResult:
        settings = get_settings()
        if not settings.browser_enabled:
            return BrowserResult(status="unavailable", detail="BROWSER_ENABLED is false")
        # Lazy import permits control-plane/API containers to stay browser-free.
        from playwright.async_api import async_playwright

        artifact_dir = Path(settings.artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        screenshot = artifact_dir / f"application-{application_id}.png"
        answers = _answer_map(candidate_facts)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            try:
                await page.goto(application_url, wait_until="domcontentloaded", timeout=45_000)
                await page.screenshot(path=str(screenshot), full_page=True)
                fields = await page.locator("input, textarea, select").evaluate_all(
                    """els => els.map((el, index) => ({
                      index, name: el.name, type: el.type, required: el.required,
                      aria: el.getAttribute('aria-label'), placeholder: el.getAttribute('placeholder'),
                      label: (el.labels && el.labels[0] && el.labels[0].innerText) || ''
                    }))"""
                )
                unknown: list[str] = []
                for field in fields:
                    if field.get("type") in ("hidden", "submit", "button", "checkbox", "radio", "file"):
                        continue
                    key = _normalise_label(field.get("label") or field.get("aria") or field.get("name") or field.get("placeholder") or "")
                    value = answers.get(key)
                    if not value:
                        # Common ATS names vary, so only use a narrow factual alias map.
                        aliases = {"first_name": "first_name", "last_name": "last_name", "email": "email", "phone": "phone"}
                        value = answers.get(aliases.get(key, ""))
                    if not value:
                        if field.get("required"):
                            unknown.append(field.get("label") or field.get("aria") or field.get("name") or "unnamed required field")
                        continue
                    locator = page.locator("input, textarea, select").nth(field["index"])
                    await locator.fill(value)
                if unknown:
                    return BrowserResult(status="needs_escalation", evidence_uri=f"file://{screenshot}", unknown_questions=tuple(unknown))

                resume_path = candidate_facts.get("resume_path")
                file_fields = page.locator('input[type="file"]')
                if await file_fields.count() and resume_path:
                    if not Path(str(resume_path)).is_file():
                        return BrowserResult(status="needs_escalation", evidence_uri=f"file://{screenshot}", unknown_questions=("Configured resume file is unavailable to browser worker",))
                    await file_fields.first.set_input_files(str(resume_path))
                elif await file_fields.count():
                    return BrowserResult(status="needs_escalation", evidence_uri=f"file://{screenshot}", unknown_questions=("Resume upload is required",))

                submit = page.locator('button[type="submit"], input[type="submit"]').first
                if await submit.count() == 0:
                    return BrowserResult(status="needs_escalation", evidence_uri=f"file://{screenshot}", unknown_questions=("Could not identify a submit control",))
                await submit.click()
                await page.wait_for_timeout(1_500)
                await page.screenshot(path=str(screenshot), full_page=True)
                body = (await page.locator("body").inner_text()).lower()
                confirmation_words = ("thank you", "application submitted", "application received")
                if not any(word in body for word in confirmation_words):
                    return BrowserResult(status="needs_escalation", evidence_uri=f"file://{screenshot}", unknown_questions=("Submission confirmation could not be verified",))
                return BrowserResult(status="submitted", evidence_uri=f"file://{screenshot}")
            finally:
                await context.close()
                await browser.close()

