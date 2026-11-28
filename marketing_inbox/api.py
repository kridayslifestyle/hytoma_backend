from fastapi import APIRouter, Depends, HTTPException, Query

from auth import require_admin
from db import db_connect


router = APIRouter(
    prefix="/admin/marketing-inbox",
    tags=["Marketing Inbox"],
    dependencies=[Depends(require_admin)]
)


def initialize_marketing_inbox():

    with db_connect() as conn, conn.cursor() as cur:

        cur.execute("""
            CREATE TABLE IF NOT EXISTS marketing_replies (

                message_id TEXT PRIMARY KEY
                    REFERENCES inbound_events(message_id),

                campaign_id BIGINT NOT NULL,
                contact_id BIGINT NOT NULL,
                display_text TEXT NOT NULL,
                read_at TIMESTAMPTZ,

                FOREIGN KEY (campaign_id, contact_id)
                    REFERENCES campaign_recipients(
                        campaign_id, contact_id
                    )
            );

            CREATE INDEX IF NOT EXISTS
                marketing_replies_contact_idx
            ON marketing_replies(contact_id);

        """, prepare=False)


def route_campaign_reply(
    cur,
    message_id,
    phone,
    context_id,
    ad_id,
    display_text
):

    if ad_id:
        return False

    if context_id:

        cur.execute("""
            SELECT r.campaign_id, r.contact_id

            FROM campaign_recipients r
            JOIN contacts c ON c.id = r.contact_id

            WHERE r.wamid = %s
              AND c.phone = %s
              AND r.send_status = 'accepted'

            UNION ALL

            SELECT m.campaign_id, m.contact_id

            FROM marketing_manual_messages m
            JOIN contacts c ON c.id = m.contact_id

            WHERE m.wamid = %s
              AND c.phone = %s

            LIMIT 1

        """, (
            context_id, phone,
            context_id, phone
        ))

    else:

        cur.execute("""
            SELECT r.campaign_id, r.contact_id

            FROM campaign_recipients r

            JOIN contacts c ON c.id = r.contact_id

            LEFT JOIN whatsapp_message_statuses s
                ON s.whatsapp_message_id = r.wamid

            LEFT JOIN LATERAL (

                SELECT MAX(m.created_at) AS last_manual

                FROM marketing_manual_messages m

                WHERE m.contact_id = r.contact_id
                  AND m.campaign_id = r.campaign_id
                  AND m.status = 'accepted'

            ) manual ON TRUE

            LEFT JOIN LATERAL (

                SELECT MAX(e.created_at) AS last_reply

                FROM marketing_replies m
                JOIN inbound_events e
                    ON e.message_id = m.message_id

                WHERE m.campaign_id = r.campaign_id
                  AND m.contact_id = r.contact_id

            ) replies ON TRUE

            WHERE c.phone = %s

              AND r.send_status = 'accepted'
              AND r.wamid IS NOT NULL

              AND (
                  COALESCE(s.delivery_status, 'accepted')
                    <> 'failed'
                  OR manual.last_manual IS NOT NULL
              )

              AND GREATEST(
                  r.attempted_at,
                  replies.last_reply,
                  manual.last_manual

              ) >= NOW() - INTERVAL '24 hours'

              AND NOT EXISTS (

                  SELECT 1
                  FROM inbound_events e

                  WHERE e.phone = c.phone
                    AND e.ad_id IS NOT NULL

                    AND e.created_at >= GREATEST(
                        r.attempted_at,
                        replies.last_reply,
                        manual.last_manual
                    )
              )

            ORDER BY GREATEST(
                r.attempted_at,
                replies.last_reply,
                manual.last_manual

            ) DESC, r.campaign_id DESC

            LIMIT 1

        """, (phone,))

    target = cur.fetchone()

    if not target:
        return False

    cur.execute("""
        INSERT INTO marketing_replies (

            message_id,
            campaign_id,
            contact_id,
            display_text

        )

        VALUES (%s, %s, %s, %s)

        ON CONFLICT (message_id) DO NOTHING
        RETURNING message_id

    """, (
        message_id,
        target["campaign_id"],
        target["contact_id"],
        display_text
    ))

    if not cur.fetchone():
        return False

    cur.execute("""
        UPDATE inbound_events

        SET state = 'processed',
            processed_at = NOW()

        WHERE message_id = %s

    """, (message_id,))

    return True


