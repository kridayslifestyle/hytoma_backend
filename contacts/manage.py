import csv
import io

from fastapi import HTTPException, Query
from fastapi.responses import StreamingResponse

from pydantic import BaseModel, Field, StrictBool

from db import db_connect

from contacts.api import (
    router,
    filter_contacts,
    BulkTagRequest
)


MAX_SELECTION = 5000


class Selection(BaseModel):

    contact_ids: list[int] = Field(
        min_length=1,
        max_length=MAX_SELECTION
    )

    confirmed: StrictBool = False


def ids_for(payload):

    ids = sorted(set(payload.contact_ids))

    if any(i <= 0 for i in ids):
        raise HTTPException(422, "Invalid contact ID.")

    return ids


# =====================================================
# SELECT ALL MATCHING CONTACTS
# =====================================================

@router.get("/matching-ids")
def matching_ids(
    search: str = Query("", max_length=150),
    consent_status: str = "all",
    contact_type: str = "all",
    tag: str = Query("", max_length=1620),
    tag_mode: str = "all",
    archived: bool = False,
):

    where, params = filter_contacts(
        search,
        consent_status,
        contact_type,
        tag,
        tag_mode,
        archived
    )

    with db_connect() as conn, conn.cursor() as cur:

        cur.execute(
            "SELECT id FROM contacts" + where +
            " ORDER BY id LIMIT %s",
            (*params, MAX_SELECTION + 1)
        )

        ids = [
            r["id"]
            for r in cur.fetchall()
        ]

    if len(ids) > MAX_SELECTION:

        raise HTTPException(
            422,
            "More than 5,000 matches. "
            "Narrow your filters before selecting all."
        )

    return {
        "contact_ids": ids
    }


# =====================================================
# BULK REMOVE TAG
# =====================================================

@router.post("/bulk-remove-tag")
def remove_tag(payload: BulkTagRequest):

    ids = ids_for(payload)

    tag = payload.tag.strip()

    if not tag or "," in tag:

        raise HTTPException(
            422, "Enter one tag."
        )

    with db_connect() as conn, conn.cursor() as cur:

        cur.execute("""
            UPDATE contacts

            SET tags = ARRAY(

                SELECT t
                FROM unnest(tags) AS t
                WHERE lower(t) <> lower(%s)

            ),

            updated_at = NOW()

            WHERE id = ANY(%s)

              AND archived_at IS NULL

              AND EXISTS (

                  SELECT 1
                  FROM unnest(tags) AS t

                  WHERE lower(t) = lower(%s)
              )

            RETURNING id

        """, (tag, ids, tag))

        updated = len(cur.fetchall())

    return {
        "updated": updated,
        "skipped": len(ids) - updated
    }


# =====================================================
# ARCHIVE / RESTORE CONTACTS
# =====================================================

@router.post("/bulk-archive")
def archive(payload: Selection):

    return archive_change(payload, True)


@router.post("/bulk-restore")
def restore(payload: Selection):

    return archive_change(payload, False)


def archive_change(payload, archiving):

    if not payload.confirmed:

        raise HTTPException(
            422, "Confirmation required."
        )

    ids = ids_for(payload)

    with db_connect() as conn, conn.cursor() as cur:

        # Coordinate with the existing sending operations.

        cur.execute("""
            SELECT phone

            FROM contacts

            WHERE id = ANY(%s)

            ORDER BY phone

        """, (ids,))

        phones = [
            r["phone"]
            for r in cur.fetchall()
        ]

        for phone in phones:

            cur.execute("""
                SELECT pg_advisory_xact_lock(
                    hashtextextended(%s, 48126)
                )
            """, (phone,))

        if archiving:

            sql = """
                UPDATE contacts

                SET archived_at = NOW(),
                    updated_at = NOW()

                WHERE id = ANY(%s)
                  AND archived_at IS NULL
            """

        else:

            sql = """
                UPDATE contacts

                SET archived_at = NULL,
                    updated_at = NOW()

                WHERE id = ANY(%s)
                  AND archived_at IS NOT NULL
            """

        cur.execute(
            sql + " RETURNING id",
            (ids,)
        )

        updated = len(cur.fetchall())

    return {
        "updated": updated,
        "skipped": len(ids) - updated
    }


