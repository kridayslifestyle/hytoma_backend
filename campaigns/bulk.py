import asyncio
import logging
import os

import requests
from datetime import datetime, timezone, timedelta

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
)

from pydantic import (
    BaseModel,
    Field,
    StrictBool,
)

from auth import require_admin

from db import (
    db_connect,
    customer_send_guard,
)

from campaigns.api import eligible_templates

from campaigns.send_test import (
    build_components,
    signature,
    verify_media,
)

from whatsapp_service import send_whatsapp_payload

logger = logging.getLogger("hytoma.campaigns")

router = APIRouter(
    prefix="/admin/campaigns",
    tags=["Campaign Queue"],
    dependencies=[Depends(require_admin)],
)

PILOT_MAX = 10


# --------------------------------------------------
# Database Migration
# --------------------------------------------------


def initialize_campaign_queue():

    with db_connect() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """

                ALTER TABLE marketing_campaigns

                DROP CONSTRAINT IF EXISTS
                    marketing_campaigns_status_check;


                ALTER TABLE marketing_campaigns

                ADD CONSTRAINT
                    marketing_campaigns_status_check

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

                ADD COLUMN IF NOT EXISTS
                    started_at TIMESTAMPTZ,

                ADD COLUMN IF NOT EXISTS
                    finished_at TIMESTAMPTZ;

                ALTER TABLE marketing_campaigns
                    ADD COLUMN IF NOT EXISTS scheduled_at TIMESTAMPTZ;


                ALTER TABLE campaign_recipients

                ADD COLUMN IF NOT EXISTS
                    send_status TEXT
                    NOT NULL DEFAULT 'pending',

                ADD COLUMN IF NOT EXISTS
                    attempted_at TIMESTAMPTZ,

                ADD COLUMN IF NOT EXISTS
                    wamid TEXT,

                ADD COLUMN IF NOT EXISTS
                    error TEXT;


                CREATE INDEX IF NOT EXISTS
                    campaign_queue_idx

                ON campaign_recipients(
                    campaign_id,
                    send_status,
                    contact_id
                );


                CREATE UNIQUE INDEX IF NOT EXISTS
                    campaign_queue_wamid_idx

                ON campaign_recipients(wamid)

                WHERE wamid IS NOT NULL;

            """,
                prepare=False,
            )


# --------------------------------------------------
# Safety Configuration
# --------------------------------------------------


def allowed_numbers():

    return {
        number.strip().lstrip("+")
        for number in os.getenv(
            "CAMPAIGN_TEST_NUMBERS",
            "",
        ).split(",")
        if number.strip()
    }


def enabled():

    return (
        os.getenv(
            "CAMPAIGN_BULK_ENABLED",
            "",
        ).lower()
        == "true"
    )


# --------------------------------------------------
# Validate Template and Build Message
# --------------------------------------------------


def validate_message(row):

    live = next(
        (
            template
            for template in eligible_templates()
            if str(template.get("id")) == row["template_id"]
        ),
        None,
    )

    if (
        not live
        or live.get("name") != row["template_name"]
        or live.get("language") != row["template_language"]
        or signature(live.get("components", []))
        != signature(
            row["template_snapshot"].get(
                "components",
                [],
            )
        )
    ):

        raise HTTPException(
            409,
            "Template changed or is not approved.",
        )

    components = build_components(row)

    # Identify campaign quick replies separately
    # from test message quick replies.

    for component in components:

        if component.get("sub_type") == "quick_reply":

            component["parameters"][0]["payload"] = component["parameters"][0][
                "payload"
            ].replace(
                "campaign_test:",
                "campaign:",
                1,
            )

    # Verify actual uploaded media.

    verify_media(row)

    message = {
        "name": row["template_name"],
        "language": {
            "code": row["template_language"],
        },
    }

    if components:

        message["components"] = components

    return message


# --------------------------------------------------
# Retrieve Campaign
# --------------------------------------------------


