"""Public unsubscribe endpoints and the authenticated suppression-list API.

``/unsubscribe/{token}`` is deliberately outside ``/api`` so recipients can reach it without
a bearer token. The token is an HMAC-signed hash of the address, so it cannot be forged and
reveals no readable address.

* ``GET``  shows a confirmation page. It never changes state, because mail scanners and
  link-preview bots fetch links automatically.
* ``POST`` records the unsubscribe. This also serves RFC 8058 one-click requests
  (``List-Unsubscribe=One-Click``) sent by Gmail, Yahoo and other mailbox providers.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from html import escape
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.entities import EmailSuppression, EmailSuppressionReason
from app.services.email_compliance import (
    ComplianceConfigurationError,
    add_suppression,
    get_compliance_config,
    verify_unsubscribe_token,
)

logger = logging.getLogger("lead_agent.api.email_compliance")

public_router = APIRouter(tags=["Email Unsubscribe"])
router = APIRouter(prefix="/api/email-suppressions", tags=["Email Suppression List"])

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>{title}</title>
<style>body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#f6f7f9;color:#1f2328;margin:0;padding:16px}}
main{{max-width:480px;margin:10vh auto;background:#fff;border-radius:12px;padding:32px;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
h1{{font-size:1.3rem;margin:0 0 12px}}p{{line-height:1.5;color:#444}}
button{{background:#1f2328;color:#fff;border:0;border-radius:8px;padding:12px 20px;font-size:1rem;cursor:pointer}}</style>
</head><body><main><h1>{title}</h1>{body}</main></body></html>"""


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=escape(title), body=body), status_code=status_code,
                        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


def _sender_name() -> str:
    try:
        cfg = get_compliance_config()
    except ComplianceConfigurationError:
        return "us"
    return escape(cfg.business_name) if cfg and cfg.business_name else "us"


def _verified_hash(token: str) -> Optional[str]:
    try:
        cfg = get_compliance_config()
    except ComplianceConfigurationError:
        cfg = None
    if cfg is None:
        return None
    return verify_unsubscribe_token(token, cfg)


@public_router.get("/unsubscribe/{token}", response_class=HTMLResponse, include_in_schema=False)
async def unsubscribe_confirm_page(token: str) -> HTMLResponse:
    if not _verified_hash(token):
        return _page("Link not valid", "<p>This unsubscribe link is invalid or has been altered. "
                     "Please reply to the email you received and ask to be removed.</p>", 400)
    body = (
        f"<p>Click below and you won't receive any more emails from {_sender_name()}.</p>"
        f'<form method="post" action="/unsubscribe/{escape(token, quote=True)}">'
        '<button type="submit">Unsubscribe</button></form>'
    )
    return _page("Unsubscribe", body)


@public_router.post("/unsubscribe/{token}", response_class=HTMLResponse, include_in_schema=False)
async def unsubscribe(token: str, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    digest = _verified_hash(token)
    if not digest:
        return _page("Link not valid", "<p>This unsubscribe link is invalid or has been altered.</p>", 400)
    await add_suppression(db, digest=digest, reason=EmailSuppressionReason.UNSUBSCRIBED, source="unsubscribe_link")
    await db.commit()
    return _page("You're unsubscribed", f"<p>You won't receive any more emails from {_sender_name()}.</p>")


# ---------------------------------------------------------------------------
# Authenticated suppression list management (/api/* requires the bearer token)
# ---------------------------------------------------------------------------

class SuppressionCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    reason: EmailSuppressionReason = EmailSuppressionReason.MANUAL
    details: Optional[str] = Field(default=None, max_length=500)


class SuppressionOut(BaseModel):
    id: uuid.UUID
    email_masked: Optional[str]
    reason: str
    source: str
    details: Optional[str]
    created_at: datetime


class SuppressionList(BaseModel):
    items: list[SuppressionOut]
    total: int
    page: int
    page_size: int


def _out(row: EmailSuppression) -> SuppressionOut:
    return SuppressionOut(id=row.id, email_masked=row.email_masked, reason=row.reason, source=row.source,
                          details=row.details, created_at=row.created_at)


@router.get("", response_model=SuppressionList)
async def list_suppressions(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    reason: Optional[EmailSuppressionReason] = None,
    db: AsyncSession = Depends(get_db),
) -> SuppressionList:
    stmt = select(EmailSuppression)
    count_stmt = select(func.count()).select_from(EmailSuppression)
    if reason:
        stmt = stmt.where(EmailSuppression.reason == reason.value)
        count_stmt = count_stmt.where(EmailSuppression.reason == reason.value)
    rows = (await db.scalars(
        stmt.order_by(EmailSuppression.created_at.desc(), EmailSuppression.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )).all()
    total = int(await db.scalar(count_stmt) or 0)
    return SuppressionList(items=[_out(r) for r in rows], total=total, page=page, page_size=page_size)


@router.post("", response_model=SuppressionOut, status_code=status.HTTP_201_CREATED)
async def create_suppression(body: SuppressionCreate, db: AsyncSession = Depends(get_db)) -> SuppressionOut:
    """Manually suppress an address (e.g. someone replied "unsubscribe")."""
    try:
        row, _ = await add_suppression(db, email=body.email, reason=body.reason, source="manual_api", details=body.details)
    except ValueError:
        raise HTTPException(status_code=422, detail={"code": "INVALID_EMAIL", "message": "A valid email address is required"})
    await db.commit()
    return _out(row)


@router.delete("/{suppression_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_suppression(suppression_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)) -> None:
    """Remove an entry, e.g. when a contact explicitly opts back in. Managers and admins only:
    removing the wrong entry would e-mail someone who opted out."""
    role = (getattr(request.state, "authenticated_role", None) or "viewer").lower()
    if role not in {"sales_manager", "admin"}:
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "Only managers and admins can remove suppression entries"})
    row = await db.get(EmailSuppression, suppression_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "SUPPRESSION_NOT_FOUND", "message": "Suppression entry not found"})
    logger.warning("Removing email suppression %s (%s, reason=%s)", row.id, row.email_masked or "[hash]", row.reason)
    await db.delete(row)
    await db.commit()
