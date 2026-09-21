"""Single OpenAI-compatible model boundary with per-call metadata capture."""

import json
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings


@dataclass(frozen=True)
class ModelResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class ModelClient:
    def complete(self, system: str, prompt: str, *, temperature: float = 0.2) -> ModelResult | None:
        settings = get_settings()
        if not (settings.model_base_url and settings.model_name):
            return None
        endpoint = f"{settings.model_base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if settings.model_api_key:
            headers["Authorization"] = f"Bearer {settings.model_api_key}"
        response = httpx.post(
            endpoint,
            headers=headers,
            json={
                "model": settings.model_name,
                "temperature": temperature,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        usage = payload.get("usage") or {}
        result = ModelResult(
            text=payload["choices"][0]["message"]["content"],
            model=payload.get("model", settings.model_name),
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
        )
        try:
            from app.events import emit, estimate_cost

            cost = estimate_cost(result.model, result.input_tokens, result.output_tokens)
            emit(
                f"AI model call → {result.model}",
                tool="deepseek",
                tokens_in=result.input_tokens,
                tokens_out=result.output_tokens,
                cost_usd=cost,
                detail={"system": system[:160], "prompt_preview": prompt[:200], "reply_preview": result.text[:200]},
            )
        except Exception:
            pass
        return result

    def complete_json(self, system: str, prompt: str) -> dict[str, Any] | None:
        result = self.complete(system, prompt + "\nReturn only one JSON object.")
        if result is None:
            return None
        text = result.text.strip().removeprefix("```json").removesuffix("```").strip()
        return json.loads(text)

