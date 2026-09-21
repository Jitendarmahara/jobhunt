from temporalio.client import Client

from app.config import get_settings
from app.workflows import (
    ApplicationWorkflow,
    DiscoveryWorkflow,
    JobLifecycleWorkflow,
    OutreachWorkflow,
    PollJobSourceWorkflow,
)


async def _client() -> Client:
    settings = get_settings()
    return await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)


async def start_job_lifecycle(job_id: str) -> str:
    settings = get_settings()
    workflow_id = f"job-lifecycle-{job_id}"
    client = await _client()
    await client.start_workflow(JobLifecycleWorkflow.run, job_id, id=workflow_id, task_queue=settings.temporal_task_queue)
    return workflow_id


async def start_discovery(source_id: str) -> str:
    settings = get_settings()
    workflow_id = f"discovery-{source_id}"
    client = await _client()
    await client.start_workflow(DiscoveryWorkflow.run, source_id, id=workflow_id, task_queue=settings.temporal_task_queue)
    return workflow_id


async def start_application(application_id: str) -> str:
    settings = get_settings()
    workflow_id = f"application-{application_id}"
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    await client.start_workflow(ApplicationWorkflow.run, application_id, id=workflow_id, task_queue=settings.temporal_task_queue)
    return workflow_id


async def start_source_poll(source_id: str) -> str:
    settings = get_settings()
    workflow_id = f"source-poll-{source_id}"
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    await client.start_workflow(PollJobSourceWorkflow.run, source_id, id=workflow_id, task_queue=settings.temporal_task_queue)
    return workflow_id


async def start_outreach(message_id: str) -> str:
    settings = get_settings()
    workflow_id = f"outreach-{message_id}"
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    await client.start_workflow(OutreachWorkflow.run, message_id, id=workflow_id, task_queue=settings.temporal_task_queue)
    return workflow_id


async def resolve_application(application_id: str, resolution: str) -> None:
    settings = get_settings()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    handle = client.get_workflow_handle(f"application-{application_id}")
    await handle.signal(ApplicationWorkflow.resolve, resolution)
