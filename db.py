
"""PostgreSQL storage and durable inbound webhook queue."""

import os
import logging
import psycopg


from datetime import datetime, timedelta, timezone

from qualification import (
    decide_next,
    text_reply,
    RETURNING_CUSTOMER_MESSAGE
)

from whatsapp_service import send_qualification_reply



from psycopg.rows import dict_row
from qualification import decide_next
from whatsapp_service import send_qualification_reply
logger = logging.getLogger("hytoma")

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    phone TEXT PRIMARY KEY,
    step TEXT NOT NULL DEFAULT 'status',
    status TEXT NOT NULL DEFAULT 'screening',
    project_status TEXT,
    completion_timeline TEXT,
    project_location TEXT,
    preferred_site_visit TEXT,
    ad_id TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS inbound_events (
    message_id TEXT PRIMARY KEY,
    phone TEXT NOT NULL,
    message_text TEXT NOT NULL,
    sender_number_id TEXT,
    ad_id TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_after TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    error_summary TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS inbound_pending_idx
ON inbound_events (state, retry_after, created_at);


ALTER TABLE leads
ADD COLUMN IF NOT EXISTS last_return_message_at
TIMESTAMPTZ;

ALTER TABLE leads
ADD COLUMN IF NOT EXISTS return_acknowledged_at
TIMESTAMPTZ;

ALTER TABLE leads
ADD COLUMN IF NOT EXISTS needs_admin_followup
BOOLEAN NOT NULL DEFAULT FALSE;

"""


def db_connect():
    url = os.environ.get("DATABASE_URL")

    if not url:
        raise RuntimeError(
            "DATABASE_URL must be configured"
        )

    return psycopg.connect(
        url,
        row_factory=dict_row,
        connect_timeout=10
    )


def initialize_database():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA, prepare=False)


def enqueue_event(
    message_id,
    phone,
    message_text,
    sender_number_id,
    ad_id
):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO inbound_events
                (
                    message_id,
                    phone,
                    message_text,
                    sender_number_id,
                    ad_id
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (message_id) DO NOTHING
                RETURNING message_id
            """, (
                message_id,
                phone,
                message_text,
                sender_number_id,
                ad_id
            ))

            return cur.fetchone() is not None