def campaign_row(
    cur,
    campaign_id,
    lock=False,
):

    cur.execute(
        "SELECT * FROM marketing_campaigns "
        "WHERE id=%s" + (" FOR UPDATE" if lock else ""),
        (campaign_id,),
    )

    row = cur.fetchone()

    if not row:

        raise HTTPException(
            404,
            "Campaign not found.",
        )

    return row


# --------------------------------------------------
# Request Models
# --------------------------------------------------


class LaunchRequest(BaseModel):

    confirmed: StrictBool

    expected_recipients: int = Field(
        ge=1,
        le=PILOT_MAX,
    )


class ScheduleRequest(LaunchRequest):
    scheduled_at: datetime


class ConfirmRequest(BaseModel):

    confirmed: StrictBool


# --------------------------------------------------
# Launch Campaign
# --------------------------------------------------


def prepare_campaign(
    campaign_id,
    payload,
    scheduled_at=None,
):

    # Existing pilot protection.

    if not enabled() or not allowed_numbers():
        raise HTTPException(403, "Pilot sending is disabled or allowlist missing.")

    if not payload.confirmed:
        raise HTTPException(422, "Confirm the saved audience.")

    permitted = ("draft", "scheduled") if scheduled_at else ("draft",)

    # Validate scheduling time.

    if scheduled_at:

        now = datetime.now(timezone.utc)

        if scheduled_at.tzinfo is None or not (
            now + timedelta(minutes=1) <= scheduled_at <= now + timedelta(days=7)
        ):
            raise HTTPException(
                422,
                "Schedule between 1 minute and 7 days "
                "in the future with a timezone.",
            )

    # Load and validate campaign configuration.

    with db_connect() as conn, conn.cursor() as cur:

        row = campaign_row(cur, campaign_id)

    if row["status"] not in permitted:
        raise HTTPException(
            409, "Campaign cannot be launched or scheduled " "in its current status."
        )

    validate_message(row)

    # Lock campaign and verify that its configuration
    # has not changed during validation.

    with db_connect() as conn, conn.cursor() as cur:

        current = campaign_row(cur, campaign_id, True)

        if (
            current["status"] != row["status"]
            or current["scheduled_at"] != row["scheduled_at"]
            or any(
                current[key] != row[key]
                for key in (
                    "template_snapshot",
                    "template_values",
                    "media_id",
                    "media_type",
                )
            )
        ):
            raise HTTPException(409, "Campaign changed. Review again.")

        # Re-check recipient eligibility.

        cur.execute(
            """
            SELECT
                c.phone,
                c.consent_status,

                EXISTS (
                    SELECT 1
                    FROM leads l
                    WHERE l.phone = c.phone
                      AND l.status = 'opted_out'
                ) AS blocked

            FROM campaign_recipients r

            JOIN contacts c
                ON c.id = r.contact_id

            WHERE r.campaign_id = %s
              AND r.send_status = 'pending'

        """,
            (campaign_id,),
        )

        recipients = cur.fetchall()

        if (
            len(recipients) != payload.expected_recipients
            or not recipients
            or len(recipients) > PILOT_MAX
        ):
            raise HTTPException(
                422, "Recipient count changed " "or exceeds pilot limit."
            )

        if any(
            r["phone"] not in allowed_numbers()
            or r["consent_status"] != "opted_in"
            or r["blocked"]
            for r in recipients
        ):
            raise HTTPException(
                403, "All pilot recipients must be " "authorized and opted in."
            )

        # Schedule campaign.

        if scheduled_at:

            cur.execute(
                """
                UPDATE marketing_campaigns

                SET status = 'scheduled',
                    scheduled_at = %s

                WHERE id = %s

            """,
                (
                    scheduled_at,
                    campaign_id,
                ),
            )

        # Start campaign immediately.

        else:

            cur.execute(
                """
                UPDATE marketing_campaigns

                SET status = 'running',
                    started_at = NOW(),
                    scheduled_at = NULL

                WHERE id = %s

            """,
                (campaign_id,),
            )

    return {
        "status": ("scheduled" if scheduled_at else "running"),
        "recipients": len(recipients),
        "scheduled_at": scheduled_at,
    }


