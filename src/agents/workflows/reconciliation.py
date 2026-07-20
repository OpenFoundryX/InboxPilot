"""Reconciliation workflow: match records across sources."""

from agents.runtime.state import RunState


async def run(inputs: dict) -> RunState:
    state = RunState(workflow="reconciliation", inputs=inputs)
    # TODO: implement reconciliation steps.
    return state
