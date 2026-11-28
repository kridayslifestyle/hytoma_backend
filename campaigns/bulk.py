import asyncio
import logging
import os
import requests

from datetime import datetime, timezone, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StrictBool

from auth import require_admin
from db import db_connect, customer_send_guard
from campaigns.api import eligible_templates
from campaigns.send_test import (
    build_components,
    signature,
    verify_media,
)
from whatsapp_service import send_whatsapp_payload


router = APIRouter(
    prefix="/admin/campaigns",
    tags=["Campaign Queue"],
    dependencies=[Depends(require_admin)],
)

logger = logging.getLogger("hytoma.campaigns")

PILOT_MAX = 10
PRODUCTION_MAX = 100
DAILY_MAX = 100


def initialize_campaign_queue():
    with db_connect() as conn, conn.cursor() as cur:

        cur.execute("""
            ALTER TABLE marketing_campaigns
            DROP CONSTRAINT IF EXISTS
            marketing_campaigns_status_check;

            ALTER TABLE marketing_campaigns
            ADD CONSTRAINT marketing_campaigns_status_check
            CHECK (
                status IN (
                    'draft',
                    'scheduled',
                    'running',
                    'paused',
                    'cancelled',
                    'completed'
                )
            );

            ALTER TABLE marketing_campaigns
            ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;

            ALTER TABLE marketing_campaigns
            ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;

            ALTER TABLE marketing_campaigns
            ADD COLUMN IF NOT EXISTS scheduled_at TIMESTAMPTZ;

            ALTER TABLE marketing_campaigns
            ADD COLUMN IF NOT EXISTS send_mode
            TEXT NOT NULL DEFAULT 'pilot';

            ALTER TABLE campaign_recipients
            ADD COLUMN IF NOT EXISTS send_status
            TEXT NOT NULL DEFAULT 'pending';

            ALTER TABLE campaign_recipients
            ADD COLUMN IF NOT EXISTS attempted_at TIMESTAMPTZ;

            ALTER TABLE campaign_recipients
            ADD COLUMN IF NOT EXISTS wamid TEXT;

            ALTER TABLE campaign_recipients
            ADD COLUMN IF NOT EXISTS error TEXT;

            CREATE INDEX IF NOT EXISTS campaign_queue_idx
            ON campaign_recipients(
                campaign_id,
                send_status,
                contact_id
            );

            CREATE UNIQUE INDEX IF NOT EXISTS
            campaign_queue_wamid_idx
            ON campaign_recipients(wamid)
            WHERE wamid IS NOT NULL;

        """, prepare=False)


def allowed_numbers():
    return {
        n.strip().lstrip("+")
        for n in os.getenv(
            "CAMPAIGN_TEST_NUMBERS", ""
        ).split(",")
        if n.strip()
    }


def enabled():
    return (
        os.getenv(
            "CAMPAIGN_BULK_ENABLED", ""
        ).lower() == "true"
    )


def configured_mode():
    return os.getenv(
        "CAMPAIGN_SEND_MODE",
        "pilot",
    ).strip().lower()


def mode_enabled(mode):

    if not enabled() or mode != configured_mode():
        return False

    if mode == "pilot":
        return bool(allowed_numbers())

    return (
        mode == "production"
        and os.getenv(
            "CAMPAIGN_PRODUCTION_ENABLED", ""
        ).lower() == "true"
    )


def mode_limit(mode):
    return (
        PRODUCTION_MAX
        if mode == "production"
        else PILOT_MAX
    )


def eligible_contact(contact, mode):

    phone = contact["phone"]

    return (
        contact["consent_status"] == "opted_in"

        and contact.get("archived_at") is None

        and not contact["blocked"]

        and not (
            phone.startswith("120255501")
            and len(phone) == 11
        )

        and (
            mode == "production"
            or phone in allowed_numbers()
        )
    )


