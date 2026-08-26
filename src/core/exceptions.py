from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse


class AppError(Exception):
    """Base application exception."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    detail: str = "Application error"

    def __init__(self, detail: str | None = None) -> None:
        if detail is not None:
            self.detail = detail
        super().__init__(self.detail)


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    detail = "Resource not found"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    detail = "Resource conflict"


class UnauthorizedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    detail = "Not authenticated"


class SignupNotInvited(AppError):
    """A new Google identity with no invite.

    403 rather than 401: the caller authenticated fine with Google, they are
    just not allowed to create an account. The OAuth callback catches this and
    redirects instead of letting the handler render it, so the status code
    matters only if some future caller lets it escape.
    """

    status_code = status.HTTP_403_FORBIDDEN
    detail = "Signups are invite-only"

    def __init__(self, email: str) -> None:
        self.email = email
        super().__init__(f"{self.detail}: {email}")


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )
