import re
from typing import Literal
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
)

from pydantic import BaseModel, Field

from auth import require_admin
from db import db_connect

router = APIRouter(
    prefix="/admin/contacts",
    tags=["Contacts Manager"],
    dependencies=[Depends(require_admin)],
)


# ============================================================
# 1. DATABASE SCHEMA
# ============================================================

CONTACTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id BIGSERIAL PRIMARY KEY,

    phone TEXT NOT NULL UNIQUE,

    name VARCHAR(150) NOT NULL DEFAULT '',

    location VARCHAR(255) NOT NULL DEFAULT '',

    source TEXT NOT NULL
        CHECK (source IN ('manual', 'crm', 'csv')),

    tags TEXT[] NOT NULL DEFAULT '{}',

    consent_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (
            consent_status IN (
                'pending',
                'opted_in',
                'opted_out'
            )
        ),

    consent_at TIMESTAMPTZ,

    consent_source TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()

    ALTER TABLE contacts
        ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;

    CREATE INDEX IF NOT EXISTS contacts_active_idx
    ON contacts(id)
    WHERE archived_at IS NULL;
);

ALTER TABLE contacts
ADD COLUMN IF NOT EXISTS contact_type TEXT
NOT NULL DEFAULT 'unclassified'
CHECK (
    contact_type IN (
        'unclassified',
        'customer',
        'interior_designer',
        'builder',
        'architect',
        'other'
    )
);

CREATE INDEX IF NOT EXISTS contacts_type_idx
ON contacts(contact_type);


CREATE INDEX IF NOT EXISTS contacts_consent_idx
ON contacts(consent_status);


