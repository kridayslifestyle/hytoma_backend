import csv
import io

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
)

from pydantic import BaseModel, Field, StrictBool

from auth import require_admin
from db import db_connect
from contacts.api import normalize_phone
from contacts.consent import grant_consent


router = APIRouter(
    prefix="/admin/contacts/csv",
    tags=["Contacts CSV Import"],
    dependencies=[Depends(require_admin)],
)

MAX_ROWS = 5000


class CSVRequest(BaseModel):

    content: str = Field(
        min_length=1,
        max_length=1_000_000
    )

class CSVConfirmRequest(CSVRequest):

    confirmed_opt_in: StrictBool = False


# ============================================================
# 1. PARSE AND VALIDATE CSV
# ============================================================

def parse_csv(content):

    try:

        reader = csv.DictReader(
            io.StringIO(
                content.lstrip("\ufeff"),
                newline=""
            ),
            strict=True
        )

        headers = [
            h.strip().lower()
            for h in (reader.fieldnames or [])
        ]

        if (
            "phone" not in headers
            or len(headers) != len(set(headers))
        ):

            raise HTTPException(
                422,
                "CSV needs a unique phone column."
            )

        reader.fieldnames = headers

        rows = []

        for row in reader:

            if not any(
                v
                for k, v in row.items()
                if k is not None
            ):
                continue

            if len(rows) >= MAX_ROWS:

                raise HTTPException(
                    422,
                    "Maximum 5000 contacts per file."
                )

            record = {
                "row": reader.line_num,
                "phone": "",
                "name": "",
                "location": "",
                "tags": [],
                "error": None,
            }

            try:

                if None in row:
                    raise ValueError(
                        "Extra columns in row."
                    )

                record["phone"] = normalize_phone(
                    row.get("phone") or ""
                )

                record["name"] = (
                    row.get("name") or ""
                ).strip()

                record["location"] = (
                    row.get("location") or ""
                ).strip()

                tags = (
                    row.get("tags") or ""
                ).split(",")

                tags = list(
                    dict.fromkeys(
                        t.strip()
                        for t in tags
                        if t.strip()
                    )
                )

                if (
                    len(record["name"]) > 150
                    or len(record["location"]) > 255
                ):

                    raise ValueError(
                        "Name or location too long."
                    )

                if (
                    len(tags) > 20
                    or any(len(t) > 80 for t in tags)
                ):

                    raise ValueError(
                        "Maximum 20 tags, "
                        "80 characters each."
                    )

                if any(
                    "\x00" in v
                    for v in [
                        record["name"],
                        record["location"],
                        *tags
                    ]
                ):

                    raise ValueError(
                        "Invalid null character."
                    )

                record["tags"] = tags

            except ValueError as exc:

                record["error"] = str(exc)

            rows.append(record)

        if not rows:

            raise HTTPException(
                422,
                "CSV has no contact rows."
            )

        return rows

    except csv.Error as exc:

        raise HTTPException(
            422,
            "Invalid CSV format."
        ) from exc


# ============================================================
# 2. CLASSIFY VALID, DUPLICATE AND INVALID CONTACTS
# ============================================================

def classify(cur, rows):

    phones = list({
        r["phone"]
        for r in rows
        if not r["error"]
    })

    cur.execute("""
        SELECT phone
        FROM contacts
        WHERE phone = ANY(%s)
    """, (phones,))

    existing = {
        r["phone"]
        for r in cur.fetchall()
    }

    seen = set()

    counts = {
        "total": len(rows),
        "valid": 0,
        "duplicate": 0,
        "invalid": 0,
    }

    for r in rows:

        if r["error"]:

            status = "invalid"

        elif (
            r["phone"] in existing
            or r["phone"] in seen
        ):

            status = "duplicate"

            r["error"] = (
                "Phone already exists "
                "or repeats in file."
            )

        else:

            status = "valid"

        if status != "invalid":
            seen.add(r["phone"])

        r["status"] = status

        counts[status] += 1

    return counts


# ============================================================
# 3. PREVIEW CSV IMPORT
# ============================================================

@router.post("/preview")
def preview_csv(payload: CSVRequest):

    rows = parse_csv(payload.content)

    with db_connect() as conn:

        with conn.cursor() as cur:

            counts = classify(cur, rows)

    return {
        "counts": counts,
        "rows": rows,
    }


# ============================================================
# 4. CONFIRM CSV IMPORT
# ============================================================

@router.post("/confirm")
def confirm_csv(payload: CSVConfirmRequest):

    rows = parse_csv(payload.content)

    with db_connect() as conn:

        with conn.cursor() as cur:

            # Revalidate to prevent stale preview results

            counts = classify(cur, rows)

            added = 0
            new_ids = []

            for r in rows:

                if r["status"] != "valid":
                    continue

                cur.execute("""
                    INSERT INTO contacts (
                        phone,
                        name,
                        location,
                        source,
                        tags
                    )

                    VALUES (
                        %s,
                        %s,
                        %s,
                        'csv',
                        %s
                    )

                    ON CONFLICT (phone)
                    DO NOTHING

                    RETURNING id

                """, (
                    r["phone"],
                    r["name"],
                    r["location"],
                    r["tags"],
                ))

                inserted = cur.fetchone()

                if inserted:

                    added += 1

                    new_ids.append(inserted["id"])

                else:

                    counts["duplicate"] += 1

            opted_in = 0

            if payload.confirmed_opt_in and new_ids:

                opted_in = grant_consent(
                    cur,
                    new_ids,
                    "csv_admin_verified"
                )

            return {
                "added": added,
                "duplicates": counts["duplicate"],
                "invalid": counts["invalid"],
                "opted_in": opted_in,
            }