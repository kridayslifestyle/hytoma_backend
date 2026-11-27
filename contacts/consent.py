from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
)

from pydantic import BaseModel, Field, StrictBool

from auth import require_admin
from db import db_connect


router = APIRouter(
    prefix="/admin/contacts/consent",
    tags=["Contact Consent"],
    dependencies=[Depends(require_admin)],
)


# ============================================================
# 1. REQUEST MODEL
# ============================================================

class BulkConsentRequest(BaseModel):

    contact_ids: list[int] = Field(
        min_length=1,
        max_length=5000
    )

    confirmed: StrictBool


# ============================================================
# 2. GRANT MARKETING CONSENT
# ============================================================

def grant_consent(cur, contact_ids, source):

    cur.execute("""
        UPDATE contacts

        SET
            consent_status = 'opted_in',
            consent_at = NOW(),
            consent_source = %s,
            updated_at = NOW()

        WHERE id = ANY(%s)

        AND consent_status = 'pending'

        AND phone !~ '^120255501[0-9]{2}$'

        AND NOT EXISTS (
            SELECT 1
            FROM leads
            WHERE leads.phone = contacts.phone
            AND leads.status = 'opted_out'
        )

        RETURNING id

    """, (
        source,
        contact_ids,
    ))

    updated = [
        row["id"]
        for row in cur.fetchall()
    ]

    for contact_id in updated:

        cur.execute("""
            INSERT INTO contact_consent_events (
                contact_id,
                status,
                source
            )

            VALUES (
                %s,
                'opted_in',
                %s
            )

        """, (
            contact_id,
            source,
        ))

    return len(updated)


# ============================================================
# 3. BULK OPT-IN EXISTING CONTACTS
# ============================================================

@router.post("/bulk-opt-in")
def bulk_opt_in(payload: BulkConsentRequest):

    if not payload.confirmed:

        raise HTTPException(
            status_code=422,
            detail="Confirm existing marketing permission."
        )

    ids = sorted(set(payload.contact_ids))

    if any(i <= 0 for i in ids):

        raise HTTPException(
            status_code=422,
            detail="Invalid contact ID."
        )

    with db_connect() as conn:

        with conn.cursor() as cur:

            updated = grant_consent(
                cur,
                ids,
                "admin_verified"
            )

    return {
        "updated": updated,
        "skipped": len(ids) - updated,
    }


# ============================================================
# 4. RECORD MANUAL OPT-OUT REQUESTS
# ============================================================

@router.post("/bulk-opt-out")
def bulk_opt_out(payload: BulkConsentRequest):

    if not payload.confirmed:

        raise HTTPException(
            status_code=422,
            detail="Confirm the opt-out request."
        )

    ids = sorted(set(payload.contact_ids))

    if any(i <= 0 for i in ids):

        raise HTTPException(
            status_code=422,
            detail="Invalid contact ID."
        )

    with db_connect() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                UPDATE contacts

                SET
                    consent_status = 'opted_out',
                    consent_at = NOW(),
                    consent_source = 'admin_opt_out',
                    updated_at = NOW()

                WHERE id = ANY(%s)

                AND consent_status <> 'opted_out'

                RETURNING id

            """, (ids,))

            updated = [
                row["id"]
                for row in cur.fetchall()
            ]

            for contact_id in updated:

                cur.execute("""
                    INSERT INTO contact_consent_events (
                        contact_id,
                        status,
                        source
                    )

                    VALUES (
                        %s,
                        'opted_out',
                        'admin_opt_out'
                    )

                """, (contact_id,))

    return {
        "updated": len(updated),
        "skipped": len(ids) - len(updated),
    }


# ============================================================
# 5. AUTOMATIC WHATSAPP UNSUBSCRIBE HANDLING
# ============================================================

OPT_OUT_COMMANDS = {
    "stop",
    "unsubscribe",
    "opt out",
    "opt_out",
    "opt-out",
    "stop marketing",
    "stop messages",
    "stop receiving offers",
    "stop receiving messages",
    "unsubscribe from marketing",
}


def record_inbound_opt_out(
    cur,
    phone,
    message_text,
    display_content=None,
):

    values = {
        " ".join(
            (value or "")
            .strip()
            .casefold()
            .split()
        ).rstrip(".!")

        for value in (
            message_text,
            display_content
        )
    }

    if not values.intersection(OPT_OUT_COMMANDS):

        return False

    cur.execute("""
        INSERT INTO contacts (
            phone,
            source,
            consent_status,
            consent_at,
            consent_source
        )

        VALUES (
            %s,
            'crm',
            'opted_out',
            NOW(),
            'whatsapp_unsubscribe'
        )

        ON CONFLICT (phone)
        DO UPDATE

        SET
            consent_status = 'opted_out',
            consent_at = NOW(),
            consent_source = 'whatsapp_unsubscribe',
            updated_at = NOW()

        WHERE contacts.consent_status <> 'opted_out'

        RETURNING id

    """, (phone,))

    row = cur.fetchone()

    if row:

        cur.execute("""
            INSERT INTO contact_consent_events (
                contact_id,
                status,
                source
            )

            VALUES (
                %s,
                'opted_out',
                'whatsapp_unsubscribe'
            )

        """, (row["id"],))

    return True