
"""PostgreSQL storage and durable inbound webhook queue."""

import os
import logging
import psycopg

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

                cur.execute("""
                    SELECT *
                    FROM leads
                    WHERE phone = %s
                    FOR UPDATE
                """, (phone,))

                lead = cur.fetchone()

                is_ad = bool(event["ad_id"])

                ad_only = (
                    os.environ.get(
                        "QUALIFY_ONLY_AD_LEADS", "false"
                    ).lower() == "true"
                )

                if lead is None and ad_only and not is_ad:

                    # New non-ad contact:
                    # do not start lead screening.
                    cur.execute("""
                        INSERT INTO leads
                        (phone, step, status)
                        VALUES (%s, 'closed', 'human')
                    """, (phone,))

                    response = None

                else:

                    updated, response = decide_next(
                        lead,
                        event["message_text"]
                    )

                    if lead is None:
                        cur.execute("""
                            INSERT INTO leads
                            (
                                phone,
                                step,
                                status,
                                project_status,
                                completion_timeline,
                                project_location,
                                preferred_site_visit,
                                ad_id
                            )
                            VALUES (
                                %s, %s, %s, %s,
                                %s, %s, %s, %s
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
                                ad_id = COALESCE(ad_id, %s),
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

                if response:
                    send_qualification_reply(
                        phone,
                        response
                    )

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
                    "Unable to record webhook processing failure"
                )

        return False