CREATE TABLE IF NOT EXISTS contact_consent_events (
    id BIGSERIAL PRIMARY KEY,

    contact_id BIGINT NOT NULL
        REFERENCES contacts(id),

    status TEXT NOT NULL
        CHECK (status IN ('opted_in', 'opted_out')),

    source TEXT NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def initialize_contacts():

    with db_connect() as conn:

        with conn.cursor() as cur:

            cur.execute(CONTACTS_SCHEMA, prepare=False)


# ============================================================
# 2. PHONE NUMBER NORMALIZATION
# ============================================================


def normalize_phone(value: str) -> str:

    phone = re.sub(r"[\s()\-]", "", value.strip())

    if phone.startswith("+"):
        phone = phone[1:]

    elif phone.startswith("00"):
        phone = phone[2:]

    elif re.fullmatch(r"0[6-9][0-9]{9}", phone):
        phone = "91" + phone[1:]

    elif re.fullmatch(r"[6-9][0-9]{9}", phone):
        phone = "91" + phone

    if not re.fullmatch(r"[1-9][0-9]{7,14}", phone):

        raise ValueError("Enter a valid phone number with country code.")

    return phone


# ============================================================
# 3. REQUEST MODELS
# ============================================================
ContactType = Literal[
    "unclassified",
    "customer",
    "interior_designer",
    "builder",
    "architect",
    "other",
]


class ContactCreate(BaseModel):
    phone: str = Field(min_length=1, max_length=40)
    name: str = Field(default="", max_length=150)
    location: str = Field(default="", max_length=255)
    tags: list[str] = Field(default_factory=list, max_length=20)
    contact_type: ContactType = "unclassified"


class ContactUpdate(BaseModel):
    name: str = Field(default="", max_length=150)
    location: str = Field(default="", max_length=255)
    tags: list[str] = Field(default_factory=list, max_length=20)
    contact_type: ContactType | None = None


# ============================================================
# 4. NORMALIZE TAGS
# ============================================================


def clean_tags(tags):

    cleaned = list(dict.fromkeys(t.strip() for t in tags if t.strip()))

    if any(len(t) > 80 for t in cleaned):

        raise HTTPException(
            status_code=422, detail="Tags must not exceed 80 characters."
        )

    return cleaned


def filter_contacts(
    search="",
    consent_status="all",
    contact_type="all",
    tag="",
    tag_mode="all",
    archived=False,
):

    if consent_status not in ("all", "pending", "opted_in", "opted_out"):
        raise HTTPException(422, "Invalid permission filter.")

    if contact_type not in (
        "all",
        "unclassified",
        "customer",
        "interior_designer",
        "builder",
        "architect",
        "other",
    ):
        raise HTTPException(422, "Invalid contact type.")

    if tag_mode not in ("all", "any"):
        raise HTTPException(422, "Invalid tag match mode.")

    tags = list(dict.fromkeys(t.strip().lower() for t in tag.split(",") if t.strip()))

    if len(tags) > 20 or any(len(t) > 80 for t in tags):
        raise HTTPException(422, "Invalid tags.")

    term = "%" + search.strip() + "%"

    where = """
        WHERE (
            name ILIKE %s
            OR phone ILIKE %s
            OR location ILIKE %s
        )

        AND (%s = 'all' OR consent_status = %s)

        AND (%s = 'all' OR contact_type = %s)

        AND (
            (%s AND archived_at IS NOT NULL)
            OR
            (NOT %s AND archived_at IS NULL)
        )
    """

    params = [
        term,
        term,
        term,
        consent_status,
        consent_status,
        contact_type,
        contact_type,
        archived,
        archived,
    ]

    if tags:

        operator = "@>" if tag_mode == "all" else "&&"

        where += (
            " AND ARRAY("
            "SELECT lower(t) FROM unnest(tags) AS t"
            ") " + operator + " %s::text[]"
        )

        params.append(tags)

    return where, params


# ============================================================
# 5. GET CONTACTS AND STATISTICS
# ============================================================


@router.get("")
def list_contacts(
    search: str = Query("", max_length=150),
    consent_status: str = "all",
    contact_type: str = "all",
    tag: str = Query("", max_length=1620),
    tag_mode: str = "all",
    archived: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):

    where, params = filter_contacts(
        search,
        consent_status,
        contact_type,
        tag,
        tag_mode,
        archived,
    )

    with db_connect() as conn, conn.cursor() as cur:

        cur.execute("""
            SELECT

                COUNT(*) AS total,

                COUNT(*) FILTER (
                    WHERE consent_status = 'pending'
                ) AS pending,

                COUNT(*) FILTER (
                    WHERE consent_status = 'opted_in'
                ) AS opted_in,

                COUNT(*) FILTER (
                    WHERE consent_status = 'opted_out'
                ) AS opted_out,

                COUNT(*) FILTER (
                    WHERE contact_type = 'customer'
                ) AS customers,

                COUNT(*) FILTER (
                    WHERE contact_type = 'interior_designer'
                ) AS interior_designers,

                COUNT(*) FILTER (
                    WHERE contact_type = 'builder'
                ) AS builders

            FROM contacts

            WHERE archived_at IS NULL
        """)

        stats = cur.fetchone()

        cur.execute("SELECT COUNT(*) AS total FROM contacts" + where, params)

        total = cur.fetchone()["total"]

        cur.execute(
            "SELECT * FROM contacts" + where + """
                ORDER BY created_at DESC, id DESC
                LIMIT %s OFFSET %s
            """,
            (*params, limit, offset),
        )

        return {"contacts": cur.fetchall(), "total": total, "stats": stats}


# ============================================================
# 6. CREATE CONTACT MANUALLY
# ============================================================


@router.post("", status_code=201)
def create_contact(payload: ContactCreate):

    try:
        phone = normalize_phone(payload.phone)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    tags = clean_tags(payload.tags)

    with db_connect() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                INSERT INTO contacts (
                    phone,
                    name,
                    location,
                    source,
                    tags,
                    contact_type
                )
                VALUES (%s, %s, %s, 'manual', %s, %s)
                ON CONFLICT (phone)
                DO NOTHING
                RETURNING *
            """,
                (
                    phone,
                    payload.name.strip(),
                    payload.location.strip(),
                    tags,
                    payload.contact_type,
                ),
            )

            contact = cur.fetchone()

            if not contact:
                raise HTTPException(
                    409,
                    "This contact already exists.",
                )

            return {"contact": contact}


# ============================================================
# 7. EDIT CONTACT DETAILS
# ============================================================


@router.patch("/{contact_id}")
def update_contact(
    contact_id: int,
    payload: ContactUpdate,
):
    tags = clean_tags(payload.tags)

    with db_connect() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE contacts
                SET
                    name = %s,
                    location = %s,
                    tags = %s,
                    contact_type = COALESCE(%s, contact_type),
                    updated_at = NOW()
                WHERE id=%s
                AND archived_at IS NULL
                RETURNING *
            """,
                (
                    payload.name.strip(),
                    payload.location.strip(),
                    tags,
                    payload.contact_type,
                    contact_id,
                ),
            )

            contact = cur.fetchone()

            if not contact:
                raise HTTPException(
                    404,
                    "Contact not found.",
                )

            return {"contact": contact}


