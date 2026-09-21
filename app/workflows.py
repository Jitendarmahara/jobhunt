from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from app.activities import execute_application_browser_activity, poll_source_activity, prepare_application_activity, send_outreach_activity


@workflow.defn
class PollJobSourceWorkflow:
    @workflow.run
    async def run(self, source_id: str) -> dict:
        return await workflow.execute_activity(poll_source_activity, source_id, start_to_close_timeout=timedelta(minutes=2))


@workflow.defn
class ApplicationWorkflow:
    def __init__(self) -> None:
        self.resolution: str | None = None

    @workflow.run
    async def run(self, application_id: str) -> dict:
        preflight = await workflow.execute_activity(prepare_application_activity, application_id, start_to_close_timeout=timedelta(minutes=1))
        if preflight["status"] != "ready":
            return preflight
        result = await workflow.execute_activity(execute_application_browser_activity, application_id, start_to_close_timeout=timedelta(minutes=5))
        if result["status"] in ("needs_escalation", "unavailable"):
            await workflow.wait_condition(lambda: self.resolution is not None)
            return {"status": "resolved_by_human", "resolution": self.resolution}
        return result

    @workflow.signal
    def resolve(self, resolution: str) -> None:
        self.resolution = resolution


@workflow.defn
class OutreachWorkflow:
    @workflow.run
    async def run(self, message_id: str) -> dict:
        return await workflow.execute_activity(send_outreach_activity, message_id, start_to_close_timeout=timedelta(minutes=2))
