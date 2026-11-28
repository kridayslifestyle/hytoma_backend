import logging

import requests

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
)

from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb

from auth import require_admin
from db import db_connect
from contacts.api import ContactType
from marketing.templates import get_marketing_templates


logger = logging.getLogger("hytoma.campaigns")


# --------------------------------------------------
# Campaign Router
# --------------------------------------------------

router = APIRouter(
    prefix="/admin/campaigns",
    tags=["Campaign Manager"],
    dependencies=[Depends(require_admin)],
)


MAX_RECIPIENTS = 5000


# --------------------------------------------------
# Database Schema
# --------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS marketing_campaigns (

    id BIGSERIAL PRIMARY KEY,

    name TEXT NOT NULL,

    template_id TEXT NOT NULL,

    template_name TEXT NOT NULL,

    template_language TEXT NOT NULL,

    template_snapshot JSONB NOT NULL,

    audience_filters JSONB NOT NULL,

    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft')),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()

);


CREATE TABLE IF NOT EXISTS campaign_recipients (

    campaign_id BIGINT NOT NULL
        REFERENCES marketing_campaigns(id),

    contact_id BIGINT NOT NULL
        REFERENCES contacts(id),

    PRIMARY KEY (campaign_id, contact_id)

);
"""


# --------------------------------------------------
# Initialize Campaign Tables
# --------------------------------------------------

def initialize_campaigns():

    with db_connect() as conn:

        with conn.cursor() as cur:

            cur.execute(
                SCHEMA,
                prepare=False,
            )


# --------------------------------------------------
# Request Models
# --------------------------------------------------

class AudienceFilters(BaseModel):

    contact_types: list[ContactType] = Field(
        default_factory=list,
        max_length=6,
    )

    location: str = Field(
        default="",
        max_length=150,
    )

    tag: str = Field(
        default="",
        max_length=80,
    )


class CampaignCreate(BaseModel):

    name: str = Field(
        min_length=1,
        max_length=150,
    )

    template_id: str = Field(
        min_length=1,
        max_length=100,
    )

    audience: AudienceFilters


# --------------------------------------------------
# Approved Marketing Templates
# --------------------------------------------------

def eligible_templates():

    try:

        templates = get_marketing_templates()

    except (
        requests.RequestException,
        RuntimeError,
    ):

        logger.exception(
            "Template lookup failed"
        )

        raise HTTPException(
            status_code=502,
            detail="Unable to retrieve Meta templates.",
        )

    return [

        template

        for template in templates

        if template.get("category") == "MARKETING"

        and template.get("status") == "APPROVED"

    ]


# --------------------------------------------------
# Audience Selection
# --------------------------------------------------

def audience_rows(cur, filters):

    types = list(
        set(filters.contact_types)
    )

    location = filters.location.strip()

    tag = filters.tag.strip()

    cur.execute("""

        SELECT

            c.id,

            c.name,

            c.phone,

            c.contact_type,

            c.location

        FROM contacts c

        WHERE c.consent_status = 'opted_in'
        AND c.archived_at IS NULL

        AND c.phone !~ '^120255501[0-9]{2}$'

        AND NOT EXISTS (

            SELECT 1

            FROM leads l

            WHERE l.phone = c.phone

            AND l.status = 'opted_out'

        )

        AND (
            %s
            OR c.contact_type = ANY(%s::text[])
        )

        AND (
            %s = ''
            OR strpos(
                lower(c.location),
                lower(%s)
            ) > 0
        )

        AND (
            %s = ''

            OR EXISTS (

                SELECT 1

                FROM unnest(c.tags) AS t(value)

                WHERE lower(t.value) = lower(%s)

            )
        )

        ORDER BY c.id

        LIMIT %s

    """, (

        not types,

        types,

        location,

        location,

        tag,

        tag,

        MAX_RECIPIENTS + 1,

    ))

    return cur.fetchall()


# --------------------------------------------------
# GET Approved Marketing Templates
# --------------------------------------------------

@router.get("/templates")
def list_campaign_templates():

    templates = eligible_templates()

    return {

        "templates": templates,

    }


# --------------------------------------------------
# POST Audience Preview
# --------------------------------------------------

@router.post("/audience/preview")
def preview_audience(
    payload: AudienceFilters,
):

    with db_connect() as conn:

        with conn.cursor() as cur:

            rows = audience_rows(
                cur,
                payload,
            )

    return {

        "eligible": min(
            len(rows),
            MAX_RECIPIENTS,
        ),

        "over_limit": (
            len(rows) > MAX_RECIPIENTS
        ),

        "max_recipients": MAX_RECIPIENTS,

        "sample": rows[:20],

    }


# --------------------------------------------------
# POST Create Campaign Draft
# --------------------------------------------------

@router.post("", status_code=201)
def create_campaign(
    payload: CampaignCreate,
):

    name = payload.name.strip()

    if not name:

        raise HTTPException(
            status_code=422,
            detail="Campaign name is required.",
        )

    # Verify selected template with Meta.

    template = next(

        (
            t
            for t in eligible_templates()

            if str(t.get("id")) == payload.template_id
        ),

        None,

    )

    if not template:

        raise HTTPException(
            status_code=422,
            detail="Select an approved marketing template.",
        )

    with db_connect() as conn:

        with conn.cursor() as cur:

            # Recalculate eligible audience.

            rows = audience_rows(
                cur,
                payload.audience,
            )

            if not rows:

                raise HTTPException(
                    status_code=422,
                    detail="No eligible contacts.",
                )

            if len(rows) > MAX_RECIPIENTS:

                raise HTTPException(
                    status_code=422,
                    detail="Narrow the audience to 5000 contacts.",
                )

            # Save campaign draft.

            cur.execute("""

                INSERT INTO marketing_campaigns (

                    name,

                    template_id,

                    template_name,

                    template_language,

                    template_snapshot,

                    audience_filters

                )

                VALUES (
                    %s, %s, %s, %s, %s, %s
                )

                RETURNING

                    id,

                    name,

                    template_name,

                    template_language,

                    status,

                    created_at

            """, (

                name,

                payload.template_id,

                template["name"],

                template["language"],

                Jsonb(template),

                Jsonb(
                    payload.audience.model_dump()
                ),

            ))

            campaign = cur.fetchone()

            # Save campaign recipients.

            cur.executemany("""

                INSERT INTO campaign_recipients (

                    campaign_id,

                    contact_id

                )

                VALUES (%s, %s)

            """, [

                (
                    campaign["id"],
                    row["id"],
                )

                for row in rows

            ])

    return {

        "campaign": campaign,

        "recipients": len(rows),

    }


# --------------------------------------------------
# GET All Campaigns
# --------------------------------------------------

@router.get("")
def list_campaigns(

    limit: int = Query(
        default=50,
        ge=1,
        le=200,
    ),

    offset: int = Query(
        default=0,
        ge=0,
    ),

):

    with db_connect() as conn:

        with conn.cursor() as cur:

            cur.execute("""

                SELECT

                    c.id,

                    c.name,

                    c.template_name,

                    c.template_language,

                    c.status,

                    c.created_at,

                    (
                        SELECT COUNT(*)

                        FROM campaign_recipients r

                        WHERE r.campaign_id = c.id

                    ) AS recipients

                FROM marketing_campaigns c

                ORDER BY c.id DESC

                LIMIT %s OFFSET %s

            """, (

                limit,

                offset,

            ))

            return {

                "campaigns": cur.fetchall(),

            }


# --------------------------------------------------
# GET Campaign Details
# --------------------------------------------------

@router.get("/{campaign_id}")
def campaign_details(campaign_id: int):

    with db_connect() as conn:

        with conn.cursor() as cur:

            cur.execute("""

                SELECT *

                FROM marketing_campaigns

                WHERE id = %s

            """, (campaign_id,))

            campaign = cur.fetchone()

            if not campaign:

                raise HTTPException(
                    status_code=404,
                    detail="Campaign not found.",
                )

            cur.execute("""

                SELECT COUNT(*) AS recipients

                FROM campaign_recipients

                WHERE campaign_id = %s

            """, (campaign_id,))

            result = cur.fetchone()

            return {

                "campaign": campaign,

                **result,

            }

def lock_unused_draft(cur, campaign_id):
    cur.execute(
        """
        SELECT * FROM marketing_campaigns
        WHERE id=%s FOR UPDATE
        """,
        (campaign_id,),
    )

    row = cur.fetchone()

    if not row:
        raise HTTPException(404, "Campaign not found.")

    if row["status"] != "draft":
        raise HTTPException(
            409,
            "Only unsent drafts can be edited or deleted.",
        )

    cur.execute("""
        SELECT EXISTS (
            SELECT 1
            FROM campaign_recipients
            WHERE campaign_id=%s
            AND (
                send_status <> 'pending'
                OR attempted_at IS NOT NULL
                OR wamid IS NOT NULL
            )
        ) OR EXISTS (
            SELECT 1
            FROM campaign_test_sends
            WHERE campaign_id=%s
        ) AS used
    """, (campaign_id, campaign_id))

    if cur.fetchone()["used"]:
        raise HTTPException(
            409,
            "This campaign has send history. Duplicate it instead.",
        )

    return row


def clear_archive(cur, campaign_id):
    cur.execute("""
        SELECT to_regclass(
            'campaign_media_archive'
        ) AS archive
    """)

    if cur.fetchone()["archive"]:
        cur.execute("""
            DELETE FROM campaign_media_archive
            WHERE campaign_id=%s
        """, (campaign_id,))


@router.patch("/{campaign_id}")
def edit_campaign(
    campaign_id: int,
    payload: CampaignCreate,
):
    name = payload.name.strip()

    if not name:
        raise HTTPException(
            422, "Campaign name is required."
        )

    template = next(
        (
            t for t in eligible_templates()
            if str(t.get("id")) == payload.template_id
        ),
        None,
    )

    if not template:
        raise HTTPException(
            422,
            "Select an approved marketing template.",
        )

    with db_connect() as conn, conn.cursor() as cur:

        row = lock_unused_draft(cur, campaign_id)

        rows = audience_rows(cur, payload.audience)

        if not rows or len(rows) > MAX_RECIPIENTS:
            raise HTTPException(
                422,
                "Select between 1 and 5000 eligible contacts.",
            )

        changed = (
            row["template_id"] != payload.template_id
            or row["template_snapshot"] != template
        )

        if changed:
            clear_archive(cur, campaign_id)

            cur.execute("""
                UPDATE marketing_campaigns
                SET media_id=NULL,
                    media_type=NULL,
                    media_filename=NULL,
                    media_uploaded_at=NULL,
                    template_values='{}'
                WHERE id=%s
            """, (campaign_id,))

        cur.execute("""
            UPDATE marketing_campaigns
            SET name=%s,
                template_id=%s,
                template_name=%s,
                template_language=%s,
                template_snapshot=%s,
                audience_filters=%s
            WHERE id=%s
            RETURNING id, name, status
        """, (
            name,
            payload.template_id,
            template["name"],
            template["language"],
            Jsonb(template),
            Jsonb(payload.audience.model_dump()),
            campaign_id,
        ))

        campaign = cur.fetchone()

        cur.execute("""
            DELETE FROM campaign_recipients
            WHERE campaign_id=%s
        """, (campaign_id,))

        cur.executemany("""
            INSERT INTO campaign_recipients (
                campaign_id,
                contact_id
            )
            VALUES (%s, %s)
        """, [
            (campaign_id, r["id"])
            for r in rows
        ])

    return {
        "campaign": campaign,
        "recipients": len(rows),
        "configuration_reset": changed,
    }


@router.delete("/{campaign_id}")
def delete_campaign(campaign_id: int):

    with db_connect() as conn, conn.cursor() as cur:

        lock_unused_draft(cur, campaign_id)

        clear_archive(cur, campaign_id)

        cur.execute("""
            DELETE FROM campaign_recipients
            WHERE campaign_id=%s
        """, (campaign_id,))

        cur.execute("""
            DELETE FROM marketing_campaigns
            WHERE id=%s
        """, (campaign_id,))

    return {"deleted": True}