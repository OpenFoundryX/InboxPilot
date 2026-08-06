"""Daily notes — a scratchpad per calendar day.

Two routes: read a window of days, and save one. Dates come from the client,
which is the point — see `models/notes.py` for why the server does no timezone
work here.
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, select

from api.deps import DbSession
from core.logging import get_logger
from models.notes import DailyNote
from models.users import User
from schemas.notes import DailyNoteRead, DailyNoteWrite
from services.auth.dependencies import get_current_user

log = get_logger(__name__)

router = APIRouter(prefix="/notes", tags=["notes"])

CurrentUser = Annotated[User, Depends(get_current_user)]

#: The widest window one request may ask for. The client asks for the days it is
#: actually showing, so anything beyond this is a bug or a scrape rather than a
#: need — and an uncapped range lets one request read a user's entire history.
MAX_RANGE_DAYS = 120


@router.get("", response_model=list[DailyNoteRead])
async def list_notes(
    user: CurrentUser,
    db: DbSession,
    from_date: Annotated[date, Query(alias="from")],
    to_date: Annotated[date, Query(alias="to")],
) -> list[DailyNote]:
    """Days with something written on them, oldest first.

    Days with nothing written are absent from the response rather than returned
    as blanks. The client is rendering a contiguous window of dates either way,
    so it already knows which days it asked about — sending back empties would
    be the server inventing rows that do not exist.
    """
    if to_date < from_date:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "`to` is before `from`")
    if (to_date - from_date).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Ask for at most {MAX_RANGE_DAYS} days at a time",
        )

    rows = await db.scalars(
        select(DailyNote)
        .where(
            DailyNote.user_id == user.id,
            DailyNote.note_date >= from_date,
            DailyNote.note_date <= to_date,
        )
        .order_by(DailyNote.note_date)
    )
    return list(rows)


@router.put("/{note_date}", response_model=DailyNoteRead)
async def save_note(
    note_date: date, payload: DailyNoteWrite, user: CurrentUser, db: DbSession
) -> DailyNoteRead:
    """Write a day's notes, or clear the day.

    Emptying a day deletes its row rather than storing a blank. The page is
    scrolled through far more than it is typed into, so keeping empties would
    accumulate a row for every day anyone ever passed. The response shape is the
    same either way, so the client does not have to care which happened.
    """
    body = payload.body.strip()

    if not body:
        await db.execute(
            delete(DailyNote).where(
                DailyNote.user_id == user.id, DailyNote.note_date == note_date
            )
        )
        return DailyNoteRead(note_date=note_date, body="", updated_at=None)

    row = await db.scalar(
        select(DailyNote).where(
            DailyNote.user_id == user.id, DailyNote.note_date == note_date
        )
    )
    if row is None:
        row = DailyNote(user_id=user.id, note_date=note_date, body=body)
        db.add(row)
    else:
        row.body = body
    await db.flush()
    # `created_at`/`updated_at` are server-side defaults, so the flush leaves
    # them unloaded on a new row and stale on an updated one. Serializing would
    # then reach for `updated_at` and trigger a lazy load from sync code —
    # which on an async session raises MissingGreenlet rather than fetching.
    # Refreshing here is what makes the returned timestamp both present and true.
    await db.refresh(row)

    return DailyNoteRead.model_validate(row)
