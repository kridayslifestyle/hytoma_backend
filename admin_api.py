
"""HYTOMA admin lead management API."""

import os
import secrets

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query
)
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from db import db_connect


router = APIRouter(
    prefix="/admin",
    tags=["HYTOMA Admin"]
)

security = HTTPBearer()


def require_admin(
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    expected_key = os.getenv("ADMIN_API_KEY", "")

    if (
        not expected_key
        or not secrets.compare_digest(
            credentials.credentials,
            expected_key
        )
    ):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized"
        )

    return True


class AssignmentUpdate(BaseModel):
    salesperson: str = Field(
        min_length=1,
        max_length=100
    )

    sales_status: str = "assigned"


class NotesUpdate(BaseModel):
    notes: str = Field(max_length=2000)


ALLOWED_SALES_STATUSES = {
    "new",
    "assigned",
    "contacted",
    "visit_scheduled",
    "closed",
    "not_interested"
}


# Dashboard counts
@router.get(
    "/stats",
    dependencies=[Depends(require_admin)]
)
def get_stats():

    with db_connect() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    COUNT(*) AS total_leads,

                    COUNT(*) FILTER (
                        WHERE status = 'completed'
                    ) AS completed_leads,

                    COUNT(*) FILTER (
                        WHERE status = 'deferred'
                    ) AS deferred_leads,

                    COUNT(*) FILTER (
                        WHERE needs_admin_followup = TRUE
                    ) AS pending_followups,

                    COUNT(*) FILTER (
                        WHERE assigned_salesperson IS NOT NULL
                    ) AS assigned_leads
                FROM leads
            """)

            return cur.fetchone()


# List leads
@router.get(
    "/leads",
    dependencies=[Depends(require_admin)]
)
def get_leads(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0)
):

    with db_connect() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    phone,
                    status,
                    step,
                    project_status,
                    completion_timeline,
                    project_location,
                    preferred_site_visit,
                    assigned_salesperson,
                    sales_status,
                    needs_admin_followup,
                    updated_at,
                    created_at
                FROM leads
                ORDER BY updated_at DESC
                LIMIT %s OFFSET %s
            """, (limit, offset))

            return {
                "leads": cur.fetchall()
            }


# Individual lead details
@router.get(
    "/leads/{phone}",
    dependencies=[Depends(require_admin)]
)
def get_lead(phone: str):

    with db_connect() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT *
                FROM leads
                WHERE phone = %s
            """, (phone,))

            lead = cur.fetchone()

            if not lead:
                raise HTTPException(
                    status_code=404,
                    detail="Lead not found"
                )

            return lead


# Chat history
@router.get(
    "/leads/{phone}/messages",
    dependencies=[Depends(require_admin)]
)
def get_messages(
    phone: str,
    limit: int = Query(default=100, ge=1, le=200)
):

    with db_connect() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    id,
                    direction,
                    message_type,
                    content,
                    created_at
                FROM (
                    SELECT *
                    FROM chat_messages
                    WHERE phone = %s
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s
                ) AS recent
                ORDER BY created_at ASC, id ASC
            """, (phone, limit))

            return {
                "phone": phone,
                "messages": cur.fetchall()
            }


# Assign lead to salesperson
@router.patch(
    "/leads/{phone}/assign",
    dependencies=[Depends(require_admin)]
)
def assign_lead(
    phone: str,
    payload: AssignmentUpdate
):

    if payload.sales_status not in ALLOWED_SALES_STATUSES:
        raise HTTPException(
            status_code=422,
            detail="Invalid sales status"
        )

    with db_connect() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                UPDATE leads
                SET
                    assigned_salesperson = %s,
                    sales_status = %s,
                    assigned_at = NOW(),
                    updated_at = NOW()
                WHERE phone = %s
                RETURNING
                    phone,
                    assigned_salesperson,
                    sales_status,
                    assigned_at
            """, (
                payload.salesperson,
                payload.sales_status,
                phone
            ))

            result = cur.fetchone()

            if not result:
                raise HTTPException(
                    status_code=404,
                    detail="Lead not found"
                )

            return result


# Update admin notes
@router.patch(
    "/leads/{phone}/notes",
    dependencies=[Depends(require_admin)]
)
def update_notes(
    phone: str,
    payload: NotesUpdate
):

    with db_connect() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                UPDATE leads
                SET
                    admin_notes = %s,
                    updated_at = NOW()
                WHERE phone = %s
                RETURNING phone, admin_notes
            """, (payload.notes, phone))

            result = cur.fetchone()

            if not result:
                raise HTTPException(
                    status_code=404,
                    detail="Lead not found"
                )

            return result


# Mark customer enquiry as reviewed
@router.patch(
    "/leads/{phone}/mark-reviewed",
    dependencies=[Depends(require_admin)]
)
def mark_reviewed(phone: str):

    with db_connect() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                UPDATE leads
                SET
                    needs_admin_followup = FALSE,
                    updated_at = NOW()
                WHERE phone = %s
                RETURNING
                    phone,
                    needs_admin_followup
            """, (phone,))

            result = cur.fetchone()

            if not result:
                raise HTTPException(
                    status_code=404,
                    detail="Lead not found"
                )

            return result