@router.get("/conversations")
def conversations(

    search: str = Query("", max_length=100),

    status: str = "all",

    offset: int = Query(0, ge=0),

    limit: int = Query(50, ge=1, le=100)

):

    if status not in ("all", "unread", "replied"):

        raise HTTPException(
            422, "Invalid filter."
        )

    pattern = "%" + search + "%"

    with db_connect() as conn, conn.cursor() as cur:

        cur.execute("""
            WITH audience AS (

                SELECT
                    contact_id,
                    MAX(attempted_at) AS last_sent

                FROM campaign_recipients

                WHERE send_status = 'accepted'

                GROUP BY contact_id

            ), replies AS (

                SELECT
                    m.contact_id,

                    COUNT(*) AS replies,

                    COUNT(*) FILTER (
                        WHERE m.read_at IS NULL
                    ) AS unread,

                    MAX(e.created_at) AS last_reply

                FROM marketing_replies m

                JOIN inbound_events e
                    ON e.message_id = m.message_id

                GROUP BY m.contact_id
            )

            SELECT

                c.id,
                c.name,
                c.phone,
                c.contact_type,
                c.consent_status,

                COALESCE(r.replies, 0) AS replies,
                COALESCE(r.unread, 0) AS unread,

                GREATEST(
                    a.last_sent,
                    r.last_reply,

                    (
                        SELECT MAX(created_at)
                        FROM marketing_manual_messages
                        WHERE contact_id = c.id
                    )

                ) AS last_at

            FROM audience a

            JOIN contacts c ON c.id = a.contact_id

            LEFT JOIN replies r ON r.contact_id = c.id

            WHERE (

                c.name ILIKE %s
                OR c.phone ILIKE %s
                OR %s = ''

                OR EXISTS (

                    SELECT 1
                    FROM marketing_replies mr

                    WHERE mr.contact_id = c.id
                    AND mr.display_text ILIKE %s
                )

                OR EXISTS (

                    SELECT 1
                    FROM marketing_manual_messages mm

                    WHERE mm.contact_id = c.id

                    AND (
                        mm.body ILIKE %s
                        OR mm.template_snapshot::text ILIKE %s
                    )
                )

                OR EXISTS (

                    SELECT 1
                    FROM campaign_recipients cr

                    JOIN marketing_campaigns mc
                        ON mc.id = cr.campaign_id

                    WHERE cr.contact_id = c.id

                      AND cr.send_status = 'accepted'

                      AND mc.template_snapshot::text ILIKE %s
                )
            )

            AND (

                %s = 'all'

                OR (%s = 'unread' AND r.unread > 0)

                OR (%s = 'replied' AND r.replies > 0)
            )

            ORDER BY last_at DESC NULLS LAST, c.id DESC

            LIMIT %s OFFSET %s

        """, (
            pattern,
            pattern,
            search,
            pattern,
            pattern,
            pattern,
            pattern,
            status,
            status,
            status,
            limit + 1,
            offset
        ))

        rows = cur.fetchall()

        cur.execute("""
            SELECT COUNT(*) AS n

            FROM marketing_replies

            WHERE read_at IS NULL
        """)

        unread_total = cur.fetchone()["n"]

    return {
        "conversations": rows[:limit],
        "has_more": len(rows) > limit,
        "unread_total": unread_total
    }


