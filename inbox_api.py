
"""HYTOMA admin conversation list."""

from fastapi import APIRouter, Depends, Query
from psycopg.rows import dict_row

from auth import require_admin
from db import db_connect


router = APIRouter(
    prefix="/admin",
    tags=["WhatsApp Inbox"],
    dependencies=[Depends(require_admin)],
)


@router.get("/conversations")
def get_conversations(
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    with db_connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT
                    l.phone,
                    l.status,
                    l.step,
                    l.project_status,
                    l.completion_timeline,
                    l.project_location,
                    l.preferred_site_visit,
                    l.assigned_salesperson,
                    l.sales_status,
                    l.needs_admin_followup,
                    l.bot_paused,
                    l.bot_paused_at,
                    l.created_at,
                    l.updated_at,

                    latest.content AS last_message_content,
                    latest.direction AS last_message_direction,
                    latest.created_at AS last_message_at,
                    latest.message_type AS last_message_type

                FROM leads AS l

                LEFT JOIN LATERAL (
                    SELECT
                        m.content,
                        m.direction,
                        m.created_at,
                        m.message_type,
                        m.id
                    FROM chat_messages AS m
                    WHERE m.phone = l.phone
                    ORDER BY m.created_at DESC, m.id DESC
                    LIMIT 1
                ) AS latest ON TRUE

                ORDER BY
                    COALESCE(
                        latest.created_at,
                        l.updated_at,
                        l.created_at
                    ) DESC,
                    l.phone ASC

                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )

            conversations = cur.fetchall()

    return {
        "conversations": conversations,
        "limit": limit,
        "offset": offset,
    }