def validate_message(row):

    live = next(
        (
            t for t in eligible_templates()
            if str(t.get("id")) == row["template_id"]
        ),
        None,
    )

    if (
        not live
        or live.get("name") != row["template_name"]
        or live.get("language") != row["template_language"]
        or signature(live.get("components", []))
        != signature(
            row["template_snapshot"].get("components", [])
        )
    ):
        raise HTTPException(
            409,
            "Template changed or is not approved.",
        )

    components = build_components(row)

    for component in components:

        if component.get("sub_type") == "quick_reply":

            component["parameters"][0]["payload"] = (
                component["parameters"][0]["payload"].replace(
                    "campaign_test:",
                    "campaign:",
                    1,
                )
            )

    verify_media(row)

    return {
        "name": row["template_name"],
        "language": {
            "code": row["template_language"]
        },
        **(
            {"components": components}
            if components else {}
        ),
    }


def campaign_row(cur, campaign_id, lock=False):

    cur.execute(
        """
        SELECT *
        FROM marketing_campaigns
        WHERE id=%s
        """ + (" FOR UPDATE" if lock else ""),
        (campaign_id,),
    )

    row = cur.fetchone()

    if not row:
        raise HTTPException(
            404,
            "Campaign not found.",
        )

    return row


class LaunchRequest(BaseModel):

    confirmed: StrictBool

    expected_recipients: int = Field(
        ge=1,
        le=PRODUCTION_MAX,
    )

    mode: Literal[
        "pilot",
        "production",
    ] = "pilot"


class ScheduleRequest(LaunchRequest):

    scheduled_at: datetime


class ConfirmRequest(BaseModel):

    confirmed: StrictBool


def prepare_campaign(
    campaign_id,
    payload,
    scheduled_at=None,
):

    mode = payload.mode

    if not mode_enabled(mode):
        raise HTTPException(
            403,
            "Requested sending mode is disabled.",
        )

    if not payload.confirmed:
        raise HTTPException(
            422,
            "Confirm the saved audience.",
        )

    permitted = (
        ("draft", "scheduled")
        if scheduled_at
        else ("draft",)
    )

    if scheduled_at:

        now = datetime.now(timezone.utc)

        if (
            scheduled_at.tzinfo is None
            or not (
                now + timedelta(minutes=1)
                <= scheduled_at
                <= now + timedelta(days=7)
            )
        ):
            raise HTTPException(
                422,
                "Schedule between 1 minute and 7 days "
                "in the future with a timezone.",
            )

    with db_connect() as conn, conn.cursor() as cur:

        row = campaign_row(cur, campaign_id)

    if (
        row["status"] == "scheduled"
        and row["send_mode"] != mode
    ):
        raise HTTPException(
            409,
            "Remove the existing schedule before changing mode.",
        )

    if row["status"] not in permitted:
        raise HTTPException(
            409,
            "Campaign cannot be launched or scheduled "
            "in its current status.",
        )

    validate_message(row)

    with db_connect() as conn, conn.cursor() as cur:

        current = campaign_row(
            cur,
            campaign_id,
            True,
        )

        if (
            current["status"] != row["status"]
            or current["scheduled_at"] != row["scheduled_at"]
            or any(
                current[k] != row[k]
                for k in (
                    "template_snapshot",
                    "template_values",
                    "media_id",
                    "media_type",
                    "audience_filters",
                    "send_mode",
                )
            )
        ):
            raise HTTPException(
                409,
                "Campaign changed. Review again.",
            )

        cur.execute("""
            SELECT
                c.phone,
                c.consent_status,
                c.archived_at,

                EXISTS (
                    SELECT 1
                    FROM leads l
                    WHERE l.phone = c.phone
                    AND l.status = 'opted_out'
                ) AS blocked

            FROM campaign_recipients r

            JOIN contacts c
                ON c.id = r.contact_id

            WHERE r.campaign_id=%s
            AND r.send_status='pending'

        """, (campaign_id,))

        recipients = cur.fetchall()

        if (
            len(recipients) != payload.expected_recipients
            or not recipients
            or len(recipients) > mode_limit(mode)
        ):
            raise HTTPException(
                422,
                "Recipient count changed or exceeds sending limit.",
            )

        if any(
            not eligible_contact(r, mode)
            for r in recipients
        ):
            raise HTTPException(
                403,
                "All recipients must be eligible and opted in "
                "for this sending mode.",
            )

        if scheduled_at:

            cur.execute("""
                UPDATE marketing_campaigns

                SET status='scheduled',
                    scheduled_at=%s,
                    send_mode=%s

                WHERE id=%s

            """, (
                scheduled_at,
                mode,
                campaign_id,
            ))

        else:

            cur.execute("""
                UPDATE marketing_campaigns

                SET status='running',
                    started_at=NOW(),
                    scheduled_at=NULL,
                    send_mode=%s

                WHERE id=%s

            """, (
                mode,
                campaign_id,
            ))

    return {
        "status": (
            "scheduled"
            if scheduled_at
            else "running"
        ),
        "recipients": len(recipients),
        "scheduled_at": scheduled_at,
    }


