"""PostgreSQL storage and durable inbound webhook queue."""

from contextlib import contextmanager
import os
import logging
import psycopg


from datetime import datetime, timedelta, timezone
from psycopg.types.json import Jsonb
from psycopg.rows import dict_row
from qualification import (
    decide_next,
    text_reply,
    RETURNING_CUSTOMER_MESSAGE,
)
from whatsapp_service import send_qualification_reply

logger = logging.getLogger("hytoma")


SCHEMA = """
-- 1. Main leads table
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

-- 2. Existing lead columns
ALTER TABLE leads
ADD COLUMN IF NOT EXISTS last_return_message_at TIMESTAMPTZ;

ALTER TABLE leads
ADD COLUMN IF NOT EXISTS return_acknowledged_at TIMESTAMPTZ;

ALTER TABLE leads
ADD COLUMN IF NOT EXISTS needs_admin_followup
BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE leads
ADD COLUMN IF NOT EXISTS assigned_salesperson TEXT;

ALTER TABLE leads
ADD COLUMN IF NOT EXISTS sales_status TEXT
NOT NULL DEFAULT 'new';

ALTER TABLE leads
ADD COLUMN IF NOT EXISTS admin_notes TEXT;

ALTER TABLE leads
ADD COLUMN IF NOT EXISTS assigned_at TIMESTAMPTZ;

ALTER TABLE leads
ADD COLUMN IF NOT EXISTS bot_paused
BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE leads
ADD COLUMN IF NOT EXISTS bot_paused_at TIMESTAMPTZ;


-- 3. Salesperson directory
CREATE TABLE IF NOT EXISTS salespeople (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 4. Unique salesperson names
CREATE UNIQUE INDEX IF NOT EXISTS salespeople_name_unique_idx
ON salespeople (LOWER(name));

-- 5. Preserve existing salesperson assignments
INSERT INTO salespeople (name)
SELECT DISTINCT TRIM(assigned_salesperson)
FROM leads
WHERE assigned_salesperson IS NOT NULL
  AND TRIM(assigned_salesperson) <> ''
ON CONFLICT DO NOTHING;


-- 6. Durable inbound webhook queue
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

CREATE INDEX IF NOT EXISTS inbound_events_phone_created_idx
ON inbound_events (phone, created_at DESC);


-- 7. WhatsApp conversation history
CREATE TABLE IF NOT EXISTS chat_messages (
    id BIGSERIAL PRIMARY KEY,
    phone TEXT NOT NULL,
    whatsapp_message_id TEXT UNIQUE,
    direction TEXT NOT NULL
        CHECK (direction IN ('incoming', 'outgoing')),
    message_type TEXT NOT NULL DEFAULT 'text',
    content TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS chat_messages_phone_idx
ON chat_messages (phone, created_at DESC);


-- 8. Admin authentication sessions
CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash TEXT PRIMARY KEY,
    csrf_hash TEXT NOT NULL,
    username TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS admin_sessions_expiry_idx
ON admin_sessions (expires_at);

CREATE TABLE IF NOT EXISTS admin_login_attempts (
    username TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    locked_until TIMESTAMPTZ
);


-- Admin outbound WhatsApp request tracking
CREATE TABLE IF NOT EXISTS outbound_requests (
    request_id UUID PRIMARY KEY,
    phone TEXT NOT NULL,
    message_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'sending'
        CHECK (status IN ('sending', 'accepted', 'unknown')),
    whatsapp_message_id TEXT UNIQUE,
    error_summary TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS outbound_requests_phone_idx
ON outbound_requests (phone, created_at DESC);


-- Delivery status of outgoing WhatsApp messages
CREATE TABLE IF NOT EXISTS whatsapp_message_statuses (
    whatsapp_message_id TEXT PRIMARY KEY,
    recipient_phone TEXT,
    delivery_status TEXT NOT NULL DEFAULT 'accepted'
        CHECK (
            delivery_status IN (
                'accepted',
                'sent',
                'delivered',
                'read',
                'failed'
            )
        ),
    status_timestamp TIMESTAMPTZ,
    error_code TEXT,
    error_details TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS whatsapp_statuses_phone_idx
ON whatsapp_message_statuses (recipient_phone);


-- Durable qualification-bot outbound queue
CREATE TABLE IF NOT EXISTS bot_outbound_requests (
    inbound_message_id TEXT PRIMARY KEY
        REFERENCES inbound_events(message_id),
    phone TEXT NOT NULL,
    response_payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (
            status IN (
                'pending',
                'sending',
                'accepted',
                'unknown',
                'cancelled'
            )
        ),
    whatsapp_message_id TEXT UNIQUE,
    error_summary TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS bot_outbound_pending_idx
ON bot_outbound_requests (status, created_at);



"""


