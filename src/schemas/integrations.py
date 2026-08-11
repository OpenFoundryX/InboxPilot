from pydantic import BaseModel


class GmailStatus(BaseModel):
    connected: bool
    # Whether a Gmail trigger exists, i.e. whether new mail actually reaches us.
    # Connected but not listening means nothing is being processed.
    listening: bool = False


class GmailConnect(BaseModel):
    redirect_url: str


class CalendarStatus(BaseModel):
    connected: bool


class CalendarConnect(BaseModel):
    redirect_url: str


class GoogleConnect(BaseModel):
    redirect_url: str


class GoogleStatus(BaseModel):
    """The single Google grant behind both Gmail and Calendar.

    `gmail` and `calendar` are reported separately because incremental auth
    means a user can end up holding one and not the other, and "reconnect"
    is only the right prompt when `needs_reconnect` is set — a grant that was
    revoked reads differently from one that was never given.
    """

    connected: bool
    gmail: bool = False
    calendar: bool = False
    listening: bool = False
    needs_reconnect: bool = False
    email: str | None = None