# =====================================================
# CSV EXPORT
# =====================================================

def safe_cell(value):

    value = str(
        value if value is not None else ""
    )

    # Prevent spreadsheet formula injection.

    if value.lstrip().startswith(
        ("=", "+", "-", "@", "\t", "\r", "\n")
    ):

        return "'" + value

    return value


@router.get("/export.csv")
def export_contacts(
    search: str = Query("", max_length=150),
    consent_status: str = "all",
    contact_type: str = "all",
    tag: str = Query("", max_length=1620),
    tag_mode: str = "all",
    archived: bool = False,
):

    where, params = filter_contacts(
        search,
        consent_status,
        contact_type,
        tag,
        tag_mode,
        archived
    )

    def stream():

        buffer = io.StringIO()

        writer = csv.writer(buffer)

        def line(values):

            writer.writerow([
                safe_cell(v)
                for v in values
            ])

            data = buffer.getvalue()

            buffer.seek(0)
            buffer.truncate(0)

            return data

        yield "\ufeff" + line([
            "phone",
            "name",
            "location",
            "contact_type",
            "tags",
            "source",
            "consent_status",
            "archived_at",
        ])

        with db_connect() as conn:

            with conn.cursor(
                name="contact_export"
            ) as cur:

                cur.execute(
                    """
                    SELECT
                        phone,
                        name,
                        location,
                        contact_type,
                        tags,
                        source,
                        consent_status,
                        archived_at

                    FROM contacts
                    """ + where + " ORDER BY id",

                    params
                )

                for r in cur:

                    yield line([
                        r["phone"],
                        r["name"],
                        r["location"],
                        r["contact_type"],
                        ", ".join(r["tags"]),
                        r["source"],
                        r["consent_status"],
                        r["archived_at"],
                    ])

    return StreamingResponse(

        stream(),

        media_type="text/csv; charset=utf-8",

        headers={
            "Content-Disposition":
                'attachment; filename="hytoma_contacts.csv"',

            "Cache-Control": "private, no-store"
        }
    )


# =====================================================
# INDIVIDUAL CONTACT CAMPAIGN HISTORY
# =====================================================

@router.get("/{contact_id}/campaign-history")
def history(
    contact_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):

    with db_connect() as conn, conn.cursor() as cur:

        cur.execute("""
            SELECT
                id,
                name,
                phone,
                archived_at

            FROM contacts

            WHERE id = %s

        """, (contact_id,))

        contact = cur.fetchone()

        if not contact:

            raise HTTPException(
                404, "Contact not found."
            )

        cur.execute("""
            SELECT

                c.id AS campaign_id,

                c.name,

                c.template_name,

                c.status AS campaign_status,

                r.attempted_at,

                r.send_status,

                COALESCE(
                    s.delivery_status,
                    r.send_status
                ) AS status,

                s.error_code,

                r.error,

                (
                    SELECT COUNT(*)

                    FROM marketing_replies m

                    WHERE m.contact_id = r.contact_id
                      AND m.campaign_id = r.campaign_id

                ) AS replies

            FROM campaign_recipients r

            JOIN marketing_campaigns c
                ON c.id = r.campaign_id

            LEFT JOIN whatsapp_message_statuses s
                ON s.whatsapp_message_id = r.wamid

            WHERE r.contact_id = %s

            ORDER BY c.created_at DESC,
                     c.id DESC

            LIMIT %s OFFSET %s

        """, (
            contact_id,
            limit + 1,
            offset
        ))

        rows = cur.fetchall()

    return {

        "contact": contact,

        "campaigns": rows[:limit],

        "has_more": len(rows) > limit
    }