@router.post("/{campaign_id}/launch")
def launch(
    campaign_id: int,
    payload: LaunchRequest,
):
    return prepare_campaign(
        campaign_id,
        payload,
    )


@router.post("/{campaign_id}/schedule")
def schedule(
    campaign_id: int,
    payload: ScheduleRequest,
):
    return prepare_campaign(
        campaign_id,
        payload,
        payload.scheduled_at,
    )


@router.post("/{campaign_id}/unschedule")
def unschedule(
    campaign_id: int,
    payload: ConfirmRequest,
):

    if not payload.confirmed:
        raise HTTPException(
            422,
            "Confirm removing the schedule.",
        )

    with db_connect() as conn, conn.cursor() as cur:

        cur.execute("""
            UPDATE marketing_campaigns

            SET status='draft',
                scheduled_at=NULL

            WHERE id=%s
            AND status='scheduled'

            RETURNING id

        """, (campaign_id,))

        if not cur.fetchone():
            raise HTTPException(
                409,
                "Only scheduled campaigns can be unscheduled.",
            )

    return {"status": "draft"}


@router.post("/{campaign_id}/pause")
def pause(campaign_id: int):

    with db_connect() as conn, conn.cursor() as cur:

        cur.execute("""
            UPDATE marketing_campaigns

            SET status='paused'

            WHERE id=%s
            AND status='running'

            RETURNING id

        """, (campaign_id,))

        if not cur.fetchone():
            raise HTTPException(
                409,
                "Only running campaigns can pause.",
            )

    return {"status": "paused"}


@router.post("/{campaign_id}/resume")
def resume(
    campaign_id: int,
    payload: ConfirmRequest,
):

    if not payload.confirmed:
        raise HTTPException(
            403,
            "Confirmation missing.",
        )

    with db_connect() as conn, conn.cursor() as cur:

        row = campaign_row(
            cur,
            campaign_id,
            True,
        )

        if not mode_enabled(row["send_mode"]):
            raise HTTPException(
                403,
                "Campaign sending mode is disabled.",
            )

        cur.execute("""
            UPDATE marketing_campaigns

            SET status='running'

            WHERE id=%s
            AND status='paused'

            RETURNING id

        """, (campaign_id,))

        if not cur.fetchone():
            raise HTTPException(
                409,
                "Only paused campaigns can resume.",
            )

    return {"status": "running"}


@router.post("/{campaign_id}/cancel")
def cancel(
    campaign_id: int,
    payload: ConfirmRequest,
):

    if not payload.confirmed:
        raise HTTPException(
            422,
            "Confirm cancellation.",
        )

    with db_connect() as conn, conn.cursor() as cur:

        cur.execute("""
            UPDATE marketing_campaigns

            SET status='cancelled',
                finished_at=NOW()

            WHERE id=%s
            AND status IN (
                'draft',
                'scheduled',
                'running',
                'paused'
            )

            RETURNING id

        """, (campaign_id,))

        if not cur.fetchone():
            raise HTTPException(
                409,
                "Campaign cannot be cancelled.",
            )

        cur.execute("""
            UPDATE campaign_recipients

            SET send_status='cancelled'

            WHERE campaign_id=%s
            AND send_status='pending'

        """, (campaign_id,))

    return {"status": "cancelled"}