# Send Now API


@router.post("/{campaign_id}/launch")
def launch(
    campaign_id: int,
    payload: LaunchRequest,
):

    return prepare_campaign(
        campaign_id,
        payload,
    )


# Schedule and Reschedule API


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


# Remove schedule and return to draft.


@router.post("/{campaign_id}/unschedule")
def unschedule(
    campaign_id: int,
    payload: ConfirmRequest,
):

    if not payload.confirmed:
        raise HTTPException(422, "Confirm removing the schedule.")

    with db_connect() as conn, conn.cursor() as cur:

        cur.execute(
            """
            UPDATE marketing_campaigns

            SET status = 'draft',
                scheduled_at = NULL

            WHERE id = %s
              AND status = 'scheduled'

            RETURNING id

        """,
            (campaign_id,),
        )

        if not cur.fetchone():

            raise HTTPException(409, "Only scheduled campaigns " "can be unscheduled.")

    return {"status": "draft"}


# --------------------------------------------------
# Pause Campaign
# --------------------------------------------------


@router.post("/{campaign_id}/pause")
def pause(campaign_id: int):

    with db_connect() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """

                UPDATE marketing_campaigns

                SET status = 'paused'

                WHERE id = %s

                AND status = 'running'

                RETURNING id

            """,
                (campaign_id,),
            )

            if not cur.fetchone():

                raise HTTPException(
                    409,
                    "Only running campaigns can pause.",
                )

    return {
        "status": "paused",
    }


# --------------------------------------------------
# Resume Campaign
# --------------------------------------------------


@router.post("/{campaign_id}/resume")
def resume(
    campaign_id: int,
    payload: ConfirmRequest,
):

    if not enabled() or not payload.confirmed:

        raise HTTPException(
            403,
            "Pilot disabled or confirmation missing.",
        )

    with db_connect() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """

                UPDATE marketing_campaigns

                SET status = 'running'

                WHERE id = %s

                AND status = 'paused'

                RETURNING id

            """,
                (campaign_id,),
            )

            if not cur.fetchone():

                raise HTTPException(
                    409,
                    "Only paused campaigns can resume.",
                )

    return {
        "status": "running",
    }


# --------------------------------------------------
# Cancel Campaign
# --------------------------------------------------


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

    with db_connect() as conn:

        with conn.cursor() as cur:

            cur.execute(
                """

                UPDATE marketing_campaigns

                SET

                    status = 'cancelled',

                    finished_at = NOW()

                WHERE id = %s

                AND status IN (

                    'draft',

                    'scheduled',

                    'running',

                    'paused'

                )

                RETURNING id

            """,
                (campaign_id,),
            )

            if not cur.fetchone():

                raise HTTPException(
                    409,
                    "Campaign cannot be cancelled.",
                )

            # Cancel recipients not yet processed.

            cur.execute(
                """

                UPDATE campaign_recipients

                SET send_status = 'cancelled'

                WHERE campaign_id = %s

                AND send_status = 'pending'

            """,
                (campaign_id,),
            )

    return {
        "status": "cancelled",
    }


# --------------------------------------------------
# Campaign Progress
# --------------------------------------------------


@router.get("/{campaign_id}/progress")
def progress(campaign_id: int):

    with db_connect() as conn, conn.cursor() as cur:

        row = campaign_row(cur, campaign_id)

        cur.execute(
            """
            SELECT
                send_status,
                COUNT(*) AS count

            FROM campaign_recipients

            WHERE campaign_id = %s

            GROUP BY send_status

        """,
            (campaign_id,),
        )

        counts = {r["send_status"]: r["count"] for r in cur.fetchall()}

    return {
        "status": row["status"],
        "total": sum(counts.values()),
        "counts": counts,
        "scheduled_at": row["scheduled_at"],
        "pilot_enabled": (enabled() and bool(allowed_numbers())),
    }


