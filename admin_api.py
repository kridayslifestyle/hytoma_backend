
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

from datetime import datetime, timedelta, timezone
import re
import requests
from auth import require_admin
from whatsapp_service import send_whatsapp_text


import logging

logger = logging.getLogger("hytoma")


router = APIRouter(
    prefix="/admin",
    tags=["HYTOMA Admin"]
)




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


# ---------------------------------------
# ADMIN WHATSAPP REPLY AND BOT CONTROLS
# ---------------------------------------

class AdminReplyRequest(BaseModel):
    message: str = Field(
        min_length=1,
        max_length=4096
    )


class BotControlRequest(BaseModel):
    paused: bool


def validate_phone(phone: str) -> str:
    """Accept WhatsApp customer numbers in digit format."""

    if not re.fullmatch(r"[1-9][0-9]{6,14}", phone):
        raise HTTPException(
            status_code=422,
            detail="Invalid customer phone number"
        )

    return phone


def latest_customer_message(cur, phone: str):
    """Get the latest received customer-message timestamp."""

    cur.execute("""
        SELECT MAX(created_at) AS last_message_at
        FROM inbound_events
        WHERE phone = %s
    """, (phone,))

    result = cur.fetchone()
    return result["last_message_at"]


def get_window_details(last_message_at):
    """Check the 24-hour customer-service window."""

    now = datetime.now(timezone.utc)

    if last_message_at is None:
        return {
            "is_open": False,
            "last_customer_message_at": None,
            "expires_at": None
        }

    expires_at = last_message_at + timedelta(hours=24)

    # Small safety margin to avoid sending at expiry.
    is_open = now < expires_at - timedelta(seconds=60)

    return {
        "is_open": is_open,
        "last_customer_message_at": last_message_at,
        "expires_at": expires_at
    }


# Check the WhatsApp messaging window
@router.get(
    "/leads/{phone}/reply-window",
    dependencies=[Depends(require_admin)]
)
def get_reply_window(phone: str):

    phone = validate_phone(phone)

    with db_connect() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT phone
                FROM leads
                WHERE phone = %s
            """, (phone,))

            if not cur.fetchone():
                raise HTTPException(
                    status_code=404,
                    detail="Lead not found"
                )

            last_message_at = latest_customer_message(
                cur, phone
            )

            return get_window_details(last_message_at)


# Pause or resume automatic qualification
@router.patch(
    "/leads/{phone}/bot",
    dependencies=[Depends(require_admin)]
)
def control_bot(
    phone: str,
    payload: BotControlRequest
):

    phone = validate_phone(phone)

    with db_connect() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                UPDATE leads
                SET
                    bot_paused = %s,
                    bot_paused_at =
                        CASE
                            WHEN %s THEN NOW()
                            ELSE NULL
                        END,
                    updated_at = NOW()
                WHERE phone = %s
                RETURNING
                    phone,
                    status,
                    step,
                    bot_paused,
                    bot_paused_at
            """, (
                payload.paused,
                payload.paused,
                phone
            ))

            result = cur.fetchone()

            if result is None:
                raise HTTPException(
                    status_code=404,
                    detail="Lead not found"
                )

            return result


# Admin replies from HYTOMA WhatsApp number
@router.post(
    "/leads/{phone}/reply",
    dependencies=[Depends(require_admin)]
)
def admin_send_reply(
    phone: str,
    payload: AdminReplyRequest
):

    phone = validate_phone(phone)
    message_text = payload.message.strip()

    if not message_text:
        raise HTTPException(
            status_code=422,
            detail="Message cannot be empty"
        )

    with db_connect() as conn:
        with conn.cursor() as cur:

            # Lock this customer's lead while
            # checking permissions and sending.
            cur.execute("""
                SELECT phone, status, bot_paused
                FROM leads
                WHERE phone = %s
                FOR UPDATE
            """, (phone,))

            lead = cur.fetchone()

            if lead is None:
                raise HTTPException(
                    status_code=404,
                    detail="Lead not found"
                )

            # Prevent replies to opted-out contacts.
            if lead["status"] == "opted_out":
                raise HTTPException(
                    status_code=403,
                    detail="Customer has opted out"
                )

            # Check the messaging window again
            # server-side, not just in the UI.
            last_message_at = latest_customer_message(
                cur, phone
            )

            window = get_window_details(last_message_at)

            if not window["is_open"]:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "24-hour customer-service window "
                        "is closed. An approved WhatsApp "
                        "message template is required."
                    )
                )

            # Send using existing Meta Cloud API.
            try:
                result = send_whatsapp_text(
                    phone,
                    message_text
                )

            except requests.RequestException:
                logger.exception(
                    "Admin WhatsApp send failed for last4=%s",
                    phone[-4:]
                )

                raise HTTPException(
                    status_code=502,
                    detail=(
                        "WhatsApp provider request failed. "
                        "Check delivery before retrying."
                    )
                )

            
            # Validate Meta's API response
            if not isinstance(result, dict):
                logger.error(
                    "WhatsApp send returned no API response; "
                    "delivery may already have succeeded."
                )

                raise HTTPException(
                    status_code=502,
                    detail=(
                        "WhatsApp message may have been sent, "
                        "but no response was returned. "
                        "Check WhatsApp before retrying."
                    )
                )

            messages = result.get("messages") or []


            message_id = (
                messages[0].get("id")
                if messages else None
            )

            if not message_id:
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "Meta did not return a message ID. "
                        "Check delivery before retrying."
                    )
                )

            # Save the outgoing admin message.
            cur.execute("""
                INSERT INTO chat_messages
                (
                    phone,
                    whatsapp_message_id,
                    direction,
                    message_type,
                    content
                )
                VALUES (
                    %s, %s, 'outgoing',
                    'text', %s
                )
                ON CONFLICT (whatsapp_message_id)
                DO NOTHING
            """, (
                phone,
                message_id,
                message_text
            ))

            # Manual reply automatically pauses bot.
            cur.execute("""
                UPDATE leads
                SET
                    bot_paused = TRUE,
                    bot_paused_at = NOW(),
                    updated_at = NOW()
                WHERE phone = %s
            """, (phone,))

            logger.info(
                "Admin sent WhatsApp reply, last4=%s",
                phone[-4:]
            )

            return {
                "status": "accepted",
                "phone": phone,
                "whatsapp_message_id": message_id,
                "bot_paused": True,
                "message": message_text
            }

