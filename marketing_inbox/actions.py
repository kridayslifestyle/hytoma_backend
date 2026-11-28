import json
import os
import re
import hashlib
import logging

from pathlib import PurePath
from uuid import UUID

import requests

from fastapi import File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from db import db_connect, customer_send_guard

from marketing.templates import get_marketing_templates
from marketing.media_upload import validate_media_content

from campaigns.content import requirements
from campaigns.send_test import build_components

from whatsapp_service import send_whatsapp_payload
from marketing_inbox.api import router


log = logging.getLogger(__name__)

MAX_BYTES = 4 * 1024 * 1024

KINDS = {
    "image/jpeg": "image",
    "image/png": "image",
    "video/mp4": "video",
    "application/pdf": "document",
}


def initialize_marketing_actions():

    with db_connect() as conn, conn.cursor() as cur:

        cur.execute("""
            ALTER TABLE inbound_events
            ADD COLUMN IF NOT EXISTS message_at TIMESTAMPTZ;

            CREATE TABLE IF NOT EXISTS marketing_manual_messages (
                request_id UUID PRIMARY KEY,
                contact_id BIGINT NOT NULL REFERENCES contacts(id),
                campaign_id BIGINT NOT NULL,
                kind TEXT NOT NULL,
                body TEXT NOT NULL DEFAULT '',
                template_snapshot JSONB,
                template_values JSONB NOT NULL DEFAULT '{}',
                media_filename TEXT,
                mime TEXT,
                media_data BYTEA,
                fingerprint TEXT NOT NULL,
                wamid TEXT UNIQUE,
                status TEXT NOT NULL DEFAULT 'sending',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

                FOREIGN KEY (campaign_id, contact_id)
                REFERENCES campaign_recipients(
                    campaign_id, contact_id
                )
            );

            CREATE INDEX IF NOT EXISTS marketing_manual_contact_idx
            ON marketing_manual_messages(
                contact_id, created_at DESC
            );
        """, prepare=False)


def target(cur, contact_id):

    cur.execute("""
        SELECT
            c.id,
            c.phone,
            c.consent_status,
            r.campaign_id,
            l.status AS lead_status,
            l.assigned_salesperson,

            (
                SELECT MAX(
                    LEAST(
                        e.created_at,
                        COALESCE(e.message_at, e.created_at)
                    )
                ) + INTERVAL '24 hours'

                FROM inbound_events e
                WHERE e.phone = c.phone

            ) AS window_expires_at

        FROM contacts c

        JOIN LATERAL (
            SELECT campaign_id
            FROM campaign_recipients
            WHERE contact_id = c.id
              AND send_status = 'accepted'
            ORDER BY attempted_at DESC NULLS LAST,
                     campaign_id DESC
            LIMIT 1
        ) r ON TRUE

        LEFT JOIN leads l ON l.phone = c.phone
        WHERE c.id = %s
    """, (contact_id,))

    row = cur.fetchone()

    if not row:
        raise HTTPException(
            404, "Conversation not found."
        )

    return row


def check_send(cur, contact_id, kind):

    row = target(cur, contact_id)

    if (
        row["consent_status"] == "opted_out"
        or row["lead_status"] == "opted_out"
        or re.fullmatch(
            r"120255501[0-9]{2}", row["phone"]
        )
    ):
        raise HTTPException(
            403, "Contact cannot receive messages."
        )

    if kind == "template":

        if row["consent_status"] != "opted_in":
            raise HTTPException(
                403,
                "Verified messaging permission is required."
            )

    else:

        cur.execute("""
            SELECT %s::timestamptz >
                NOW() + INTERVAL '60 seconds'
                AS open
        """, (row["window_expires_at"],))

        if cur.fetchone()["open"] is not True:

            raise HTTPException(
                403,
                "Reply window closed. Use an approved template."
            )

    return row


def templates():

    try:
        live = get_marketing_templates()

    except (requests.RequestException, RuntimeError):
        raise HTTPException(
            502, "Unable to load approved templates."
        )

    result = []

    for template in live:

        if template.get("status") != "APPROVED":
            continue

        if template.get("category") not in (
            "MARKETING", "UTILITY"
        ):
            continue

        try:
            req = requirements(template)
        except HTTPException:
            continue

        result.append({
            **template,
            **req
        })

    return result


@router.get("/reply-templates")
def reply_templates():

    return {
        "templates": templates()
    }


def read_file(file):

    if not file:
        return None, None, None

    try:

        mime = file.content_type or ""

        name = PurePath(
            (file.filename or "").replace("\\", "/")
        ).name

        if mime not in KINDS or not name:
            raise HTTPException(
                422, "Choose JPG, PNG, MP4 or PDF."
            )

        data = file.file.read(MAX_BYTES + 1)

        if not data or len(data) > MAX_BYTES:
            raise HTTPException(
                413, "Attachment must be 4 MB or smaller."
            )

        if mime == "application/pdf":

            if (
                not name.lower().endswith(".pdf")
                or not data.startswith(b"%PDF-")
            ):
                raise HTTPException(422, "Invalid PDF.")

        else:

            try:
                validate_media_content(name, mime, data)

            except (ValueError, OSError) as exc:
                raise HTTPException(422, str(exc))

        return name, mime, data

    finally:

        file.file.close()


