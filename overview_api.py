from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
)

from auth import require_admin
from db import db_connect


router = APIRouter(
    prefix="/admin",
    tags=["Overview Analytics"],
    dependencies=[Depends(require_admin)]
)

IST = ZoneInfo("Asia/Kolkata")


@router.get("/overview")
def overview(
    start: date | None = None,
    end: date | None = None,
):

    today = datetime.now(IST).date()

    end = end or today
    start = start or end - timedelta(days=29)

    if (
        start > end
        or end > today
        or (end - start).days > 365
    ):

        raise HTTPException(
            422,
            "Choose a valid date range of at most 366 days."
        )

    lower = datetime.combine(
        start,
        datetime.min.time(),
        IST
    )

    upper = datetime.combine(
        end + timedelta(days=1),
        datetime.min.time(),
        IST
    )

    bounds = (lower, upper)

    with db_connect() as conn, conn.cursor() as cur:

        cur.execute("""
            SET TRANSACTION ISOLATION LEVEL
            REPEATABLE READ READ ONLY
        """)

        # LEAD ANALYTICS

        cur.execute("""
            SELECT

                COUNT(*) AS total,

                COUNT(*) FILTER (
                    WHERE status = 'completed'
                ) AS completed,

                COUNT(*) FILTER (
                    WHERE status = 'deferred'
                ) AS deferred,

                COUNT(*) FILTER (
                    WHERE needs_admin_followup
                ) AS followups,

                COUNT(*) FILTER (
                    WHERE assigned_salesperson IS NOT NULL
                    AND assigned_salesperson <> ''
                ) AS assigned,

                COUNT(*) FILTER (
                    WHERE sales_status = 'closed'
                ) AS closed,

                COUNT(*) FILTER (
                    WHERE sales_status = 'visit_scheduled'
                ) AS visits

            FROM leads

            WHERE created_at >= %s
              AND created_at < %s

        """, bounds)

        leads = cur.fetchone()


        # CONTACT ANALYTICS

        cur.execute("""
            SELECT

                COUNT(*) AS active,

                COUNT(*) FILTER (
                    WHERE consent_status = 'opted_in'
                ) AS marketable

            FROM contacts

            WHERE archived_at IS NULL
        """)

        contacts = cur.fetchone()

        cur.execute("""
            SELECT COUNT(*) AS added

            FROM contacts

            WHERE created_at >= %s
              AND created_at < %s

        """, bounds)

        contacts.update(cur.fetchone())


        # CAMPAIGN COUNTS

        cur.execute("""
            SELECT

                COUNT(*) AS campaigns_created,

                COUNT(*) FILTER (
                    WHERE status = 'completed'
                ) AS completed

            FROM marketing_campaigns

            WHERE created_at >= %s
              AND created_at < %s

        """, bounds)

        campaigns = cur.fetchone()


        # UNREAD MARKETING REPLIES

        cur.execute("""
            SELECT

                COUNT(*) AS unread_messages,

                COUNT(DISTINCT contact_id)
                    AS unread_conversations

            FROM marketing_replies

            WHERE read_at IS NULL
        """)

        unread = cur.fetchone()


        # CAMPAIGN RECIPIENT STATISTICS

        base = """
            WITH attempts AS (

                SELECT

                    r.campaign_id,
                    r.contact_id,
                    r.attempted_at,
                    r.send_status,

                    COALESCE(
                        s.delivery_status,
                        r.send_status
                    ) AS delivery_status,

                    COALESCE(m.replies, 0) AS replies

                FROM campaign_recipients r

                LEFT JOIN whatsapp_message_statuses s

                    ON s.whatsapp_message_id = r.wamid

                LEFT JOIN (

                    SELECT
                        campaign_id,
                        contact_id,
                        COUNT(*) AS replies

                    FROM marketing_replies

                    GROUP BY
                        campaign_id,
                        contact_id

                ) m

                    ON m.campaign_id = r.campaign_id

                   AND m.contact_id = r.contact_id

                WHERE r.attempted_at >= %s
                  AND r.attempted_at < %s

            )
        """

        totals = """

            COUNT(*) AS attempted,

            COUNT(*) FILTER (

                WHERE send_status = 'accepted'

            ) AS accepted,

            COUNT(*) FILTER (

                WHERE delivery_status IN (
                    'sent',
                    'delivered',
                    'read'
                )

            ) AS sent,

            COUNT(*) FILTER (

                WHERE delivery_status IN (
                    'delivered',
                    'read'
                )

            ) AS delivered,

            COUNT(*) FILTER (

                WHERE delivery_status = 'read'

            ) AS read,

            COUNT(*) FILTER (

                WHERE delivery_status = 'failed'

            ) AS failed,

            COUNT(*) FILTER (

                WHERE send_status = 'rejected'

            ) AS rejected,

            COUNT(*) FILTER (

                WHERE send_status = 'unknown'

            ) AS unknown,

            COUNT(*) FILTER (

                WHERE send_status = 'accepted'
                AND replies > 0

            ) AS replied,

            COALESCE(

                SUM(replies) FILTER (
                    WHERE send_status = 'accepted'
                ),

                0

            ) AS replies
        """

        cur.execute(
            base + "SELECT " + totals +
            " FROM attempts",
            bounds
        )

        marketing = cur.fetchone()


        # CAMPAIGN-WISE COMPARISON

        cur.execute(
            base +

            """
                SELECT
                    a.campaign_id,
                    c.name,
                    c.status,
            """

            + totals +

            """
                FROM attempts a

                JOIN marketing_campaigns c

                    ON c.id = a.campaign_id

                GROUP BY
                    a.campaign_id,
                    c.name,
                    c.status

                ORDER BY
                    MAX(a.attempted_at) DESC,
                    a.campaign_id DESC

                LIMIT 20
            """,

            bounds
        )

        comparison = cur.fetchall()


        # SALESPERSON PERFORMANCE

        cur.execute("""
            SELECT

                COALESCE(

                    NULLIF(
                        assigned_salesperson,
                        ''
                    ),

                    'Unassigned'

                ) AS name,

                COUNT(*) AS leads,

                COUNT(*) FILTER (
                    WHERE sales_status = 'contacted'
                ) AS contacted,

                COUNT(*) FILTER (
                    WHERE sales_status = 'visit_scheduled'
                ) AS visits,

                COUNT(*) FILTER (
                    WHERE sales_status = 'closed'
                ) AS closed,

                COUNT(*) FILTER (
                    WHERE needs_admin_followup
                ) AS followups

            FROM leads

            WHERE created_at >= %s
              AND created_at < %s

            GROUP BY 1

            ORDER BY leads DESC, name

        """, bounds)

        sales = cur.fetchall()


        # DAILY PERFORMANCE CHARTS

        cur.execute("""
            SELECT

                d.day::date AS day,

                COALESCE(l.n, 0) AS leads,

                COALESCE(c.n, 0) AS contacts,

                COALESCE(a.accepted, 0) AS accepted,

                COALESCE(a.delivered, 0) AS delivered,

                COALESCE(m.n, 0) AS replies

            FROM generate_series(

                %s::date::timestamp,

                %s::date::timestamp,

                INTERVAL '1 day'

            ) d(day)

            LEFT JOIN (

                SELECT

                    (
                        created_at AT TIME ZONE
                        'Asia/Kolkata'
                    )::date AS day,

                    COUNT(*) AS n

                FROM leads

                WHERE created_at >= %s
                  AND created_at < %s

                GROUP BY 1

            ) l ON l.day = d.day::date

            LEFT JOIN (

                SELECT

                    (
                        created_at AT TIME ZONE
                        'Asia/Kolkata'
                    )::date AS day,

                    COUNT(*) AS n

                FROM contacts

                WHERE created_at >= %s
                  AND created_at < %s

                GROUP BY 1

            ) c ON c.day = d.day::date

            LEFT JOIN (

                SELECT

                    (
                        r.attempted_at AT TIME ZONE
                        'Asia/Kolkata'
                    )::date AS day,

                    COUNT(*) FILTER (

                        WHERE r.send_status = 'accepted'

                    ) AS accepted,

                    COUNT(*) FILTER (

                        WHERE s.delivery_status IN (
                            'delivered',
                            'read'
                        )

                    ) AS delivered

                FROM campaign_recipients r

                LEFT JOIN whatsapp_message_statuses s

                    ON s.whatsapp_message_id = r.wamid

                WHERE r.attempted_at >= %s
                  AND r.attempted_at < %s

                GROUP BY 1

            ) a ON a.day = d.day::date

            LEFT JOIN (

                SELECT

                    (
                        e.created_at AT TIME ZONE
                        'Asia/Kolkata'
                    )::date AS day,

                    COUNT(*) AS n

                FROM marketing_replies m

                JOIN inbound_events e

                    ON e.message_id = m.message_id

                WHERE e.created_at >= %s
                  AND e.created_at < %s

                GROUP BY 1

            ) m ON m.day = d.day::date

            ORDER BY d.day

        """, (
            start,
            end,
            *bounds,
            *bounds,
            *bounds,
            *bounds
        ))

        trend = cur.fetchall()

    return {
        "start": start,
        "end": end,
        "timezone": "Asia/Kolkata",
        "leads": leads,
        "contacts": contacts,
        "campaigns": campaigns,
        "marketing": marketing,
        "unread": unread,
        "comparison": comparison,
        "sales": sales,
        "trend": trend,
        "as_of": datetime.now(timezone.utc)
    }
