import csv
import io
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from auth import require_admin
from db import db_connect


router = APIRouter(
    prefix="/admin/campaigns",
    tags=["Campaign Reports"],
    dependencies=[Depends(require_admin)],
)


def report_rows(campaign_id):

    with db_connect() as conn, conn.cursor() as cur:

        cur.execute(
            "SELECT id FROM marketing_campaigns WHERE id=%s",
            (campaign_id,),
        )

        if not cur.fetchone():
            raise HTTPException(404, "Campaign not found.")

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
                ) AS error,

                COALESCE(m.reply_count, 0) AS reply_count,
                m.last_reply_at

            FROM campaign_recipients r

            JOIN contacts c
                ON c.id = r.contact_id

            LEFT JOIN whatsapp_message_statuses s
                ON s.whatsapp_message_id = r.wamid

            LEFT JOIN LATERAL (
                SELECT
                    COUNT(*) AS reply_count,
                    MAX(e.created_at) AS last_reply_at

                FROM marketing_replies m

                JOIN inbound_events e
                    ON e.message_id = m.message_id

                WHERE m.campaign_id = r.campaign_id
                    AND m.contact_id = r.contact_id

            ) m ON TRUE

            WHERE r.campaign_id = %s

            ORDER BY r.contact_id

        """, (campaign_id,))

        return cur.fetchall()


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
            422, "Invalid report status."
        )

    rows = report_rows(campaign_id)

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

    replied = sum(
        row["reply_count"] > 0
        for row in rows
    )

    replies = sum(
        row["reply_count"]
        for row in rows
    )

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
            "replied": replied,
            "replies": replies,

            "delivery_rate": (
                round(delivered * 100 / accepted, 1)
                if accepted else 0
            ),

            "read_rate": (
                round(read * 100 / delivered, 1)
                if delivered else 0
            ),

            "reply_rate": (
                round(replied * 100 / accepted, 1)
                if accepted else 0
            ),
        },

        "counts": dict(counts),

        "filtered_total": len(filtered),

        "recipients": filtered[
            offset:offset + limit
        ],
    }


def safe_csv_cell(value):

    if value is None:
        return ""

    value = str(value)

    if (
        value.lstrip().startswith(("=", "+", "-", "@"))
        or value.startswith(("\t", "\r", "\n"))
    ):
        return "'" + value

    return value


@router.get("/{campaign_id}/report.csv")
def export_campaign_report(campaign_id: int):

    rows = report_rows(campaign_id)

    fields = [
        "contact_id",
        "name",
        "phone",
        "send_status",
        "status",
        "attempted_at",
        "status_timestamp",
        "reply_count",
        "last_reply_at",
        "error_code",
        "error",
    ]

    output = io.StringIO()

    writer = csv.writer(output)

    writer.writerow(fields)

    for row in rows:

        writer.writerow([
            safe_csv_cell(row.get(field))
            for field in fields
        ])

    return Response(
        content=output.getvalue().encode("utf-8-sig"),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="campaign-{campaign_id}-report.csv"'
            ),
            "Cache-Control": "no-store",
        },
    )