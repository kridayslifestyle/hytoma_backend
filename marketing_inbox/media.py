import os
import requests

from urllib.parse import urlsplit
from fastapi import HTTPException
from fastapi.responses import Response

from db import db_connect
from marketing_inbox.api import router


LIMITS = {
    "image/jpeg": 5 * 1024 * 1024,
    "image/png": 5 * 1024 * 1024,
    "video/mp4": 16 * 1024 * 1024,
}


def media_response(data, mime):

    if (
        mime not in LIMITS
        or not data
        or len(data) > LIMITS[mime]
    ):
        raise HTTPException(
            502,
            "Invalid media."
        )

    return Response(
        content=bytes(data),
        media_type=mime,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/campaign-media/{campaign_id}")
def campaign_media(campaign_id: int):

    # First check permanently saved campaign media.

    with db_connect() as conn, conn.cursor() as cur:

        cur.execute("""
            SELECT
                c.media_id,
                a.mime,
                a.data

            FROM marketing_campaigns c

            LEFT JOIN campaign_media_archive a
                ON a.campaign_id = c.id
               AND a.media_id = c.media_id

            WHERE c.id = %s

            AND EXISTS (
                SELECT 1
                FROM campaign_recipients r
                WHERE r.campaign_id = c.id
                  AND r.send_status = 'accepted'
            )

        """, (campaign_id,))

        row = cur.fetchone()


    if not row or not row["media_id"]:

        raise HTTPException(
            404,
            "Media not found."
        )


    # If already archived, display directly.

    if row["data"] is not None:

        return media_response(
            row["data"],
            row["mime"],
        )


    # Existing campaigns may not yet have archived
    # media. Retrieve their original file from Meta.

    media_id = row["media_id"]

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


    if (
        not token
        or not phone_id
        or not media_id.isdigit()
    ):
        raise HTTPException(
            503,
            "Media retrieval unavailable."
        )


    headers = {
        "Authorization": f"Bearer {token}"
    }


    try:

        # Retrieve a fresh temporary media URL.

        with requests.get(
            f"https://graph.facebook.com/{version}/{media_id}",

            params={
                "phone_number_id": phone_id
            },

            headers=headers,
            timeout=15,
            allow_redirects=False,

        ) as res:

            if res.status_code != 200:
                raise ValueError(
                    "Metadata unavailable"
                )

            info = res.json()


        mime = info["mime_type"]
        url = info["url"]

        parsed = urlsplit(url)


        # Security: only retrieve files from
        # the trusted Meta media download host.

        if (
            mime not in LIMITS
            or parsed.scheme != "https"
            or parsed.hostname != "lookaside.fbsbx.com"
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ValueError(
                "Unexpected media metadata"
            )


        # Download original image or video.

        with requests.get(
            url,
            headers=headers,
            stream=True,
            timeout=(10, 30),
            allow_redirects=False,

        ) as res:

            response_mime = (
                res.headers
                .get("Content-Type", "")
                .split(";")[0]
                .strip()
                .lower()
            )

            if (
                res.status_code != 200
                or response_mime != mime
            ):
                raise ValueError(
                    "Download unavailable"
                )


            data = bytearray()

            for chunk in res.iter_content(65536):

                data.extend(chunk)

                if len(data) > LIMITS[mime]:
                    raise ValueError(
                        "Media too large"
                    )


        if not data:
            raise ValueError(
                "Empty media"
            )


    except (
        requests.RequestException,
        ValueError,
        KeyError,
        TypeError,
    ):

        raise HTTPException(
            502,
            "Original media unavailable from Meta."
        )


    # Save the recovered media permanently.

    with db_connect() as conn, conn.cursor() as cur:

        cur.execute("""
            INSERT INTO campaign_media_archive (
                campaign_id,
                media_id,
                mime,
                data
            )

            SELECT
                id,
                media_id,
                %s,
                %s

            FROM marketing_campaigns

            WHERE id = %s
              AND media_id = %s

            ON CONFLICT (campaign_id)
            DO NOTHING

        """, (
            mime,
            bytes(data),
            campaign_id,
            media_id,
        ))


    return media_response(
        data,
        mime,
    )