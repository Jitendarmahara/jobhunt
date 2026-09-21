"""High-resolution activity tracing — the observability backbone.

Every agent and tool emits events here. The live UI streams them so a human can
watch, in real time, which agent is running, what tool call it makes (DeepSeek,
chromium, GitHub, Gmail), how many tokens it used, and what it cost.

Design notes:
- ``emit()`` writes one durable row (own short-lived session) so it works from
  any process (API or Temporal worker) and the live stream can poll by ``id``.
- A ``contextvars`` trace context lets nested code (e.g. ``ModelClient``) attach
  the current agent/job automatically without threading arguments everywhere.
"""

from __future__ import annotations

import contextvars
import time
from contextlib import contextmanager

from app.db import SessionLocal
from app.models import AgentEvent

# Estimated model prices (USD per 1M tokens): (input, output). Clearly estimates.
_MODEL_PRICES: dict[str, tuple[float, float]] = {
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    "deepseek-flash": (0.075, 0.30),
}
_DEFAULT_PRICE = (0.27, 1.10)

_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("agent_trace_ctx", default={})


def set_context(*, trace_id: str | None = None, job_id: str | None = None, agent: str | None = None) -> None:
    current = dict(_ctx.get())
    if trace_id is not None:
        current["trace_id"] = trace_id
    if job_id is not None:
        current["job_id"] = job_id
    if agent is not None:
        current["agent"] = agent
    _ctx.set(current)


def get_context() -> dict:
    return dict(_ctx.get())


def estimate_cost(model: str | None, tokens_in: int, tokens_out: int) -> float:
    price_in, price_out = _MODEL_PRICES.get((model or "").lower(), _DEFAULT_PRICE)
    return round((tokens_in / 1_000_000) * price_in + (tokens_out / 1_000_000) * price_out, 6)


def emit(
    title: str,
    *,
    tool: str | None = None,
    phase: str = "info",
    detail: dict | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
    duration_ms: int | None = None,
    level: str = "info",
    agent: str | None = None,
    job_id: str | None = None,
    trace_id: str | None = None,
) -> None:
    ctx = _ctx.get()
    row = AgentEvent(
        trace_id=trace_id or ctx.get("trace_id"),
        job_id=job_id or ctx.get("job_id"),
        agent=agent or ctx.get("agent") or "system",
        tool=tool,
        phase=phase,
        title=title[:1000],
        detail=detail or {},
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        level=level,
    )
    try:
        with SessionLocal() as session:
            session.add(row)
            session.commit()
    except Exception:
        # Observability must never break the pipeline it observes.
        pass


@contextmanager
def step(title: str, *, agent: str | None = None, tool: str | None = None, detail: dict | None = None):
    """Emit a start event, then an end (with duration) or an error event."""
    emit(title, agent=agent, tool=tool, phase="start", detail=detail)
    started = time.perf_counter()
    try:
        yield
    except Exception as exc:
        emit(
            f"{title}: error — {exc}",
            agent=agent,
            tool=tool,
            phase="error",
            level="error",
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        raise
    else:
        emit(
            f"{title}: done",
            agent=agent,
            tool=tool,
            phase="end",
            duration_ms=int((time.perf_counter() - started) * 1000),
        )


def event_to_dict(row: AgentEvent) -> dict:
    return {
        "id": row.id,
        "trace_id": row.trace_id,
        "job_id": row.job_id,
        "agent": row.agent,
        "tool": row.tool,
        "phase": row.phase,
        "title": row.title,
        "detail": row.detail,
        "tokens_in": row.tokens_in,
        "tokens_out": row.tokens_out,
        "cost_usd": row.cost_usd,
        "duration_ms": row.duration_ms,
        "level": row.level,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
