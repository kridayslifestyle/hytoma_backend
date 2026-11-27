import logging
import os
import re

from pathlib import PurePath

import requests

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    UploadFile,
)

from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb

from auth import require_admin
from db import db_connect

from marketing.media_upload import (
    ALLOWED_MEDIA_TYPES,
    validate_media_content,
)


logger = logging.getLogger("hytoma.campaigns")


router = APIRouter(
    prefix="/admin/campaigns",
    tags=["Campaign Content"],
    dependencies=[Depends(require_admin)],
)


# --------------------------------------------------
# Database Migration
# --------------------------------------------------

def initialize_campaign_content():

    with db_connect() as conn:

        with conn.cursor() as cur:

            cur.execute("""

                ALTER TABLE marketing_campaigns

                ADD COLUMN IF NOT EXISTS media_id TEXT,

                ADD COLUMN IF NOT EXISTS media_type TEXT,

                ADD COLUMN IF NOT EXISTS media_filename TEXT,

                ADD COLUMN IF NOT EXISTS
                    media_uploaded_at TIMESTAMPTZ,

                ADD COLUMN IF NOT EXISTS
                    template_values JSONB
                    NOT NULL DEFAULT '{}';

            """, prepare=False)


# --------------------------------------------------
# Detect Template Requirements
# --------------------------------------------------

def requirements(template):

    fields = []
    media = None

    def add(text, prefix):

        tokens = re.findall(
            r"\{\{\s*(.*?)\s*\}\}",
            text or "",
        )

        if any(
            not re.fullmatch(
                r"[1-9][0-9]*",
                token,
            )
            for token in tokens
        ):

            raise HTTPException(
                422,
                "Only numbered template variables are supported.",
            )

        numbers = sorted({
            int(token)
            for token in tokens
        })

        if numbers != list(
            range(1, len(numbers) + 1)
        ):

            raise HTTPException(
                422,
                "Template variable numbering is unsupported.",
            )

        for number in numbers:

            fields.append(
                f"{prefix}:{number}"
            )

    for component in template.get(
        "components", []
    ):

        kind = component.get("type")

        # Template header

        if kind == "HEADER":

            fmt = component.get("format")

            if fmt in ("IMAGE", "VIDEO"):

                media = fmt

            elif fmt == "TEXT":

                add(
                    component.get("text"),
                    "header",
                )

            else:

                raise HTTPException(
                    422,
                    "Unsupported template header.",
                )

        # Message body variables

        elif kind == "BODY":

            add(
                component.get("text"),
                "body",
            )

        # Interactive buttons

        elif kind == "BUTTONS":

            for index, button in enumerate(
                component.get("buttons", [])
            ):

                if button.get("type") == "URL":

                    add(
                        button.get("url"),
                        f"url:{index}",
                    )

                elif button.get("type") not in (
                    "PHONE_NUMBER",
                    "QUICK_REPLY",
                ):

                    raise HTTPException(
                        422,
                        "Unsupported button type.",
                    )

        elif kind != "FOOTER":

            raise HTTPException(
                422,
                "Unsupported template component.",
            )

    return {

        "media_required": media,

        "fields": fields,

    }


# --------------------------------------------------
# Retrieve Campaign Draft
# --------------------------------------------------

def get_draft(
    cur,
    campaign_id,
    lock=False,
):

    cur.execute(

        "SELECT * FROM marketing_campaigns "
        "WHERE id=%s"

        + (
            " FOR UPDATE"
            if lock
            else ""
        ),

        (campaign_id,),

    )

    row = cur.fetchone()

    if not row:

        raise HTTPException(
            404,
            "Campaign not found.",
        )

    if row["status"] != "draft":

        raise HTTPException(
            409,
            "Only drafts can be configured.",
        )

    return row


# --------------------------------------------------
# GET Campaign Content Requirements
# --------------------------------------------------

@router.get("/{campaign_id}/content")
def get_content(campaign_id: int):

    with db_connect() as conn:

        with conn.cursor() as cur:

            row = get_draft(
                cur,
                campaign_id,
            )

    return {

        **requirements(
            row["template_snapshot"]
        ),

        "values": row["template_values"],

        "media_id": row["media_id"],

        "media_type": row["media_type"],

        "media_filename": row["media_filename"],

        "media_uploaded_at": row["media_uploaded_at"],

    }


# --------------------------------------------------
# Template Variables Request
# --------------------------------------------------

class ContentRequest(BaseModel):

    values: dict[str, str] = Field(
        default_factory=dict,
        max_length=50,
    )


# --------------------------------------------------
# PATCH Save Template Variables
# --------------------------------------------------