def db_connect():
    url = os.environ.get("DATABASE_URL")

    if not url:
        raise RuntimeError("DATABASE_URL must be configured")

    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10)


@contextmanager
def customer_send_guard(phone: str):
    """
    Coordinate WhatsApp sends and admin takeover
    for one customer.

    The PostgreSQL transaction holds the advisory
    lock until the guarded operation finishes.

    All competing operations must use this same guard.
    """
    with db_connect() as lock_conn:
        with lock_conn.cursor() as cur:
            cur.execute(
                """
                SELECT pg_advisory_xact_lock(
                    hashtextextended(%s, 48126)
                )
                """,
                (str(phone),),
            )

        try:
            yield
        finally:
            # The connection context releases the
            # transaction-scoped lock on exit.
            pass



def initialize_database():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA, prepare=False)


def enqueue_event(
    message_id,
    phone,
    message_text,
    sender_number_id,
    ad_id,
    display_content=None,
):
    with db_connect() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
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
            """,
                (message_id, phone, message_text, sender_number_id, ad_id),
            )

            inserted = cur.fetchone() is not None

            if not inserted:
                return False

            # Preserve message for admin inbox
            cur.execute(
                """
                INSERT INTO chat_messages
                (
                    phone,
                    whatsapp_message_id,
                    direction,
                    message_type,
                    content
                )
                VALUES (
                    %s, %s, 'incoming', 'text', %s
                )
                ON CONFLICT (whatsapp_message_id)
                DO NOTHING
            """,
                (phone, message_id, display_content or message_text),
            )

            return True


def process_next_event():
    """Process one pending WhatsApp event."""

    event_id = None

    try:
        with db_connect() as conn:
            with conn.cursor() as cur:

                # --------------------------------
                # 1. GET PENDING MESSAGE
                # --------------------------------
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

                # --------------------------------
                # 2. CHECK EXISTING CUSTOMER
                # --------------------------------
                cur.execute(
                    """
                    SELECT *
                    FROM leads
                    WHERE phone = %s
                    FOR UPDATE
                """,
                    (phone,),
                )

                lead = cur.fetchone()
                response = None

                # Admin takeover: never send bot replies
                # while the conversation is paused.
                if lead is not None and lead.get("bot_paused", False):
                    cur.execute(
                        """
                        UPDATE leads
                        SET
                            needs_admin_followup = TRUE,
                            updated_at = NOW()
                        WHERE phone = %s
                    """,
                        (phone,),
                    )

                    cur.execute(
                        """
                        UPDATE inbound_events
                        SET
                            state = 'processed',
                            processed_at = NOW(),
                            error_summary = NULL
                        WHERE message_id = %s
                    """,
                        (event_id,),
                    )

                    logger.info(
                        "Admin takeover: message stored " "without bot reply, last4=%s",
                        phone[-4:],
                    )

                    return True

                # --------------------------------
                # 3. RETURNING CUSTOMER HANDLING
                # --------------------------------
                if lead is not None and lead["status"] in {"completed", "deferred"}:

                    now = datetime.now(timezone.utc)

                    last_message = lead.get("last_return_message_at")

                    # Only acknowledge after 24 hours
                    # of inactivity, or on first return.
                    should_acknowledge = (
                        last_message is None
                        or now - last_message >= timedelta(hours=24)
                    )

                    # Flag customer for admin attention
                    cur.execute(
                        """
                        UPDATE leads
                        SET
                            last_return_message_at = NOW(),
                            needs_admin_followup = TRUE,
                            updated_at = NOW()
                        WHERE phone = %s
                    """,
                        (phone,),
                    )

                    if should_acknowledge:

                        response = text_reply(RETURNING_CUSTOMER_MESSAGE)

                        cur.execute(
                            """
                            UPDATE leads
                            SET
                                return_acknowledged_at = NOW()
                            WHERE phone = %s
                        """,
                            (phone,),
                        )

                    logger.info(
                        "Returning customer activity: " "last4=%s acknowledgement=%s",
                        phone[-4:],
                        should_acknowledge,
                    )

                # --------------------------------
                # 4. NEW CUSTOMER / SCREENING
                # --------------------------------
                else:

                    is_ad = bool(event["ad_id"])

                    ad_only = (
                        os.environ.get("QUALIFY_ONLY_AD_LEADS", "false").lower()
                        == "true"
                    )

                    # New non-ad customer:
                    # send to admin if ad-only enabled
                    if lead is None and ad_only and not is_ad:

                        cur.execute(
                            """
                            INSERT INTO leads
                            (
                                phone,
                                step,
                                status,
                                needs_admin_followup
                            )
                            VALUES (
                                %s,
                                'closed',
                                'human',
                                TRUE
                            )
                        """,
                            (phone,),
                        )

                    else:

                        updated, response = decide_next(lead, event["message_text"])

                        # Create new lead
                        if lead is None:

                            cur.execute(
                                """
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
                            """,
                                (
                                    phone,
                                    updated["step"],
                                    updated["status"],
                                    updated["project_status"],
                                    updated["completion_timeline"],
                                    updated["project_location"],
                                    updated["preferred_site_visit"],
                                    event["ad_id"],
                                ),
                            )

                        # Update existing lead
                        else:

                            cur.execute(
                                """
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
                            """,
                                (
                                    updated["step"],
                                    updated["status"],
                                    updated["project_status"],
                                    updated["completion_timeline"],
                                    updated["project_location"],
                                    updated["preferred_site_visit"],
                                    event["ad_id"],
                                    phone,
                                ),
                            )

                            # Notify admin after qualification
                            # or customer requests a human
                            if updated["status"] in {"completed", "human"}:

                                cur.execute(
                                    """
                                    UPDATE leads
                                    SET
                                        needs_admin_followup = TRUE
                                    WHERE phone = %s
                                """,
                                    (phone,),
                                )

                # --------------------------------
                # 5. QUEUE BOT RESPONSE DURABLY
                # --------------------------------
                # Do not call Meta inside this transaction.
                # One inbound event can create one bot job.

                if response is not None:

                    cur.execute(
                        """
                        INSERT INTO bot_outbound_requests (
                            inbound_message_id,
                            phone,
                            response_payload,
                            status
                        )
                        VALUES (%s, %s, %s, 'pending')
                        ON CONFLICT (inbound_message_id)
                        DO NOTHING
                        """,
                        (
                            event_id,
                            phone,
                            Jsonb(response),
                        ),
                    )

                # --------------------------------
                # 6. MARK INCOMING EVENT PROCESSED
                # --------------------------------
                # Must execute even when response is None.

                cur.execute(
                    """
                    UPDATE inbound_events
                    SET
                        state = 'processed',
                        processed_at = NOW(),
                        error_summary = NULL
                    WHERE message_id = %s
                """,
                    (event_id,),
                )

                logger.info("Processed WhatsApp message %s", event_id)

                return True

    # --------------------------------
    # 7. ERROR HANDLING AND RETRIES
    # --------------------------------
    except Exception:

        logger.exception("Processing failed for message %s", event_id)

        if event_id:
            try:
                with db_connect() as conn:
                    with conn.cursor() as cur:

                        cur.execute(
                            """
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
                        """,
                            ("ProcessingError", event_id),
                        )

            except Exception:
                logger.exception("Unable to record processing failure")

        return False


def mark_bot_outbound_unknown(inbound_message_id, reason):
    """Keep uncertain bot sends blocked from automatic retry."""
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE bot_outbound_requests
                    SET status = 'unknown',
                        error_summary = %s,
                        updated_at = NOW()
                    WHERE inbound_message_id = %s
                      AND status = 'sending'
                    """,
                    (str(reason)[:500], inbound_message_id),
                )
    except Exception:
        logger.exception("Failed to record uncertain bot send")



