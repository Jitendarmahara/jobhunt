import asyncio
import logging
from datetime import datetime, timezone

from temporalio.client import Client
from temporalio.worker import Worker

from app.activities import (
    analyze_gaps_activity,
    create_application_activity,
    execute_application_browser_activity,
    find_people_activity,
    generate_resume_activity,
    lifecycle_submit_application_activity,
    list_enabled_sources_activity,
    mark_normalized_activity,
    match_candidate_activity,
    poll_source_activity,
    prepare_application_activity,
    qualify_job_activity,
    research_company_activity,
    send_outreach_activity,
)
from app.config import get_settings
from app.db import initialize_database
from app.scheduler import ensure_discovery_schedule
from app.workflows import (
    ApplicationWorkflow,
    DiscoveryWorkflow,
    JobLifecycleWorkflow,
    OutreachWorkflow,
    PollAllSourcesWorkflow,
    PollJobSourceWorkflow,
)


async def _heartbeat_loop() -> None:
    """Write a durable liveness marker so the UI can show 'Worker: running'."""
    from app.db import SessionLocal
    from app.models import SystemSetting

    while True:
        try:
            with SessionLocal() as session:
                value = {"ts": datetime.now(timezone.utc).isoformat()}
                setting = session.get(SystemSetting, "worker_heartbeat")
                if setting:
                    setting.value = value
                else:
                    session.add(SystemSetting(key="worker_heartbeat", value=value))
                session.commit()
        except Exception:  # never let heartbeat failures crash the worker
            pass
        await asyncio.sleep(10)


async def run() -> None:
    settings = get_settings()
    initialize_database()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    await ensure_discovery_schedule(client)
    asyncio.create_task(_heartbeat_loop())
    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[
            PollJobSourceWorkflow,
            ApplicationWorkflow,
            OutreachWorkflow,
            JobLifecycleWorkflow,
            DiscoveryWorkflow,
            PollAllSourcesWorkflow,
        ],
        activities=[
            poll_source_activity,
            prepare_application_activity,
            execute_application_browser_activity,
            send_outreach_activity,
            list_enabled_sources_activity,
            mark_normalized_activity,
            qualify_job_activity,
            research_company_activity,
            analyze_gaps_activity,
            find_people_activity,
            match_candidate_activity,
            generate_resume_activity,
            create_application_activity,
            lifecycle_submit_application_activity,
        ],
    )
    await worker.run()


if __name__ == "__main__":
    logging.basicConfig(level=get_settings().log_level)
    asyncio.run(run())
