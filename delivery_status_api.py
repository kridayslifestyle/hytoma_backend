
"""Read outgoing WhatsApp delivery states."""

from fastapi import APIRouter, Depends, HTTPException
from psycopg.rows import dict_row

from auth import require_admin
from db import db_connect


router = APIRouter(
    prefix="/admin",
    tags=["WhatsApp Delivery Tracking"],
    dependencies=[Depends(require_admin)],
)


@router.get("/leads/{phone}/delivery-statuses")
def get_delivery_statuses(phone: str):
    with db_connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT phone
                FROM leads
                WHERE phone = %s
                """,
                (phone,),
            )

            if cur.fetchone() is None:
                raise HTTPException(
                    status_code=404,
                    detail="Lead not found",
                )

            cur.execute(
                """
                SELECT
                    m.whatsapp_message_id,
                    s.delivery_status,
                    s.status_timestamp,
                    s.error_code,
                    s.error_details

                FROM chat_messages AS m

                LEFT JOIN whatsapp_message_statuses AS s
                    ON s.whatsapp_message_id =
                       m.whatsapp_message_id

                WHERE m.phone = %s
                  AND m.direction = 'outgoing'
                  AND m.whatsapp_message_id IS NOT NULL

                ORDER BY m.created_at DESC, m.id DESC
                LIMIT 200
                """,
                (phone,),
            )

            messages = cur.fetchall()

    return {"statuses": messages}
