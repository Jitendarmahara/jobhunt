"""Autonomous discovery scheduling.

Registers a Temporal Schedule that periodically runs ``PollAllSourcesWorkflow``,
which fans out a ``DiscoveryWorkflow`` per enabled source, which in turn launches
a ``JobLifecycleWorkflow`` per newly discovered job. This is what makes the
system run automatically without a human polling sources.
"""

import logging
from datetime import timedelta

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleIntervalSpec,
    ScheduleSpec,
    ScheduleState,
)

from app.config import get_settings
from app.workflows import PollAllSourcesWorkflow

logger = logging.getLogger(__name__)

SCHEDULE_ID = "career-poll-all-sources"


async def ensure_discovery_schedule(client: Client) -> bool:
    """Create the recurring discovery schedule if it does not already exist.

    Returns True if a schedule was created, False if it already existed or
    auto-discovery is disabled. Idempotent and safe to call on every startup.
    """
    settings = get_settings()
    if not settings.auto_discovery_enabled:
        logger.info("Auto-discovery disabled; not registering schedule.")
        return False

    interval = max(1, settings.discovery_interval_minutes)
    try:
        await client.create_schedule(
            SCHEDULE_ID,
            Schedule(
                action=ScheduleActionStartWorkflow(
                    PollAllSourcesWorkflow.run,
                    id="poll-all-sources",
                    task_queue=settings.temporal_task_queue,
                ),
                spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(minutes=interval))]),
                state=ScheduleState(note="Autonomous source polling", paused=False),
            ),
        )
        logger.info("Registered discovery schedule '%s' every %s minute(s).", SCHEDULE_ID, interval)
        return True
    except Exception as exc:  # AlreadyExists on restart, or transient RPC error.
        logger.info("Discovery schedule not created (likely already exists): %s", exc)
        return False