@router.get("/conversations/{contact_id}")
def conversation(
    contact_id: int,
    search: str = Query("", max_length=100)
):

    pattern = "%" + search + "%"

    with db_connect() as conn, conn.cursor() as cur:

        cur.execute("""
            SELECT
                id,
                name,
                phone,
                contact_type,
                consent_status,
                tags,
                location

            FROM contacts

            WHERE id = %s

            AND EXISTS (

                SELECT 1
                FROM campaign_recipients r

                WHERE r.contact_id = contacts.id
                  AND r.send_status = 'accepted'
            )

        """, (contact_id,))

        contact = cur.fetchone()

        if not contact:

            raise HTTPException(
                404, "Conversation not found."
            )

        # EXISTING CAMPAIGN MESSAGES

        cur.execute("""
            SELECT

                r.wamid AS id,

                'outgoing' AS direction,

                r.attempted_at AS at,

                c.name AS campaign_name,

                c.id AS campaign_id,

                c.template_snapshot,
                c.template_values,
                c.media_filename,

                COALESCE(
                    s.delivery_status,
                    r.send_status
                ) AS status

            FROM campaign_recipients r

            JOIN marketing_campaigns c
                ON c.id = r.campaign_id

            LEFT JOIN whatsapp_message_statuses s
                ON s.whatsapp_message_id = r.wamid

            WHERE r.contact_id = %s

              AND r.send_status = 'accepted'

              AND (
                  %s = ''
                  OR c.template_snapshot::text ILIKE %s
                  OR c.template_values::text ILIKE %s
              )

            ORDER BY r.attempted_at DESC

            LIMIT 100

        """, (contact_id, search, pattern, pattern))

        outgoing = cur.fetchall()

        # CUSTOMER MESSAGES

        cur.execute("""
            SELECT

                m.message_id AS id,

                'incoming' AS direction,

                e.created_at AS at,

                m.display_text AS text,

                m.campaign_id,

                m.read_at

            FROM marketing_replies m

            JOIN inbound_events e
                ON e.message_id = m.message_id

            WHERE m.contact_id = %s

              AND (
                  %s = ''
                  OR m.display_text ILIKE %s
              )

            ORDER BY e.created_at DESC

            LIMIT 100

        """, (contact_id, search, pattern))

        incoming = cur.fetchall()

        # MANUAL OUTGOING MESSAGES

        cur.execute("""
            SELECT

                m.request_id::text AS id,

                'outgoing' AS direction,

                m.created_at AS at,

                m.kind,

                m.body AS text,

                m.template_snapshot,

                m.template_values,

                m.media_filename,

                m.mime,

                m.request_id::text AS manual_id,

                CASE

                    WHEN m.status = 'sending'
                    AND m.created_at <
                        NOW() - INTERVAL '10 minutes'

                    THEN 'unknown'

                    ELSE COALESCE(
                        s.delivery_status,
                        m.status
                    )

                END AS status,

                s.error_code

            FROM marketing_manual_messages m

            LEFT JOIN whatsapp_message_statuses s
                ON s.whatsapp_message_id = m.wamid

            WHERE m.contact_id = %s

            AND (

                %s = ''

                OR m.body ILIKE %s

                OR m.template_snapshot::text ILIKE %s

                OR m.template_values::text ILIKE %s
            )

            ORDER BY m.created_at DESC

            LIMIT 100

        """, (
            contact_id,
            search,
            pattern,
            pattern,
            pattern
        ))

        manual = cur.fetchall()

        from marketing_inbox.actions import target

        info = target(cur, contact_id)

    messages = sorted(
        outgoing + incoming + manual,
        key=lambda x: (x["at"], x["id"])
    )[-100:]

    return {

        "contact": contact,

        "messages": messages,

        "window_expires_at": info["window_expires_at"],

        "lead_status": info["lead_status"],

        "assigned_salesperson": info["assigned_salesperson"]
    }


@router.post("/conversations/{contact_id}/read")
def mark_read(contact_id: int):

    with db_connect() as conn, conn.cursor() as cur:

        cur.execute("""
            UPDATE marketing_replies

            SET read_at = NOW()

            WHERE contact_id = %s
              AND read_at IS NULL

        """, (contact_id,))

        return {
            "updated": cur.rowcount
        }

    