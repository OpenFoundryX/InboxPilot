"""Run state and checkpointing for agent executions."""

from dataclasses import dataclass, field


@dataclass
class RunState:
    workflow: str
    inputs: dict
    steps: list[dict] = field(default_factory=list)
    output: dict | None = None
    done: bool = False