def process_next_bot_outbound():
    """
    Reserve one bot reply, then coordinate sending
    with the customer's admin takeover lock.
    """

    # PHASE 1: Reserve job and commit before Meta call.
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM bot_outbound_requests
                WHERE status = 'pending'
                ORDER BY created_at, inbound_message_id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """
            )

            job = cur.fetchone()

            if job is None:
                return False

            inbound_id = job["inbound_message_id"]
            phone = job["phone"]
            response = job["response_payload"]

            cur.execute(
                """
                UPDATE bot_outbound_requests
                SET status = 'sending',
                    updated_at = NOW()
                WHERE inbound_message_id = %s
                  AND status = 'pending'
                """,
                (inbound_id,),
            )

    # PHASE 2: Coordinate with admin pause/reply.
    with customer_send_guard(phone):

        # Recheck the bot state AFTER acquiring the lock.
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT bot_paused
                    FROM leads
                    WHERE phone = %s
                    """,
                    (phone,),
                )

                lead = cur.fetchone()

                if lead is None or lead["bot_paused"]:
                    cur.execute(
                        """
                        UPDATE bot_outbound_requests
                        SET status = 'cancelled',
                            updated_at = NOW()
                        WHERE inbound_message_id = %s
                          AND status = 'sending'
                        """,
                        (inbound_id,),
                    )

                    logger.info(
                        "Cancelled reserved bot reply: inbound=%s",
                        inbound_id,
                    )
                    return True

        # PHASE 3: Call Meta once while holding the lock.
        try:
            result = send_qualification_reply(
                phone,
                response,
            )

            if not isinstance(result, dict):
                raise RuntimeError(
                    "No valid WhatsApp API response"
                )

            sent_messages = result.get("messages") or []

            if not sent_messages:
                raise RuntimeError(
                    "WhatsApp response contains no message ID"
                )

            whatsapp_message_id = sent_messages[0].get("id")

            if not whatsapp_message_id:
                raise RuntimeError(
                    "Missing WhatsApp message ID"
                )

        except Exception as exc:
            logger.exception(
                "Bot send outcome uncertain: inbound=%s",
                inbound_id,
            )

            mark_bot_outbound_unknown(
                inbound_id,
                exc,
            )

            return True

        # PHASE 4: Persist the accepted result.
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
                        VALUES (
                            %s, %s, 'outgoing', %s, %s
                        )
                        ON CONFLICT (whatsapp_message_id)
                        DO NOTHING
                        """,
                        (
                            phone,
                            whatsapp_message_id,
                            response["type"],
                            response["body"],
                        ),
                    )

                    cur.execute(
                        """
                        UPDATE bot_outbound_requests
                        SET status = 'accepted',
                            whatsapp_message_id = %s,
                            error_summary = NULL,
                            updated_at = NOW()
                        WHERE inbound_message_id = %s
                          AND status = 'sending'
                        """,
                        (
                            whatsapp_message_id,
                            inbound_id,
                        ),
                    )

            logger.info(
                "Bot reply accepted: inbound=%s",
                inbound_id,
            )

        except Exception as exc:
            logger.exception(
                "Meta accepted bot reply but saving failed"
            )

            mark_bot_outbound_unknown(
                inbound_id,
                exc,
            )

    return True