@router.get("/{campaign_id}/progress")
def progress(campaign_id: int):

    with db_connect() as conn, conn.cursor() as cur:

        row = campaign_row(
            cur,
            campaign_id,
        )

        cur.execute("""
            SELECT
                send_status,
                COUNT(*) AS count

            FROM campaign_recipients

            WHERE campaign_id=%s

            GROUP BY send_status

        """, (campaign_id,))

        counts = {
            r["send_status"]: r["count"]
            for r in cur.fetchall()
        }

        cur.execute("""
            SELECT COUNT(*) AS n

            FROM campaign_recipients

            WHERE attempted_at >
                NOW() - INTERVAL '24 hours'
        """)

        remaining = max(
            0,
            DAILY_MAX - cur.fetchone()["n"],
        )

    mode = (
        configured_mode()
        if row["status"] == "draft"
        else row["send_mode"]
    )

    return {
        "status": row["status"],
        "total": sum(counts.values()),
        "counts": counts,
        "scheduled_at": row["scheduled_at"],
        "mode": mode,
        "sending_enabled": mode_enabled(mode),
        "max_recipients": mode_limit(mode),
        "pilot_enabled": mode_enabled("pilot"),
        "daily_remaining": (
            remaining
            if mode == "production"
            else None
        ),
    }


def finish(cur, campaign_id):

    cur.execute("""
        UPDATE marketing_campaigns

        SET status='completed',
            finished_at=NOW()

        WHERE id=%s
        AND status='running'

        AND NOT EXISTS (
            SELECT 1
            FROM campaign_recipients
            WHERE campaign_id=%s
            AND send_status IN ('pending', 'sending')
        )

    """, (
        campaign_id,
        campaign_id,
    ))


