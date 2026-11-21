
"""Store Meta WhatsApp delivery-status webhooks."""

import logging
import os
from datetime import datetime, timezone

from db import db_connect

logger = logging.getLogger("hytoma")

VALID_STATUSES = {
    "sent",
    "delivered",
    "read",
    "failed",
}

# Prevent delayed webhook events from downgrading
# a status already confirmed by Meta.
STATUS_RANK = {
    "accepted": 0,
    "sent": 1,
    "failed": 2,
    "delivered": 3,
    "read": 4,
}


def extract_statuses(payload):
    """Extract status records from every webhook change."""
    if not isinstance(payload, dict):
        return []

    if payload.get("object") != "whatsapp_business_account":
        return []

    results = []

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "messages":
                continue

            value = change.get("value") or {}

            for status in value.get("statuses", []):
                results.append(status)

    return results


def parse_timestamp(value):
    if value is None:
        return None

    try:
        return datetime.fromtimestamp(
            int(value),
            tz=timezone.utc,
        )
    except (TypeError, ValueError, OverflowError):
        return None


def record_delivery_statuses(payload):
    """Persist statuses without changing lead qualification."""

    statuses = extract_statuses(payload)

    if not statuses:
        return 0

    recorded = 0

    with db_connect() as conn:
        with conn.cursor() as cur:
            for event in statuses:
                message_id = event.get("id")
                status = event.get("status")

                if not message_id or status not in VALID_STATUSES:
                    continue

                recipient = event.get("recipient_id")
                event_time = parse_timestamp(
                    event.get("timestamp")
                )

                error_code = None
                error_details = None

                if status == "failed":
                    errors = event.get("errors") or []

                    if errors:
                        first_error = errors[0]

                        if first_error.get("code") is not None:
                            error_code = str(
                                first_error["code"]
                            )

                        error_details = (
                            (first_error.get("error_data") or {})
                            .get("details")
                            or first_error.get("message")
                            or first_error.get("title")
                        )

                        if error_details:
                            error_details = str(
                                error_details
                            )[:1000]

                # Upsert directly by WhatsApp message ID.
                # This works even if the outgoing chat row
                # has not yet been committed.
                #
                # Only promote status according to rank.
                # Duplicate or delayed events won't
                # downgrade delivered/read to sent.
                cur.execute(
                    """
                    INSERT INTO whatsapp_message_statuses (
                        whatsapp_message_id,
                        recipient_phone,
                        delivery_status,
                        status_timestamp,
                        error_code,
                        error_details
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)

                    ON CONFLICT (whatsapp_message_id)
                    DO UPDATE SET
                        recipient_phone = COALESCE(
                            EXCLUDED.recipient_phone,
                            whatsapp_message_statuses.recipient_phone
                        ),
                        delivery_status =
                            CASE
                                WHEN (
                                    CASE EXCLUDED.delivery_status
                                        WHEN 'accepted' THEN 0
                                        WHEN 'sent' THEN 1
                                        WHEN 'failed' THEN 2
                                        WHEN 'delivered' THEN 3
                                        WHEN 'read' THEN 4
                                    END
                                ) > (
                                    CASE whatsapp_message_statuses.delivery_status
                                        WHEN 'accepted' THEN 0
                                        WHEN 'sent' THEN 1
                                        WHEN 'failed' THEN 2
                                        WHEN 'delivered' THEN 3
                                        WHEN 'read' THEN 4
                                    END
                                )
                                THEN EXCLUDED.delivery_status
                                ELSE whatsapp_message_statuses.delivery_status
                            END,
                        status_timestamp =
                            CASE
                                WHEN (
                                    CASE EXCLUDED.delivery_status
                                        WHEN 'accepted' THEN 0
                                        WHEN 'sent' THEN 1
                                        WHEN 'failed' THEN 2
                                        WHEN 'delivered' THEN 3
                                        WHEN 'read' THEN 4
                                    END
                                ) > (
                                    CASE whatsapp_message_statuses.delivery_status
                                        WHEN 'accepted' THEN 0
                                        WHEN 'sent' THEN 1
                                        WHEN 'failed' THEN 2
                                        WHEN 'delivered' THEN 3
                                        WHEN 'read' THEN 4
                                    END
                                )
                                THEN EXCLUDED.status_timestamp
                                ELSE whatsapp_message_statuses.status_timestamp
                            END,
                        error_code =
                            CASE
                                WHEN EXCLUDED.delivery_status = 'failed'
                                 AND whatsapp_message_statuses.delivery_status
                                     IN ('accepted', 'sent', 'failed')
                                THEN EXCLUDED.error_code
                                ELSE whatsapp_message_statuses.error_code
                            END,
                        error_details =
                            CASE
                                WHEN EXCLUDED.delivery_status = 'failed'
                                 AND whatsapp_message_statuses.delivery_status
                                     IN ('accepted', 'sent', 'failed')
                                THEN EXCLUDED.error_details
                                ELSE whatsapp_message_statuses.error_details
                            END,
                        updated_at = NOW()
                    """,
                    (
                        message_id,
                        recipient,
                        status,
                        event_time,
                        error_code,
                        error_details,
                    ),
                )

                recorded += 1

    logger.info(
        "Recorded %s WhatsApp delivery status events",
        recorded,
    )

    return recorded