def process_next_event():
    """Process one pending WhatsApp event."""

    event_id = None

    try:
        with db_connect() as conn:
            with conn.cursor() as cur:

                # Get one incoming message
                cur.execute("""
                    SELECT *
                    FROM inbound_events
                    WHERE state = 'pending'
                    AND retry_after <= NOW()
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                """)

                event = cur.fetchone()

                if not event:
                    return False

                event_id = event["message_id"]
                phone = event["phone"]

                # Check whether customer already exists
                cur.execute("""
                    SELECT *
                    FROM leads
                    WHERE phone = %s
                    FOR UPDATE
                """, (phone,))

                lead = cur.fetchone()
                response = None

                # --------------------------------
                # A. RETURNING CUSTOMER
                # --------------------------------
                if (
                    lead is not None
                    and lead["status"] in {
                        "completed",
                        "deferred"
                    }
                ):

                    now = datetime.now(timezone.utc)

                    last_message = lead.get(
                        "last_return_message_at"
                    )

                    # New session after 24 hours
                    # without returning-customer activity.
                    should_acknowledge = (
                        last_message is None
                        or now - last_message
                        >= timedelta(hours=24)
                    )

                    # Flag for admin review on EVERY
                    # returning-customer message.
                    cur.execute("""
                        UPDATE leads
                        SET
                            last_return_message_at = NOW(),
                            needs_admin_followup = TRUE,
                            updated_at = NOW()
                        WHERE phone = %s
                    """, (phone,))

                    if should_acknowledge:

                        response = text_reply(
                            RETURNING_CUSTOMER_MESSAGE
                        )

                        cur.execute("""
                            UPDATE leads
                            SET
                                return_acknowledged_at = NOW()
                            WHERE phone = %s
                        """, (phone,))

                    logger.info(
                        "Returning customer activity: "
                        "last4=%s acknowledgement=%s",
                        phone[-4:],
                        should_acknowledge
                    )

                # --------------------------------
                # B. NEW CUSTOMER / SCREENING
                # --------------------------------
                else:

                    is_ad = bool(event["ad_id"])

                    ad_only = (
                        os.environ.get(
                            "QUALIFY_ONLY_AD_LEADS",
                            "false"
                        ).lower() == "true"
                    )

                    # New contact not from an ad
                    # when ad-only screening enabled
                    if (
                        lead is None
                        and ad_only
                        and not is_ad
                    ):

                        cur.execute("""
                            INSERT INTO leads
                            (phone, step, status,
                             needs_admin_followup)
                            VALUES (
                                %s,
                                'closed',
                                'human',
                                TRUE
                            )
                        """, (phone,))

                    else:

                        updated, response = decide_next(
                            lead,
                            event["message_text"]
                        )

                        if lead is None:

                            cur.execute("""
                                INSERT INTO leads (
                                    phone,
                                    step,
                                    status,
                                    project_status,
                                    completion_timeline,
                                    project_location,
                                    preferred_site_visit,
                                    ad_id,
                                    needs_admin_followup
                                )
                                VALUES (
                                    %s, %s, %s, %s, %s,
                                    %s, %s, %s, FALSE
                                )
                            """, (
                                phone,
                                updated["step"],
                                updated["status"],
                                updated["project_status"],
                                updated["completion_timeline"],
                                updated["project_location"],
                                updated["preferred_site_visit"],
                                event["ad_id"]
                            ))

                        else:

                            cur.execute("""
                                UPDATE leads
                                SET
                                    step = %s,
                                    status = %s,
                                    project_status = %s,
                                    completion_timeline = %s,
                                    project_location = %s,
                                    preferred_site_visit = %s,
                                    ad_id = COALESCE(
                                        ad_id, %s
                                    ),
                                    updated_at = NOW()
                                WHERE phone = %s
                            """, (
                                updated["step"],
                                updated["status"],
                                updated["project_status"],
                                updated["completion_timeline"],
                                updated["project_location"],
                                updated["preferred_site_visit"],
                                event["ad_id"],
                                phone
                            ))

                            # Completed/human enquiries
                            # can be reviewed by admin.
                            if updated["status"] in {
                                "completed",
                                "human"
                            }:
                                cur.execute("""
                                    UPDATE leads
                                    SET needs_admin_followup = TRUE
                                    WHERE phone = %s
                                """, (phone,))

                # --------------------------------
                # C. SEND RESPONSE IF REQUIRED
                # --------------------------------
                if response:
                    send_qualification_reply(
                        phone,
                        response
                    )

                # --------------------------------
                # D. MARK EVENT AS PROCESSED
                # --------------------------------
                cur.execute("""
                    UPDATE inbound_events
                    SET
                        state = 'processed',
                        processed_at = NOW(),
                        error_summary = NULL
                    WHERE message_id = %s
                """, (event_id,))

                logger.info(
                    "Processed WhatsApp message %s",
                    event_id
                )

                return True

    except Exception as exc:

        logger.error(
            "Processing failed for message %s: %s",
            event_id,
            type(exc).__name__
        )

        if event_id:
            try:
                with db_connect() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE inbound_events
                            SET
                                attempts = attempts + 1,
                                state = CASE
                                    WHEN attempts + 1 >= 5
                                    THEN 'failed'
                                    ELSE 'pending'
                                END,
                                retry_after =
                                    NOW() +
                                    INTERVAL '1 minute' *
                                    LEAST(
                                        30,
                                        POWER(2, attempts)
                                    ),
                                error_summary = %s
                            WHERE message_id = %s
                        """, (
                            type(exc).__name__,
                            event_id
                        ))

            except Exception:
                logger.exception(
                    "Unable to record processing failure"
                )

        return False