def upload(name, mime, data):

    token = os.getenv("WHATSAPP_ACCESS_TOKEN")

    phone = os.getenv("WHATSAPP_PHONE_NUMBER_ID")

    version = os.getenv(
        "WHATSAPP_API_VERSION", "v27.0"
    )

    if not token or not phone:
        raise HTTPException(
            503, "WhatsApp credentials missing."
        )

    try:

        res = requests.post(
            f"https://graph.facebook.com/{version}/{phone}/media",

            headers={
                "Authorization": f"Bearer {token}"
            },

            data={
                "messaging_product": "whatsapp",
                "type": mime
            },

            files={
                "file": (name, data, mime)
            },

            timeout=60,
        )

        res.raise_for_status()

        media_id = str(res.json()["id"])

        if not media_id.isdigit():
            raise ValueError("Invalid media ID")

        return media_id

    except (
        requests.RequestException,
        ValueError,
        KeyError
    ):

        raise HTTPException(
            502, "Media upload failed; no message sent."
        )


@router.post("/conversations/{contact_id}/send")
def send(
    contact_id: int,
    request_id: UUID = Form(...),
    kind: str = Form(...),
    body: str = Form(""),
    template_id: str = Form(""),
    values: str = Form("{}"),
    file: UploadFile | None = File(None),
):

    body = body.strip()

    if kind not in (
        "text", "image", "video", "document", "template"
    ) or len(body) > 4096:

        raise HTTPException(422, "Invalid message.")

    name, mime, data = read_file(file)

    try:

        vals = json.loads(values)

        if not isinstance(vals, dict) or any(
            not isinstance(v, str)
            or not v.strip()
            or len(v) > 1024
            for v in vals.values()
        ):
            raise ValueError()

    except (ValueError, TypeError):

        raise HTTPException(
            422, "Invalid template variables."
        )

    if kind == "text" and (not body or data):
        raise HTTPException(
            422, "Enter text without an attachment."
        )

    if kind in ("image", "video", "document"):

        if KINDS.get(mime) != kind or len(body) > 1024:

            raise HTTPException(
                422,
                "Choose a matching attachment and "
                "caption up to 1024 characters."
            )

    fingerprint = hashlib.sha256(
        json.dumps(
            [kind, body, template_id, vals, name, mime],
            sort_keys=True
        ).encode() + (data or b"")
    ).hexdigest()

    with db_connect() as conn, conn.cursor() as cur:

        row = target(cur, contact_id)

    with customer_send_guard(row["phone"]):

        with db_connect() as conn, conn.cursor() as cur:

            cur.execute("""
                SELECT contact_id, fingerprint, status, wamid

                FROM marketing_manual_messages
                WHERE request_id = %s
            """, (request_id,))

            prev = cur.fetchone()

            if prev:

                if (
                    prev["contact_id"] != contact_id
                    or prev["fingerprint"] != fingerprint
                ):
                    raise HTTPException(
                        409, "Request ID already used."
                    )

                return {
                    "status": prev["status"],
                    "whatsapp_message_id": prev["wamid"]
                }

            row = check_send(cur, contact_id, kind)

            cur.execute("""
                SELECT 1
                FROM marketing_manual_messages
                WHERE contact_id = %s
                  AND status IN ('sending', 'unknown')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM whatsapp_message_statuses s
                      WHERE s.whatsapp_message_id =
                            marketing_manual_messages.wamid
                  )
                LIMIT 1
            """, (contact_id,))

            if cur.fetchone():

                raise HTTPException(
                    409,
                    "Previous send is uncertain. "
                    "Verify delivery before further sends."
                )

        snapshot = None

        payload = {
            "messaging_product": "whatsapp",
            "to": row["phone"],
            "type": kind
        }

        # TEXT MESSAGE

        if kind == "text":

            payload["text"] = {
                "body": body,
                "preview_url": False
            }

        # APPROVED TEMPLATE

        elif kind == "template":

            snapshot = next(
                (
                    t for t in templates()
                    if str(t["id"]) == template_id
                ),
                None
            )

            if not snapshot:
                raise HTTPException(
                    422,
                    "Template is not currently approved."
                )

            req = requirements(snapshot)

            if set(vals) != set(req["fields"]):

                raise HTTPException(
                    422, "Fill every template variable."
                )

            required = req["media_required"]

            if (
                required
                and KINDS.get(mime, "").upper() != required
            ) or (not required and data):

                raise HTTPException(
                    422,
                    "Select the required template header media."
                )

            media_id = (
                upload(name, mime, data)
                if data else None
            )

            components = build_components({

                "id": row["campaign_id"],
                "template_snapshot": snapshot,
                "template_values": vals,
                "media_id": media_id,
                "media_type": required

            })

            payload["template"] = {

                "name": snapshot["name"],

                "language": {
                    "code": snapshot["language"]
                },

                "components": components
            }

        # IMAGE, VIDEO OR DOCUMENT

        else:

            media_id = upload(name, mime, data)

            payload[kind] = {
                "id": media_id
            }

            if body:
                payload[kind]["caption"] = body

            if kind == "document":
                payload[kind]["filename"] = name

        # Reserve request before sending.

        with db_connect() as conn, conn.cursor() as cur:

            row = check_send(cur, contact_id, kind)

            cur.execute("""
                INSERT INTO marketing_manual_messages (
                    request_id,
                    contact_id,
                    campaign_id,
                    kind,
                    body,
                    template_snapshot,
                    template_values,
                    media_filename,
                    mime,
                    media_data,
                    fingerprint
                )

                VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )

                ON CONFLICT (request_id) DO NOTHING
                RETURNING request_id

            """, (
                request_id,
                contact_id,
                row["campaign_id"],
                kind,
                body,
                Jsonb(snapshot) if snapshot else None,
                Jsonb(vals),
                name,
                mime,
                data,
                fingerprint
            ))

            if not cur.fetchone():

                raise HTTPException(
                    409, "Request already submitted."
                )

        wamid = None

        try:

            result = send_whatsapp_payload(payload)

            wamid = result["messages"][0]["id"]

            if not wamid:
                raise ValueError("Missing message ID")

            with db_connect() as conn, conn.cursor() as cur:

                cur.execute("""
                    UPDATE marketing_manual_messages

                    SET status = 'accepted',
                        wamid = %s

                    WHERE request_id = %s

                """, (wamid, request_id))

        except Exception as exc:

            rejected = (
                isinstance(exc, requests.HTTPError)
                and exc.response is not None
                and 400 <= exc.response.status_code < 500
            )

            log.warning(
                "Manual marketing send %s",
                "rejected" if rejected else "uncertain"
            )

            with db_connect() as conn, conn.cursor() as cur:

                cur.execute("""
                    UPDATE marketing_manual_messages

                    SET status = %s,
                        wamid = %s

                    WHERE request_id = %s
                      AND status = 'sending'

                """, (
                    "failed" if rejected else "unknown",
                    wamid,
                    request_id
                ))

            if rejected:

                raise HTTPException(
                    422,
                    "Meta rejected the message. "
                    "Check template or recipient eligibility."
                ) from exc

            raise HTTPException(
                502,
                "Send outcome uncertain. "
                "Do not resend; check WhatsApp."
            ) from exc

        return {
            "status": "accepted",
            "whatsapp_message_id": wamid
        }


