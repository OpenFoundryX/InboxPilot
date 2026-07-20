"""Orchestration loop / graph runner for agent workflows."""

from agents.runtime.state import RunState


class Executor:
    """Drives a workflow to completion, calling tools and the LLM."""

    async def run(self, workflow: str, inputs: dict) -> RunState:
        state = RunState(workflow=workflow, inputs=inputs)
        # TODO: implement the orchestration loop.
        return state
