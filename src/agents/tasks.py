"""Celery tasks that kick off long-running agent runs."""

from workers.celery_app import celery_app


@celery_app.task(name="agents.run_workflow")
def run_workflow(workflow: str, inputs: dict | None = None) -> dict:
    # TODO: dispatch to agents.runtime.executor
    return {"workflow": workflow, "status": "not_implemented"}