@router.get("/manual-media/{request_id}")
def manual_media(request_id: UUID):

    with db_connect() as conn, conn.cursor() as cur:

        cur.execute("""
            SELECT mime, media_data
            FROM marketing_manual_messages
            WHERE request_id = %s

        """, (request_id,))

        row = cur.fetchone()

    if (
        not row
        or not row["media_data"]
        or row["mime"] not in KINDS
    ):

        raise HTTPException(
            404, "Attachment unavailable."
        )

    headers = {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff"
    }

    if row["mime"] == "application/pdf":

        headers["Content-Disposition"] = (
            'attachment; filename="document.pdf"'
        )

    return Response(
        bytes(row["media_data"]),
        media_type=row["mime"],
        headers=headers
    )


class LeadRequest(BaseModel):

    salesperson_id: int | None = Field(
        default=None, gt=0
    )


@router.post("/conversations/{contact_id}/lead")
def create_lead(
    contact_id: int,
    payload: LeadRequest
):

    with db_connect() as conn, conn.cursor() as cur:

        row = target(cur, contact_id)

    with customer_send_guard(row["phone"]):

        with db_connect() as conn, conn.cursor() as cur:

            row = target(cur, contact_id)

            if (
                row["consent_status"] == "opted_out"
                or row["lead_status"] == "opted_out"
            ):
                raise HTTPException(
                    403, "Contact opted out."
                )

            salesperson = None

            if payload.salesperson_id:

                cur.execute("""
                    SELECT name
                    FROM salespeople
                    WHERE id = %s
                      AND is_active
                """, (payload.salesperson_id,))

                person = cur.fetchone()

                if not person:

                    raise HTTPException(
                        422, "Choose an active salesperson."
                    )

                salesperson = person["name"]

            cur.execute("""
                INSERT INTO leads (
                    phone,
                    step,
                    status,
                    needs_admin_followup,
                    bot_paused,
                    bot_paused_at
                )

                VALUES (
                    %s,
                    'closed',
                    'human',
                    TRUE,
                    TRUE,
                    NOW()
                )

                ON CONFLICT (phone) DO NOTHING

            """, (row["phone"],))

            if salesperson:

                cur.execute("""
                    UPDATE leads

                    SET assigned_salesperson = %s,
                        sales_status = 'assigned',
                        assigned_at = NOW(),
                        updated_at = NOW()

                    WHERE phone = %s
                      AND status <> 'opted_out'

                """, (salesperson, row["phone"]))

            cur.execute("""
                SELECT
                    phone,
                    status,
                    assigned_salesperson,
                    sales_status

                FROM leads
                WHERE phone = %s

            """, (row["phone"],))

            return {
                "lead": cur.fetchone()
            }