# ============================================================
# 8. IMPORT EXISTING CRM LEADS
# ============================================================


@router.post("/sync-crm")
def sync_crm_contacts():

    added = 0
    existing = 0
    invalid = 0

    with db_connect() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    phone,
                    project_location,
                    status

                FROM leads
            """)

            leads = cur.fetchall()

            for lead in leads:

                try:

                    phone = normalize_phone(lead["phone"])

                except (ValueError, AttributeError):

                    invalid += 1
                    continue

                cur.execute(
                    """
                    INSERT INTO contacts (
                        phone,
                        location,
                        source
                    )

                    VALUES (
                        %s,
                        %s,
                        'crm'
                    )

                    ON CONFLICT (phone)
                    DO NOTHING

                    RETURNING id
                """,
                    (
                        phone,
                        (lead["project_location"] or "")[:255],
                    ),
                )

                if cur.fetchone():

                    added += 1

                else:

                    existing += 1

                # Preserve existing CRM opt-out requests

                if lead["status"] == "opted_out":

                    cur.execute(
                        """
                        UPDATE contacts

                        SET
                            consent_status = 'opted_out',
                            consent_at = NOW(),
                            consent_source = 'crm_opt_out',
                            updated_at = NOW()

                        WHERE phone = %s

                        AND consent_status <> 'opted_out'

                        RETURNING id
                    """,
                        (phone,),
                    )

                    changed = cur.fetchone()

                    if changed:

                        cur.execute(
                            """
                            INSERT INTO contact_consent_events (
                                contact_id,
                                status,
                                source
                            )

                            VALUES (
                                %s,
                                'opted_out',
                                'crm_opt_out'
                            )
                        """,
                            (changed["id"],),
                        )

    return {
        "added": added,
        "existing": existing,
        "invalid": invalid,
    }


class BulkContactType(BaseModel):
    contact_ids: list[int] = Field(
        min_length=1,
        max_length=5000,
    )
    contact_type: ContactType


@router.post("/bulk-type")
def bulk_contact_type(payload: BulkContactType):

    ids = list(set(payload.contact_ids))

    if any(i <= 0 for i in ids):
        raise HTTPException(
            422,
            "Invalid contact ID.",
        )

    with db_connect() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE contacts
                SET
                    contact_type = %s,
                    updated_at = NOW()
                WHERE id = ANY(%s)
                AND contact_type <> %s
                AND archived_at IS NULL
                RETURNING id
            """,
                (
                    payload.contact_type,
                    ids,
                    payload.contact_type,
                ),
            )

            updated = len(cur.fetchall())

    return {
        "updated": updated,
        "skipped": len(ids) - updated,
    }


class BulkTagRequest(BaseModel):

    contact_ids: list[int] = Field(
        min_length=1,
        max_length=5000,
    )

    tag: str = Field(
        min_length=1,
        max_length=80,
    )


@router.post("/bulk-tag")
def bulk_tag(payload: BulkTagRequest):

    ids = sorted(set(payload.contact_ids))

    tag = payload.tag.strip()

    if any(i <= 0 for i in ids) or not tag or "," in tag:
        raise HTTPException(
            422,
            "Enter one valid tag and select contacts.",
        )

    with db_connect() as conn, conn.cursor() as cur:

        cur.execute(
            """
            SELECT id, tags
            FROM contacts
            WHERE id = ANY(%s)
            AND archived_at IS NULL
            ORDER BY id FOR UPDATE
        """,
            (ids,),
        )

        rows = cur.fetchall()

        existing = 0
        updated = 0
        full = 0

        for row in rows:

            tags = row["tags"] or []

            if any(t.casefold() == tag.casefold() for t in tags):
                existing += 1
                continue

            if len(tags) >= 20:
                full += 1
                continue

            cur.execute(
                """
                UPDATE contacts

                SET tags = array_append(tags, %s),
                    updated_at = NOW()

                WHERE id = %s

            """,
                (
                    tag,
                    row["id"],
                ),
            )

            updated += 1

    return {
        "updated": updated,
        "already_tagged": existing,
        "tag_limit": full,
        "missing": len(ids) - len(rows),
    }
