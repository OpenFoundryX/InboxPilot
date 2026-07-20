from agents.runtime.state import RunState


def test_run_state_defaults():
    state = RunState(workflow="demo", inputs={})
    assert state.done is False
    assert state.steps == []
