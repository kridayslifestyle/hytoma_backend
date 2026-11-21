"""Idempotent HYTOMA admin WhatsApp replies."""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from psycopg.rows import dict_row

from auth import require_admin
from db import db_connect, customer_send_guard
from whatsapp_service import send_whatsapp_text

logger = logging.getLogger("hytoma")

router = APIRouter(
    prefix="/admin",
    tags=["Safe WhatsApp Replies"],
    dependencies=[Depends(require_admin)],
)


class SafeReplyRequest(BaseModel):
    request_id: UUID
    message: str = Field(min_length=1, max_length=4096)


def mark_unknown(
    request_id,
    error_message,
    whatsapp_message_id=None,
):
    """
    Preserve an uncertain result.

    If Meta returned a message ID, retain it for
    reconciliation without sending another message.
    """
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE outbound_requests
                    SET
                        status = 'unknown',
                        whatsapp_message_id = COALESCE(
                            whatsapp_message_id,
                            %s
                        ),
                        error_summary = %s,
                        updated_at = NOW()
                    WHERE request_id = %s
                      AND status = 'sending'
                    """,
                    (
                        whatsapp_message_id,
                        str(error_message)[:500],
                        request_id,
                    ),
                )

    except Exception:
        logger.exception("Unable to record uncertain outbound request")


@router.post("/leads/{phone}/reply-safe")
def send_safe_reply(phone: str, payload: SafeReplyRequest):
    phone = phone.strip()
    message = payload.message.strip()
    request_id = payload.request_id

    if not phone.isdigit() or not (8 <= len(phone) <= 15):
        raise HTTPException(status_code=422, detail="Invalid WhatsApp phone number")

    if not message:
        raise HTTPException(status_code=422, detail="Message cannot be empty")

    if len(message) > 4096:
        raise HTTPException(status_code=422, detail="Message is too long")

    # PHASE 1: Reserve the request in PostgreSQL.
    # This transaction must commit before the Meta API call.
    with db_connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                INSERT INTO outbound_requests (
                    request_id, phone, message_text, status
                )
                VALUES (%s, %s, %s, 'sending')
                ON CONFLICT (request_id) DO NOTHING
                RETURNING request_id
                """,
                (request_id, phone, message),
            )

            inserted = cur.fetchone()

            if inserted is None:
                cur.execute(
                    """
                    SELECT phone, message_text, status,
                           whatsapp_message_id
                    FROM outbound_requests
                    WHERE request_id = %s
                    """,
                    (request_id,),
                )

                existing = cur.fetchone()

                if not existing:
                    raise HTTPException(
                        status_code=409, detail="Request is being processed"
                    )

                if existing["phone"] != phone or existing["message_text"] != message:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Request ID was already used " "for a different message"
                        ),
                    )

                if existing["status"] == "accepted":
                    return {
                        "status": "accepted",
                        "phone": phone,
                        "request_id": str(request_id),
                        "whatsapp_message_id": existing["whatsapp_message_id"],
                        "already_processed": True,
                    }

                # Never resend a potentially accepted request.
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "This request was already submitted, "
                        "but its delivery outcome is uncertain. "
                        "Check chat history and WhatsApp."
                    ),
                )

            # Validate the customer and the service window.
            cur.execute(
                """
                SELECT status
                FROM leads
                WHERE phone = %s
                """,
                (phone,),
            )

            lead = cur.fetchone()

            if not lead:
                raise HTTPException(status_code=404, detail="Customer lead not found")

            if lead["status"] == "opted_out":
                raise HTTPException(status_code=403, detail="Customer has opted out")

            # Match the existing admin window rule:
            # latest inbound within 24 hours, with a
            # 60-second safety margin.
            cur.execute(
                """
                SELECT created_at
                FROM inbound_events
                WHERE phone = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (phone,),
            )

            latest = cur.fetchone()

            cur.execute(
                """
                SELECT (
                    %s::timestamptz >
                    NOW() - INTERVAL '23 hours 59 minutes'
                ) AS is_open
                """,
                (latest["created_at"] if latest else None,),
            )

            check = cur.fetchone()

            if check["is_open"] is not True:
                raise HTTPException(
                    status_code=403,
                    detail=("24-hour customer-service " "reply window is closed"),
                )

    # The reservation is now committed.
    # A duplicate request ID cannot cause a second send.

    
    with customer_send_guard(phone):
        try:
            return _complete_reserved_safe_reply(
                phone,
                message,
                request_id,
            )
        except HTTPException as exc:
            if exc.status_code in (403, 404):
                mark_unknown(
                    request_id,
                    "Pre-send permission/window check rejected request",
                )
            raise

    # PHASE 2 and 3: Coordinate with bot/admin operations.
    # The original request reservation is already committed.
    # with customer_send_guard(phone):
    #     return _complete_reserved_safe_reply(
    #         phone,
    #         message,
    #         request_id,
    #     )

def _complete_reserved_safe_reply(phone, message, request_id):
    """
    Complete a reserved admin send while the caller
    holds the customer's advisory lock.
    """

    # Recheck permission and customer-service window
    # after waiting for the customer's lock.
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status
                FROM leads
                WHERE phone = %s
                """,
                (phone,),
            )

            lead = cur.fetchone()

            if lead is None:
                raise HTTPException(
                    status_code=404,
                    detail="Lead not found",
                )

            if lead["status"] == "opted_out":
                raise HTTPException(
                    status_code=403,
                    detail="Customer has opted out",
                )

            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM inbound_events
                    WHERE phone = %s
                      AND created_at >
                          NOW() - INTERVAL '23 hours 59 minutes'
                ) AS is_open
                """,
                (phone,),
            )

            window = cur.fetchone()

            if not window["is_open"]:
                raise HTTPException(
                    status_code=403,
                    detail="24-hour reply window is closed",
                )

    # Since this request was already reserved as
    # sending, rejected pre-send checks must not
    # leave it stuck in that state.
    #
    # For now, mark these as unknown for review.
    # We will add an explicit rejected status later
    # rather than changing the existing DB constraint.

    try:
        result = send_whatsapp_text(phone, message)

        if not isinstance(result, dict):
            raise RuntimeError(
                "WhatsApp API returned no valid response"
            )

        sent_messages = result.get("messages") or []

        if not sent_messages:
            raise RuntimeError(
                "WhatsApp response has no messages"
            )

        whatsapp_message_id = sent_messages[0].get("id")

        if not whatsapp_message_id:
            raise RuntimeError(
                "WhatsApp response has no message ID"
            )

    except Exception as exc:
        logger.exception(
            "Admin WhatsApp send outcome uncertain"
        )

        mark_unknown(request_id, exc)

        raise HTTPException(
            status_code=502,
            detail=(
                "WhatsApp send outcome is uncertain. "
                "Do not retry with another request ID."
            ),
        ) from exc

    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO chat_messages (
                        phone,
                        whatsapp_message_id,
                        direction,
                        message_type,
                        content
                    )
                    VALUES (%s, %s, 'outgoing', 'text', %s)
                    ON CONFLICT (whatsapp_message_id)
                    DO NOTHING
                    """,
                    (phone, whatsapp_message_id, message),
                )

                cur.execute(
                    """
                    UPDATE leads
                    SET bot_paused = TRUE,
                        bot_paused_at = NOW(),
                        updated_at = NOW()
                    WHERE phone = %s
                    """,
                    (phone,),
                )

                cur.execute(
                    """
                    UPDATE bot_outbound_requests
                    SET status = 'cancelled',
                        updated_at = NOW()
                    WHERE phone = %s
                      AND status = 'pending'
                    """,
                    (phone,),
                )

                cur.execute(
                    """
                    UPDATE outbound_requests
                    SET status = 'accepted',
                        whatsapp_message_id = %s,
                        error_summary = NULL,
                        updated_at = NOW()
                    WHERE request_id = %s
                      AND status = 'sending'
                    """,
                    (whatsapp_message_id, request_id),
                )

    except Exception as exc:
        logger.exception(
            "Meta accepted admin reply but database save failed"
        )

        mark_unknown(
            request_id,
            exc,
            whatsapp_message_id=whatsapp_message_id,
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "Meta accepted the message, but database "
                "confirmation failed. Do not resend."
            ),
        ) from exc

    return {
        "status": "accepted",
        "phone": phone,
        "request_id": str(request_id),
        "whatsapp_message_id": whatsapp_message_id,
        "already_processed": False,
        "bot_paused": True,
    }



