"""Drafts API — auto-reply settings, uploaded context files, and draft history.

Backs the Drafts page. General holds the master switch, the category gate, style,
and follow-ups; Signature holds the signature; Custom Files manages the uploads
that steer or inform drafting.

Two things this API deliberately does not offer. There is no "draft this message
now" endpoint — drafting is driven entirely by mail arriving and by the periodic
sweeps (`workers.jobs.drafts_sweep`). And there is
no draft history, because no draft is ever stored; the drafts live only in the
user's Gmail.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.concurrency import run_in_threadpool

from api.deps import DbSession
from core.config import settings as app_settings
from models.drafts import FILE_PURPOSES, MAX_FILE_CHARS, DraftFile, DraftSettings
from models.users import User
from schemas.drafts import (
    DraftFilePreview,
    DraftFileRead,
    DraftFileUpdate,
    DraftSettingsRead,
    DraftSettingsUpdate,
)
from services.auth.dependencies import get_current_user
from services.categorization.store import get_or_create_categories
from services.drafts import store
from services.drafts.extract import (
    SUPPORTED_EXTS,
    ExtractionError,
    FileTooLarge,
    UnsupportedFile,
    extract_text,
)

router = APIRouter(prefix="/drafts", tags=["drafts"])

CurrentUser = Annotated[User, Depends(get_current_user)]

# How much of a file's text the preview endpoint returns. Enough to tell a clean
# parse from a garbled one at a glance.
PREVIEW_CHARS = 1_000


@router.get("/settings", response_model=DraftSettingsRead)
async def get_settings(user: CurrentUser, db: DbSession) -> DraftSettings:
    return await store.get_or_create_settings(db, user.id)


@router.put("/settings", response_model=DraftSettingsRead)
async def update_settings(
    payload: DraftSettingsUpdate, user: CurrentUser, db: DbSession
) -> DraftSettings:
    row = await store.get_or_create_settings(db, user.id)
    data = payload.model_dump(exclude_unset=True)

    if (model := data.get("model")) is not None:
        allowed = app_settings.allowed_classifier_models
        if model not in allowed:
            raise HTTPException(422, f"model must be one of {sorted(allowed)}")

    if data.get("category_keys") is not None:
        # Every key must name a category this user actually has, or the gate would
        # silently never match and drafting would look broken rather than
        # misconfigured.
        known = {c.key for c in await get_or_create_categories(db, user.id)}
        unknown = [k for k in data["category_keys"] if k not in known]
        if unknown:
            raise HTTPException(422, f"no categories with keys {unknown}")

    # A user can legitimately turn drafting on with no categories selected, but
    # it would then never draft anything. Reject it rather than leave them
    # waiting on a feature that cannot fire.
    is_enabled = data.get("is_enabled", row.is_enabled)
    category_keys = data.get("category_keys", list(row.category_keys or []))
    if is_enabled and not category_keys:
        raise HTTPException(422, "select at least one category to draft replies for")

    for field, value in data.items():
        setattr(row, field, value)

    return row


@router.get("/files", response_model=list[DraftFileRead])
async def list_files(
    user: CurrentUser,
    db: DbSession,
    purpose: Annotated[str | None, Query()] = None,
) -> list[DraftFile]:
    """Uploaded files, newest first. Filter by `instruction` or `knowledge`."""

    if purpose is not None and purpose not in FILE_PURPOSES:
        raise HTTPException(422, f"purpose must be one of {sorted(FILE_PURPOSES)}")
    return await store.list_files(db, user.id, purpose)


@router.post("/files", response_model=DraftFileRead, status_code=status.HTTP_201_CREATED)
async def upload_file(
    user: CurrentUser,
    db: DbSession,
    file: Annotated[UploadFile, File()],
    purpose: Annotated[str, Form()],
) -> DraftFile:
    """Upload a file to steer (`instruction`) or inform (`knowledge`) drafting.

    Only the extracted text is kept — the original bytes are discarded once
    parsed, so there is nothing to download later. Extraction happens here rather
    than in a worker so a file that cannot be read fails in front of the user,
    while they still have it open, instead of silently doing nothing.
    """
    if purpose not in FILE_PURPOSES:
        raise HTTPException(422, f"purpose must be one of {sorted(FILE_PURPOSES)}")

    filename = (file.filename or "").strip()
    if not filename:
        raise HTTPException(422, "the upload has no filename")

    data = await file.read()

    try:
        # Blocking, CPU-bound parsing — off the event loop, or a large PDF stalls
        # every other request on this worker.
        text = await run_in_threadpool(extract_text, filename, data)
    except FileTooLarge as exc:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(exc)) from exc
    except UnsupportedFile as exc:
        raise HTTPException(415, str(exc)) from exc
    except ExtractionError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        # A corrupt or password-protected file raises from deep inside the parser.
        # Surfacing a 500 would read as our bug rather than their file.
        raise HTTPException(422, f"could not read {filename!r}: {exc}") from exc

    row = DraftFile(
        user_id=user.id,
        purpose=purpose,
        filename=filename[:255],
        content_type=(file.content_type or "application/octet-stream")[:128],
        size_bytes=len(data),
        extracted_text=text[:MAX_FILE_CHARS],
        char_count=min(len(text), MAX_FILE_CHARS),
        is_enabled=True,
    )
    db.add(row)
    await db.flush()
    return row


@router.get("/files/{file_id}/preview", response_model=DraftFilePreview)
async def preview_file(
    file_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> DraftFilePreview:
    """The head of a file's extracted text, so a bad parse is visible."""
    row = await store.get_file(db, user.id, file_id)
    if row is None:
        raise HTTPException(404, f"no file with id {file_id}")
    return DraftFilePreview(
        id=row.id,
        filename=row.filename,
        char_count=row.char_count,
        excerpt=row.extracted_text[:PREVIEW_CHARS],
    )


@router.patch("/files/{file_id}", response_model=DraftFileRead)
async def update_file(
    file_id: uuid.UUID, payload: DraftFileUpdate, user: CurrentUser, db: DbSession
) -> DraftFile:
    """Toggle a file off without deleting it, or move it between purposes."""
    row = await store.get_file(db, user.id, file_id)
    if row is None:
        raise HTTPException(404, f"no file with id {file_id}")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    return row


@router.delete("/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(file_id: uuid.UUID, user: CurrentUser, db: DbSession) -> None:
    row = await store.get_file(db, user.id, file_id)
    if row is None:
        raise HTTPException(404, f"no file with id {file_id}")
    await db.delete(row)


@router.get("/supported-file-types", response_model=list[str])
async def supported_file_types() -> list[str]:
    """What the upload accepts, so the file picker and the parser cannot drift."""
    return list(SUPPORTED_EXTS)
