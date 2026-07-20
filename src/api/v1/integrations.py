"""Integration routes (API v1) — Gmail via Composio."""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool

from integrations.composio import gmail
from models.users import User
from schemas.integrations import GmailConnect, GmailStatus, SyncQueued, SyncResult
from services.auth.dependencies import get_current_user
from workers.celery_app import celery_app
from workers.jobs.sync_last_7_days import sync_last_7_days

router = APIRouter(prefix="/integrations", tags=["integrations"])

CurrentUser = Annotated[User, Depends(get_current_user)]


@router.get("/gmail/status", response_model=GmailStatus)
async def gmail_status(user: CurrentUser) -> GmailStatus:
    """Whether the current user has an ACTIVE Composio Gmail connection."""
    connected = await run_in_threadpool(gmail.is_connected, str(user.id))
    return GmailStatus(connected=connected)


@router.get("/gmail/connect", response_model=GmailConnect)
async def gmail_connect(user: CurrentUser) -> GmailConnect:
    """Start the Gmail OAuth grant; returns a URL to send the user to."""
    redirect_url = await run_in_threadpool(gmail.initiate_connection, str(user.id))
    return GmailConnect(redirect_url=redirect_url)


@router.post("/gmail/sync", status_code=202, response_model=SyncQueued)
async def gmail_sync(user: CurrentUser) -> SyncQueued:
    """Queue a background sync of the last 7 days of email for the current user."""
    task = sync_last_7_days.delay(str(user.id))
    return SyncQueued(task_id=task.id)


@router.get("/gmail/sync/{task_id}", response_model=SyncResult)
async def gmail_sync_result(task_id: str, user: CurrentUser) -> SyncResult:
    """Poll a queued sync: PENDING/STARTED/SUCCESS/FAILURE and its result."""
    result = celery_app.AsyncResult(task_id)
    return SyncResult(
        task_id=task_id,
        status=result.status,
        result=result.result if result.successful() else None,
        error=str(result.result) if result.failed() else None,
    )