@router.patch("/{campaign_id}/content")
def save_content(
    campaign_id: int,
    payload: ContentRequest,
):

    with db_connect() as conn:

        with conn.cursor() as cur:

            row = get_draft(
                cur,
                campaign_id,
                lock=True,
            )

            req = requirements(
                row["template_snapshot"]
            )

            if set(payload.values) != set(
                req["fields"]
            ):

                raise HTTPException(
                    422,
                    "Provide exactly the required template variables.",
                )

            values = {

                key: value.strip()

                for key, value
                in payload.values.items()

            }

            if any(

                not value

                or len(value) > 500

                or any(
                    ord(character) < 32
                    for character in value
                )

                for value in values.values()

            ):

                raise HTTPException(
                    422,
                    "Variables must be 1–500 characters without control characters.",
                )

            cur.execute("""

                UPDATE marketing_campaigns

                SET template_values = %s

                WHERE id = %s

            """, (

                Jsonb(values),

                campaign_id,

            ))

    return {

        "saved": True,

        "values": values,

    }


# --------------------------------------------------
# POST Upload Campaign Media
# --------------------------------------------------

@router.post("/{campaign_id}/media")
def upload_campaign_media(

    campaign_id: int,

    file: UploadFile = File(...),

):

    mime = file.content_type or ""

    filename = PurePath(

        (file.filename or "").replace(
            "\\",
            "/",
        )

    ).name

    try:

        # Check the template requirements.

        with db_connect() as conn:

            with conn.cursor() as cur:

                row = get_draft(
                    cur,
                    campaign_id,
                )

                req = requirements(
                    row["template_snapshot"]
                )

        # Detect uploaded media category.

        actual = (

            "IMAGE"

            if mime in (
                "image/jpeg",
                "image/png",
            )

            else "VIDEO"

            if mime == "video/mp4"

            else None

        )

        if (
            not actual
            or actual != req["media_required"]
        ):

            raise HTTPException(
                422,
                "File type does not match the template header.",
            )

        # Read the file within our size limit.

        data = file.file.read(

            ALLOWED_MEDIA_TYPES[mime] + 1

        )

        if len(data) > ALLOWED_MEDIA_TYPES[mime]:

            raise HTTPException(
                413,
                "Media file too large.",
            )

        # Reuse existing image/video validation.

        try:

            validate_media_content(

                filename,

                mime,

                data,

            )

        except ValueError as exc:

            raise HTTPException(
                422,
                str(exc),
            ) from exc

    finally:

        file.file.close()


    # --------------------------------------------------
    # Meta Cloud API Credentials
    # --------------------------------------------------

    token = os.getenv(
        "WHATSAPP_ACCESS_TOKEN"
    )

    phone_id = os.getenv(
        "WHATSAPP_PHONE_NUMBER_ID"
    )

    version = os.getenv(
        "WHATSAPP_API_VERSION",
        "v27.0",
    )

    if not token or not phone_id:

        raise HTTPException(
            503,
            "WhatsApp credentials are missing.",
        )


    # --------------------------------------------------
    # Upload Actual Sending Media to Meta
    # --------------------------------------------------

    try:

        result = requests.post(

            (
                f"https://graph.facebook.com/"
                f"{version}/{phone_id}/media"
            ),

            headers={

                "Authorization": f"Bearer {token}",

            },

            data={

                "messaging_product": "whatsapp",

                "type": mime,

            },

            files={

                "file": (
                    filename,
                    data,
                    mime,
                ),

            },

            timeout=60,

        )

        result.raise_for_status()

        media_id = str(
            result.json()["id"]
        )

        if not media_id.isdigit():

            raise ValueError(
                "Invalid media ID"
            )

    except (

        requests.RequestException,

        ValueError,

        KeyError,

    ):

        logger.warning(
            "Campaign media upload failed"
        )

        raise HTTPException(
            502,
            "Meta media upload failed.",
        )


    # --------------------------------------------------
    # Save Media ID to Campaign
    # --------------------------------------------------

    with db_connect() as conn:

        with conn.cursor() as cur:

            row = get_draft(

                cur,

                campaign_id,

                lock=True,

            )

            if requirements(
                row["template_snapshot"]
            )["media_required"] != actual:

                raise HTTPException(
                    409,
                    "Template changed. Upload again.",
                )

            cur.execute("""

                UPDATE marketing_campaigns

                SET

                    media_id = %s,

                    media_type = %s,

                    media_filename = %s,

                    media_uploaded_at = NOW()

                WHERE id = %s

                RETURNING media_uploaded_at

            """, (

                media_id,

                actual,

                filename,

                campaign_id,

            ))

            uploaded_at = cur.fetchone()[
                "media_uploaded_at"
            ]


    return {

        "media_id": media_id,

        "media_type": actual,

        "media_filename": filename,

        "media_uploaded_at": uploaded_at,

    }