# --------------------------------------------------
# Complete Campaign
# --------------------------------------------------


def finish(cur, campaign_id):

    cur.execute(
        """

        UPDATE marketing_campaigns

        SET

            status = 'completed',

            finished_at = NOW()

        WHERE id = %s

        AND status = 'running'

        AND NOT EXISTS (

            SELECT 1

            FROM campaign_recipients

            WHERE campaign_id = %s

            AND send_status IN (

                'pending',

                'sending'

            )

        )

    """,
        (
            campaign_id,
            campaign_id,
        ),
    )


# --------------------------------------------------
# Process One Campaign Recipient
# --------------------------------------------------


def process_campaign_once():

    if not enabled():

        return

    # Global PostgreSQL lock.
    # Prevent multiple workers from sending
    # campaign messages simultaneously.

    with db_connect() as lock_conn:

        with lock_conn.cursor() as guard:

            guard.execute("""

                SELECT pg_try_advisory_xact_lock(
                    845167241
                ) AS locked

            """)

            if not guard.fetchone()["locked"]:

                return

            # --------------------------------------------------
            # Recover Interrupted Jobs
            # --------------------------------------------------

            with db_connect() as conn:

                with conn.cursor() as cur:

                    cur.execute("""

                        UPDATE campaign_recipients

                        SET

                            send_status = 'unknown',

                            error = 'Worker interrupted'

                        WHERE send_status = 'sending'

                        RETURNING campaign_id

                    """)

                    affected = {row["campaign_id"] for row in cur.fetchall()}

                    for campaign_id in affected:

                        cur.execute(
                            """

                            UPDATE marketing_campaigns

                            SET status = 'paused'

                            WHERE id = %s

                            AND status = 'running'

                        """,
                            (campaign_id,),
                        )

                        # Global minimum send interval.

                        # Return missed schedules to draft after excessive downtime.

                        cur.execute("""
                            UPDATE marketing_campaigns

                            SET status = 'draft',
                            scheduled_at = NULL

                            WHERE status = 'scheduled'

                            AND scheduled_at
                            < NOW() - INTERVAL '15 minutes'
                        """)

                        # Start campaigns whose scheduled time has arrived.

                        cur.execute("""
                            UPDATE marketing_campaigns

                            SET status = 'running',
                            started_at = NOW()

                            WHERE status = 'scheduled'

                            AND scheduled_at <= NOW()
                        """)

                    cur.execute("""

                        SELECT 1

                        FROM campaign_recipients

                        WHERE attempted_at >

                            NOW() - INTERVAL '5 seconds'

                        LIMIT 1

                    """)

                    if cur.fetchone():

                        return

                    # Find an active campaign.

                    cur.execute("""

                        SELECT id

                        FROM marketing_campaigns

                        WHERE status = 'running'

                        ORDER BY id

                        LIMIT 1

                    """)

                    picked = cur.fetchone()

                    if not picked:

                        return

                    cid = picked["id"]

                    row = campaign_row(
                        cur,
                        cid,
                    )

                    # Get next pending recipient.

                    cur.execute(
                        """

                        SELECT

                            r.contact_id,

                            c.phone

                        FROM campaign_recipients r

                        JOIN contacts c

                            ON c.id = r.contact_id

                        WHERE r.campaign_id = %s

                        AND r.send_status = 'pending'

                        ORDER BY r.contact_id

                        LIMIT 1

                    """,
                        (cid,),
                    )

                    recipient = cur.fetchone()

                    if not recipient:

                        finish(cur, cid)

                        return

            # --------------------------------------------------
            # Validate Campaign Message
            # --------------------------------------------------

            try:

                message = validate_message(row)

            except HTTPException:

                with db_connect() as conn:

                    with conn.cursor() as cur:

                        cur.execute(
                            """

                            UPDATE marketing_campaigns

                            SET status = 'paused'

                            WHERE id = %s

                            AND status = 'running'

                        """,
                            (cid,),
                        )

                return

            phone = recipient["phone"]

            rid = recipient["contact_id"]

            # --------------------------------------------------
            # Lock Individual Recipient
            # --------------------------------------------------

            with customer_send_guard(phone):

                with db_connect() as conn:

                    with conn.cursor() as cur:

                        current = campaign_row(
                            cur,
                            cid,
                            True,
                        )

                        if not enabled() or current["status"] != "running":

                            return

                        # Recheck latest contact consent.

                        cur.execute(
                            """

                            SELECT

                                c.phone,

                                c.consent_status,

                                EXISTS (

                                    SELECT 1

                                    FROM leads l

                                    WHERE l.phone = c.phone

                                    AND l.status = 'opted_out'

                                ) AS blocked

                            FROM contacts c

                            WHERE c.id = %s

                        """,
                            (rid,),
                        )

                        contact = cur.fetchone()

                        # Exclude opted-out or
                        # unauthorized recipients.

                        if (
                            not contact
                            or contact["phone"] != phone
                            or contact["consent_status"] != "opted_in"
                            or contact["blocked"]
                            or phone not in allowed_numbers()
                            or (phone.startswith("120255501") and len(phone) == 11)
                        ):

                            cur.execute(
                                """

                                UPDATE campaign_recipients

                                SET

                                    send_status = 'skipped',

                                    error =
                                        'Recipient no longer eligible'

                                WHERE campaign_id = %s

                                AND contact_id = %s

                                AND send_status = 'pending'

                            """,
                                (cid, rid),
                            )

                            finish(cur, cid)

                            return

                        # Persist send state BEFORE
                        # contacting the external API.

                        cur.execute(
                            """

                            UPDATE campaign_recipients

                            SET

                                send_status = 'sending',

                                attempted_at = NOW()

                            WHERE campaign_id = %s

                            AND contact_id = %s

                            AND send_status = 'pending'

                            RETURNING contact_id

                        """,
                            (cid, rid),
                        )

                        if not cur.fetchone():

                            return

                # --------------------------------------------------
                # Send WhatsApp Message
                # --------------------------------------------------

                status = "unknown"

                wamid = None

                error = "Uncertain send outcome"

                try:

                    result = send_whatsapp_payload(
                        {
                            "messaging_product": "whatsapp",
                            "to": phone,
                            "type": "template",
                            "template": message,
                        }
                    )

                    wamid = result["messages"][0]["id"]

                    status = "accepted"

                    error = None

                except requests.HTTPError as exc:

                    if (
                        exc.response is not None
                        and 400 <= exc.response.status_code < 500
                    ):

                        status = "rejected"

                        error = f"Meta HTTP " f"{exc.response.status_code}"

                except Exception:

                    logger.exception("Campaign request outcome uncertain")

                # --------------------------------------------------
                # Save Send Outcome
                # --------------------------------------------------

                with db_connect() as conn:

                    with conn.cursor() as cur:

                        campaign_row(
                            cur,
                            cid,
                            True,
                        )

                        cur.execute(
                            """

                            UPDATE campaign_recipients

                            SET

                                send_status = %s,

                                wamid = %s,

                                error = %s

                            WHERE campaign_id = %s

                            AND contact_id = %s

                            AND send_status = 'sending'

                        """,
                            (
                                status,
                                wamid,
                                error,
                                cid,
                                rid,
                            ),
                        )

                        # Pause campaign on errors.
                        # Do not keep sending blindly.

                        if status != "accepted":

                            cur.execute(
                                """

                                UPDATE marketing_campaigns

                                SET status = 'paused'

                                WHERE id = %s

                                AND status = 'running'

                            """,
                                (cid,),
                            )

                        # Complete if no pending jobs remain.

                        finish(cur, cid)


# --------------------------------------------------
# Background Campaign Worker
# --------------------------------------------------


async def campaign_worker_loop():

    while True:

        try:

            await asyncio.to_thread(process_campaign_once)

        except asyncio.CancelledError:

            raise

        except Exception:

            logger.exception("Campaign queue error")

        await asyncio.sleep(2)
