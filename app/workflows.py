from datetime import timedelta

from temporalio import workflow
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from app.activities import (
        analyze_gaps_activity,
        build_project_activity,
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

# Shared retry defaults are Temporal's; timeouts are per-activity below.
_QUICK = timedelta(seconds=30)
_STD = timedelta(minutes=2)
_POLL = timedelta(minutes=3)
_BROWSER = timedelta(minutes=6)


@workflow.defn
class PollJobSourceWorkflow:
    @workflow.run
    async def run(self, source_id: str) -> dict:
        return await workflow.execute_activity(poll_source_activity, source_id, start_to_close_timeout=_STD)


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
        return await workflow.execute_activity(send_outreach_activity, message_id, start_to_close_timeout=_STD)


@workflow.defn
class JobLifecycleWorkflow:
    """Durable, policy-gated autonomous lifecycle for a single job.

    Temporal owns control flow; each step is a bounded activity returning a
    structured decision. The workflow stops safely at APPLICATION_READY when
    live actions are disabled — no external action fires until the operator
    arms the policy. No per-stage human approval is required in the happy path.
    """

    @workflow.run
    async def run(self, job_id: str) -> dict:
        await workflow.execute_activity(mark_normalized_activity, job_id, start_to_close_timeout=_STD)

        qualified = await workflow.execute_activity(qualify_job_activity, job_id, start_to_close_timeout=_STD)
        if qualified.get("status") == "failed":
            return {"job_id": job_id, "status": "failed", "stage": "qualify", "reason": qualified.get("reason")}
        if not qualified.get("qualified"):
            return {"job_id": job_id, "status": "skipped", "score": qualified.get("score")}

        # Response Builder: research the company (chromium), then match evidence.
        await workflow.execute_activity(research_company_activity, job_id, start_to_close_timeout=_BROWSER)
        await workflow.execute_activity(match_candidate_activity, job_id, start_to_close_timeout=_STD)
        # Resume strengths/gaps and people to contact (best-effort annotations).
        gap_result = await workflow.execute_activity(analyze_gaps_activity, job_id, start_to_close_timeout=_STD)
        await workflow.execute_activity(find_people_activity, job_id, start_to_close_timeout=_STD)

        # Project Builder: only when the gap analysis found a material, closeable gap.
        if gap_result.get("gaps", {}).get("project_recommended"):
            project = await workflow.execute_activity(build_project_activity, job_id, start_to_close_timeout=_POLL)
            if project.get("status") == "failed":
                return {"job_id": job_id, "status": "failed", "stage": "project", "reason": project.get("reason")}

        resume = await workflow.execute_activity(generate_resume_activity, job_id, start_to_close_timeout=_STD)
        if resume.get("status") == "failed":
            return {"job_id": job_id, "status": "failed", "stage": "resume", "reason": resume.get("reason")}

        application = await workflow.execute_activity(
            create_application_activity,
            args=[job_id, resume.get("resume_artifact_id")],
            start_to_close_timeout=_STD,
        )
        if application.get("status") == "failed":
            return {"job_id": job_id, "status": "failed", "stage": "application", "reason": application.get("reason")}

        if not application.get("live_submit"):
            # Safe-by-default: the pipeline ran end-to-end but no external action
            # is taken until live actions + a daily cap are enabled.
            return {
                "job_id": job_id,
                "status": "ready_awaiting_live_actions",
                "application_id": application.get("application_id"),
            }

        result = await workflow.execute_activity(
            lifecycle_submit_application_activity,
            args=[job_id, application["application_id"]],
            start_to_close_timeout=_BROWSER,
        )
        return {
            "job_id": job_id,
            "status": result.get("status"),
            "application_id": application.get("application_id"),
        }


@workflow.defn
class DiscoveryWorkflow:
    """Poll one source and launch an autonomous lifecycle per newly discovered job."""

    @workflow.run
    async def run(self, source_id: str) -> dict:
        result = await workflow.execute_activity(poll_source_activity, source_id, start_to_close_timeout=_POLL)
        new_job_ids = result.get("new_job_ids", []) if isinstance(result, dict) else []
        started = 0
        for job_id in new_job_ids:
            await workflow.start_child_workflow(
                JobLifecycleWorkflow.run,
                job_id,
                id=f"job-lifecycle-{job_id}",
                task_queue=workflow.info().task_queue,
                parent_close_policy=ParentClosePolicy.ABANDON,
            )
            started += 1
        return {
            "source_id": source_id,
            "fetched": result.get("fetched", 0) if isinstance(result, dict) else 0,
            "new": len(new_job_ids),
            "lifecycles_started": started,
        }


@workflow.defn
class PollAllSourcesWorkflow:
    """Fan out a DiscoveryWorkflow for every enabled source. Driven by a schedule."""

    @workflow.run
    async def run(self) -> dict:
        source_ids = await workflow.execute_activity(list_enabled_sources_activity, start_to_close_timeout=_QUICK)
        parent_id = workflow.info().workflow_id
        for source_id in source_ids:
            await workflow.start_child_workflow(
                DiscoveryWorkflow.run,
                source_id,
                id=f"discovery-{source_id}-{parent_id}",
                task_queue=workflow.info().task_queue,
                parent_close_policy=ParentClosePolicy.ABANDON,
            )
        return {"sources": len(source_ids)}
