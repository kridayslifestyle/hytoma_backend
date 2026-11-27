from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import require_admin
from db import db_connect


router = APIRouter(
    prefix="/admin/campaigns",
    tags=["Campaign Reports"],
    dependencies=[Depends(require_admin)],
)


@router.get("/{campaign_id}/report")
def campaign_report(
    campaign_id: int,
    status: str = Query(default="all"),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    allowed = {
        "all",
        "pending",
        "sending",
        "accepted",
        "sent",
        "delivered",
        "read",
        "failed",
        "rejected",
        "skipped",
        "unknown",
        "cancelled",
    }

    if status not in allowed:
        raise HTTPException(
            422,
            "Invalid report status."
        )

    with db_connect() as conn, conn.cursor() as cur:

        # Verify campaign exists

        cur.execute(
            """
            SELECT id
            FROM marketing_campaigns
            WHERE id=%s
            """,
            (campaign_id,),
        )

        if not cur.fetchone():
            raise HTTPException(
                404,
                "Campaign not found."
            )

        # Match campaign messages with WhatsApp delivery statuses

        cur.execute("""
            SELECT
                r.contact_id,
                c.name,
                c.phone,
                r.send_status,
                r.attempted_at,

                COALESCE(
                    s.delivery_status,
                    r.send_status
                ) AS status,

                s.status_timestamp,
                s.error_code,

                COALESCE(
                    s.error_details,
                    r.error
                ) AS error

            FROM campaign_recipients r

            JOIN contacts c
                ON c.id = r.contact_id

            LEFT JOIN whatsapp_message_statuses s
                ON s.whatsapp_message_id = r.wamid

            WHERE r.campaign_id = %s

            ORDER BY r.contact_id

        """, (campaign_id,))

        rows = cur.fetchall()

    # Calculate campaign statistics

    counts = Counter(
        row["status"] for row in rows
    )

    total = len(rows)

    accepted = sum(
        row["send_status"] == "accepted"
        for row in rows
    )

    sent = (
        counts["sent"]
        + counts["delivered"]
        + counts["read"]
    )

    delivered = (
        counts["delivered"]
        + counts["read"]
    )

    read = counts["read"]

    # Filter individual recipient records

    filtered = [
        row for row in rows
        if status == "all"
        or row["status"] == status
    ]

    return {
        "summary": {
            "total": total,
            "accepted": accepted,
            "sent": sent,
            "delivered": delivered,
            "read": read,
            "failed": counts["failed"],

            "delivery_rate": (
                round(delivered * 100 / accepted, 1)
                if accepted else 0
            ),

            "read_rate": (
                round(read * 100 / delivered, 1)
                if delivered else 0
            ),
        },

        "counts": dict(counts),

        "filtered_total": len(filtered),

        "recipients": filtered[
            offset:offset + limit
        ],
    }