@router.get("/outbound-requests/{request_id}")
def get_outbound_request_status(request_id: UUID):
    """
    Read an outbound request without retrying it.
    This endpoint never sends WhatsApp messages.
    """

    with db_connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT
                    request_id,
                    phone,
                    status,
                    whatsapp_message_id,
                    created_at,
                    updated_at
                FROM outbound_requests
                WHERE request_id = %s
                """,
                (request_id,),
            )

            request_record = cur.fetchone()

            if request_record is None:
                raise HTTPException(
                    status_code=404,
                    detail="Outbound request not found",
                )

            message_id = request_record["whatsapp_message_id"]

            delivery_status = None
            saved_in_chat = False

            if message_id:
                cur.execute(
                    """
                    SELECT delivery_status
                    FROM whatsapp_message_statuses
                    WHERE whatsapp_message_id = %s
                    """,
                    (message_id,),
                )

                delivery = cur.fetchone()

                if delivery:
                    delivery_status = delivery["delivery_status"]

                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM chat_messages
                        WHERE whatsapp_message_id = %s
                          AND direction = 'outgoing'
                    ) AS exists_in_chat
                    """,
                    (message_id,),
                )

                saved_in_chat = cur.fetchone()["exists_in_chat"]

    return {
        "request_id": str(request_record["request_id"]),
        "phone": request_record["phone"],
        "status": request_record["status"],
        "whatsapp_message_id": message_id,
        "delivery_status": delivery_status,
        "saved_in_chat": saved_in_chat,
        "created_at": request_record["created_at"],
        "updated_at": request_record["updated_at"],
    }
