"""Domain-level exceptions raised by the service layer (评审 E2).

Three service files (`services/alignment/alignment_service.py`,
`services/matrix/bid_export_service.py`, `services/submission/
quote_confirmation_service.py`) previously `raise HTTPException(...)` directly
— HTTP is a routing/transport concern, but it was mixed into business logic,
scattered across 21 independent call sites. Nothing enforced that the same
semantic ("data needs human review before it can proceed", "referenced entity
missing") picked the same status code twice.

Services now raise the domain exceptions below; routes and `main.py` never
see HTTP status codes chosen inside a service function. `register_exception_
handlers()` (called once from main.py) maps each domain exception to a
status code — that mapping lives in exactly one place.

**This module only does the mechanism migration (E2), not the policy fix
(E1).** Each of the 21 migrated call sites keeps the exact status code it
already returned — mapping the exception *type* to match, not deciding
whether that code was the right choice. Whether "no confirmed session" should
be 400 vs 404, or "quality gate failed" should be 409 vs 422 uniformly, is
evaluation E1's job, done as a separate, deliberate policy change on top of
this.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class DomainError(Exception):
    """Base class for business-rule violations that should become HTTP errors.

    `detail` may be a bare string or a structured dict — both forms already
    exist across the migrated call sites; this batch preserves that as-is.
    Standardizing the shape is evaluation E3's job, not this one's.
    """

    status_code: int = 500

    def __init__(self, detail: Any):
        self.detail = detail
        super().__init__(detail if isinstance(detail, str) else str(detail))


class ValidationError(DomainError):
    """400 — request/state doesn't satisfy a precondition the caller controls."""

    status_code = 400


class NotFoundError(DomainError):
    """404 — referenced entity doesn't exist."""

    status_code = 404


class ConflictError(DomainError):
    """409 — request is valid but conflicts with current server-side state."""

    status_code = 409


class ReviewRequiredError(DomainError):
    """422 — data was accepted but needs human review before it can proceed
    (structural integrity gate, checksum gate, missing-total gate, ...)."""

    status_code = 422


def register_exception_handlers(app: FastAPI) -> None:
    """Wire DomainError → HTTP response. Call once from main.py at startup.

    Response shape matches FastAPI's own default HTTPException handler
    exactly (`{"detail": ...}`), so this is a zero-wire-format-change swap
    for every call site it replaces.
    """

    @app.exception_handler(DomainError)
    async def _domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
