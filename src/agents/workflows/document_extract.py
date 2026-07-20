"""Document extraction workflow: pull structured data from documents."""

from agents.runtime.state import RunState


async def run(inputs: dict) -> RunState:
    state = RunState(workflow="document_extract", inputs=inputs)
    # TODO: implement extraction steps.
    return state
