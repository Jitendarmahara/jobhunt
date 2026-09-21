from dataclasses import dataclass
from datetime import datetime, time, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Application, OutreachMessage


class PolicyBlockedError(ValueError):
    pass


@dataclass(frozen=True)
class LivePolicy:
    enabled: bool
    daily_application_limit: int
    daily_outreach_limit: int


def _day_start() -> datetime:
    now = datetime.now(timezone.utc)
    return datetime.combine(now.date(), time.min, tzinfo=timezone.utc)


def enforce_application_policy(session: Session, policy: LivePolicy) -> None:
    if not policy.enabled or policy.daily_application_limit == 0:
        raise PolicyBlockedError("Live application submission is disabled by policy.")
    sent = session.scalar(
        select(func.count()).select_from(Application).where(
            Application.submitted_at >= _day_start(),
        )
    )
    if (sent or 0) >= policy.daily_application_limit:
        raise PolicyBlockedError("Daily application limit has been reached.")


def enforce_outreach_policy(session: Session, policy: LivePolicy) -> None:
    if not policy.enabled or policy.daily_outreach_limit == 0:
        raise PolicyBlockedError("Live outreach is disabled by policy.")
    sent = session.scalar(
        select(func.count()).select_from(OutreachMessage).where(
            OutreachMessage.sent_at >= _day_start(),
        )
    )
    if (sent or 0) >= policy.daily_outreach_limit:
        raise PolicyBlockedError("Daily outreach limit has been reached.")


def unknown_answer_requires_escalation(candidate_facts: dict, question_key: str) -> bool:
    """Application workers must call this rather than infer answers outside facts."""
    answers = candidate_facts.get("application_answers", {})
    return question_key not in answers or answers[question_key] in (None, "")

