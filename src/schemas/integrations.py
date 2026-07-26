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