def process_campaign_once():

    mode = configured_mode()

    if not mode_enabled(mode):
        return

    with db_connect() as lock_conn, lock_conn.cursor() as guard:

        # Prevent multiple campaign workers from
        # submitting messages simultaneously.

        guard.execute("""
            SELECT pg_try_advisory_xact_lock(
                845167241
            ) AS locked
        """)

        if not guard.fetchone()["locked"]:
            return

        with db_connect() as conn, conn.cursor() as cur:

            # An interrupted submission must not
            # be automatically retried.

            cur.execute("""
                UPDATE campaign_recipients

                SET send_status='unknown',
                    error='Worker interrupted'

                WHERE send_status='sending'

                RETURNING campaign_id
            """)

            affected = {
                r["campaign_id"]
                for r in cur.fetchall()
            }

            for cid in affected:

                cur.execute("""
                    UPDATE marketing_campaigns

                    SET status='paused'

                    WHERE id=%s
                    AND status='running'

                """, (cid,))

            # Expired schedules return to draft.

            cur.execute("""
                UPDATE marketing_campaigns

                SET status='draft',
                    scheduled_at=NULL

                WHERE status='scheduled'

                AND scheduled_at <
                    NOW() - INTERVAL '15 minutes'
            """)

            # Start due campaigns for the active mode.

            cur.execute("""
                UPDATE marketing_campaigns

                SET status='running',
                    started_at=NOW()

                WHERE status='scheduled'
                AND scheduled_at <= NOW()
                AND send_mode=%s

            """, (mode,))

            # Minimum 5 seconds between attempts.

            cur.execute("""
                SELECT 1
                FROM campaign_recipients

                WHERE attempted_at >
                    NOW() - INTERVAL '5 seconds'

                LIMIT 1
            """)

            if cur.fetchone():
                return

            # Production rolling 24-hour limit.

            if mode == "production":

                cur.execute("""
                    SELECT COUNT(*) AS n

                    FROM campaign_recipients

                    WHERE attempted_at >
                        NOW() - INTERVAL '24 hours'
                """)

                if cur.fetchone()["n"] >= DAILY_MAX:
                    return

            cur.execute("""
                SELECT id
                FROM marketing_campaigns

                WHERE status='running'
                AND send_mode=%s

                ORDER BY id

                LIMIT 1

            """, (mode,))

            picked = cur.fetchone()

            if not picked:
                return

            cid = picked["id"]

            row = campaign_row(
                cur,
                cid,
            )

            cur.execute("""
                SELECT
                    r.contact_id,
                    c.phone

                FROM campaign_recipients r

                JOIN contacts c
                    ON c.id = r.contact_id

                WHERE r.campaign_id=%s
                AND r.send_status='pending'

                ORDER BY r.contact_id

                LIMIT 1

            """, (cid,))

            recipient = cur.fetchone()

            if not recipient:

                finish(cur, cid)

                return

        try:

            message = validate_message(row)

        except HTTPException:

            with db_connect() as conn, conn.cursor() as cur:

                cur.execute("""
                    UPDATE marketing_campaigns

                    SET status='paused'

                    WHERE id=%s
                    AND status='running'

                """, (cid,))

            return

        phone = recipient["phone"]

        rid = recipient["contact_id"]

        with (
            customer_send_guard(phone),
            db_connect() as consent_conn,
            consent_conn.cursor() as consent_cur,
        ):

            # Coordinate with concurrent contact
            # consent updates during submission.

            consent_cur.execute("""
                SELECT id
                FROM contacts

                WHERE id=%s

                FOR NO KEY UPDATE

            """, (rid,))

            with db_connect() as conn, conn.cursor() as cur:

                current = campaign_row(
                    cur,
                    cid,
                    True,
                )

                if (
                    not mode_enabled(mode)
                    or current["status"] != "running"
                    or current["send_mode"] != mode
                ):
                    return

                # Recheck recipient consent before sending.

                cur.execute("""
                    SELECT
                        c.phone,
                        c.consent_status,
                        c.archived_at,

                        EXISTS (
                            SELECT 1
                            FROM leads l

                            WHERE l.phone = c.phone
                            AND l.status = 'opted_out'
                        ) AS blocked

                    FROM contacts c

                    WHERE c.id=%s

                """, (rid,))

                contact = cur.fetchone()

                if (
                    not contact
                    or contact["phone"] != phone
                    or not eligible_contact(contact, mode)
                ):

                    cur.execute("""
                        UPDATE campaign_recipients

                        SET send_status='skipped',
                            error='Recipient no longer eligible'

                        WHERE campaign_id=%s
                        AND contact_id=%s
                        AND send_status='pending'

                    """, (cid, rid))

                    finish(cur, cid)

                    return

                # Avoid repeatedly targeting the
                # same contact in production mode.

                if mode == "production":

                    cur.execute("""
                        SELECT 1
                        FROM campaign_recipients

                        WHERE contact_id=%s
                        AND attempted_at >
                            NOW() - INTERVAL '24 hours'

                        LIMIT 1

                    """, (rid,))

                    if cur.fetchone():

                        cur.execute("""
                            UPDATE campaign_recipients

                            SET send_status='skipped',
                                error='Recent campaign attempt'

                            WHERE campaign_id=%s
                            AND contact_id=%s
                            AND send_status='pending'

                        """, (cid, rid))

                        finish(cur, cid)

                        return

                # Claim recipient before sending.

                cur.execute("""
                    UPDATE campaign_recipients

                    SET send_status='sending',
                        attempted_at=NOW()

                    WHERE campaign_id=%s
                    AND contact_id=%s
                    AND send_status='pending'

                    RETURNING contact_id

                """, (cid, rid))

                if not cur.fetchone():
                    return

            status = "unknown"

            wamid = None

            error = "Uncertain send outcome"

            try:

                result = send_whatsapp_payload({
                    "messaging_product": "whatsapp",
                    "to": phone,
                    "type": "template",
                    "template": message,
                })

                wamid = result["messages"][0]["id"]

                status = "accepted"

                error = None

            except requests.HTTPError as exc:

                if (
                    exc.response is not None
                    and 400 <= exc.response.status_code < 500
                ):

                    status = "rejected"

                    error = (
                        f"Meta HTTP {exc.response.status_code}"
                    )

            except Exception:

                logger.exception(
                    "Campaign request outcome uncertain"
                )

            with db_connect() as conn, conn.cursor() as cur:

                campaign_row(
                    cur,
                    cid,
                    True,
                )

                cur.execute("""
                    UPDATE campaign_recipients

                    SET send_status=%s,
                        wamid=%s,
                        error=%s

                    WHERE campaign_id=%s
                    AND contact_id=%s
                    AND send_status='sending'

                """, (
                    status,
                    wamid,
                    error,
                    cid,
                    rid,
                ))

                if status != "accepted":

                    cur.execute("""
                        UPDATE marketing_campaigns

                        SET status='paused'

                        WHERE id=%s
                        AND status='running'

                    """, (cid,))

                finish(cur, cid)


async def campaign_worker_loop():

    while True:

        try:

            await asyncio.to_thread(
                process_campaign_once
            )

        except asyncio.CancelledError:

            raise

        except Exception:

            logger.exception(
                "Campaign queue error"
            )

        await asyncio.sleep(2)