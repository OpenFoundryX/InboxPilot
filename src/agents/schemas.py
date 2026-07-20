"""Shared schemas for agent runs, tool calls, and results."""

from pydantic import BaseModel


class AgentRunRequest(BaseModel):
    workflow: str
    inputs: dict = {}


class AgentRunResult(BaseModel):
    run_id: str
    status: str
    output: dict | None = None
