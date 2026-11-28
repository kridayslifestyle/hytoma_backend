import os
import re

from uuid import UUID

import requests

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

from contacts.api import normalize_phone

from campaigns.api import eligible_templates

from campaigns.content import (
    get_draft,
    requirements,
)

from whatsapp_service import send_whatsapp_payload


router = APIRouter(
    prefix="/admin/campaigns",
    tags=["Campaign Test Send"],
    dependencies=[Depends(require_admin)],
)


# --------------------------------------------------
# Database Initialization
# --------------------------------------------------

def initialize_campaign_test_send():

    with db_connect() as conn:

        with conn.cursor() as cur:

            cur.execute("""

                CREATE TABLE IF NOT EXISTS campaign_test_sends (

                    request_id UUID PRIMARY KEY,

                    campaign_id BIGINT NOT NULL
                        REFERENCES marketing_campaigns(id),

                    phone TEXT NOT NULL,

                    status TEXT NOT NULL
                        CHECK (
                            status IN (
                                'sending',
                                'accepted',
                                'unknown',
                                'rejected'
                            )
                        ),

                    wamid TEXT,

                    created_at TIMESTAMPTZ
                        NOT NULL DEFAULT NOW()

                );

                CREATE INDEX IF NOT EXISTS
                    campaign_test_phone_idx

                ON campaign_test_sends(
                    phone,
                    created_at DESC
                );

            """, prepare=False)


# --------------------------------------------------
# Test Send Request
# --------------------------------------------------

class TestSendRequest(BaseModel):

    phone: str = Field(
        min_length=1,
        max_length=40,
    )

    request_id: UUID

    confirmed: StrictBool = False


# --------------------------------------------------
# Compare Template Structure
# --------------------------------------------------

def signature(value):

    if isinstance(value, dict):

        return {

            key: signature(item)

            for key, item in value.items()

            if key != "example"

        }

    if isinstance(value, list):

        return [

            signature(item)

            for item in value

        ]

    return value


# --------------------------------------------------
# Build WhatsApp Template Components
# --------------------------------------------------

def build_components(row):

    req = requirements(
        row["template_snapshot"]
    )

    values = (
        row["template_values"] or {}
    )

    if (

        set(values) != set(req["fields"])

        or any(
            not value.strip()
            for value in values.values()
        )

    ):

        raise HTTPException(

            422,

            "Save all required template variables first.",

        )

    result = []


    # Generate text parameters.

    def text_params(prefix):

        keys = [

            key

            for key in req["fields"]

            if key.startswith(
                prefix + ":"
            )

        ]

        return [

            {

                "type": "text",

                "text": values[key],

            }

            for key in keys

        ]


    # --------------------------------------------------
    # Image / Video Header
    # --------------------------------------------------

    if req["media_required"]:

        kind = req[
            "media_required"
        ].lower()

        if (

            not row["media_id"]

            or row["media_type"] !=
                req["media_required"]

        ):

            raise HTTPException(

                422,

                "Upload the required media first.",

            )

        result.append({

            "type": "header",

            "parameters": [

                {

                    "type": kind,

                    kind: {

                        "id": row["media_id"],

                    },

                }

            ],

        })


    # --------------------------------------------------
    # Text Header Variables
    # --------------------------------------------------

    elif text_params("header"):

        result.append({

            "type": "header",

            "parameters": text_params(
                "header"
            ),

        })


    # --------------------------------------------------
    # Body Variables
    # --------------------------------------------------

    if text_params("body"):

        result.append({

            "type": "body",

            "parameters": text_params(
                "body"
            ),

        })


    # --------------------------------------------------
    # Buttons
    # --------------------------------------------------

    for component in row[
        "template_snapshot"
    ].get("components", []):

        if component.get("type") != "BUTTONS":

            continue

        for index, button in enumerate(

            component.get("buttons", [])

        ):

            # Dynamic website URL.

            if button.get("type") == "URL":

                params = text_params(
                    f"url:{index}"
                )

                if params:

                    if len(params) != 1:

                        raise HTTPException(

                            422,

                            "Unsupported dynamic URL.",

                        )

                    result.append({

                        "type": "button",

                        "sub_type": "url",

                        "index": str(index),

                        "parameters": params,

                    })


            # Quick Reply button.

            elif button.get("type") == "QUICK_REPLY":

                result.append({

                    "type": "button",

                    "sub_type": "quick_reply",

                    "index": str(index),

                    "parameters": [

                        {

                            "type": "payload",

                            "payload": (
                                f"campaign_test:"
                                f"{row['id']}:"
                                f"{index}"
                            ),

                        }

                    ],

                })


    # Static phone and website buttons
    # are already part of the approved template.

    return result


# --------------------------------------------------
# Verify Uploaded Media with Meta
# --------------------------------------------------

def verify_media(row):

    if not row["media_id"]:

        return

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

            "WhatsApp credentials missing.",

        )

    try:

        response = requests.get(

            (
                f"https://graph.facebook.com/"
                f"{version}/{row['media_id']}"
            ),

            headers={

                "Authorization": f"Bearer {token}",

            },

            params={

                "phone_number_id": phone_id,

            },

            timeout=15,

        )

        response.raise_for_status()

        mime = response.json().get(
            "mime_type",
            "",
        )

        if not mime.startswith(

            row["media_type"].lower() + "/"

        ):

            raise ValueError(
                "Media type mismatch"
            )

    except (

        requests.RequestException,

        ValueError,

    ):

        raise HTTPException(

            422,

            "Unable to verify uploaded media. "
            "Upload it again or retry verification.",

        )


# --------------------------------------------------
# POST Send Test Message
# --------------------------------------------------

@router.post("/{campaign_id}/test-send")
def test_send(

    campaign_id: int,

    payload: TestSendRequest,

):

    if not payload.confirmed:

        raise HTTPException(

            422,

            "Confirm this is your authorized test number.",

        )

    try:

        phone = normalize_phone(
            payload.phone
        )

    except ValueError as exc:

        raise HTTPException(
            422,
            str(exc),
        ) from exc


    # --------------------------------------------------
    # Only Authorized Test Numbers
    # --------------------------------------------------

    allowed = {

        number.strip().lstrip("+")

        for number in os.getenv(
            "CAMPAIGN_TEST_NUMBERS",
            "",
        ).split(",")

        if number.strip()

    }

    if (

        phone not in allowed

        or re.fullmatch(
            r"120255501[0-9]{2}",
            phone,
        )

    ):

        raise HTTPException(

            403,

            "Number is not an allowed test recipient.",

        )


    # --------------------------------------------------
    # Serialize Sends for This Number
    # --------------------------------------------------

    with customer_send_guard(phone):

        with db_connect() as conn:

            with conn.cursor() as cur:

                # Prevent duplicate requests.

                cur.execute("""

                    SELECT

                        campaign_id,

                        phone,

                        status,

                        wamid

                    FROM campaign_test_sends

                    WHERE request_id = %s

                """, (
                    payload.request_id,
                ))

                previous = cur.fetchone()

                if previous:

                    if (

                        previous["campaign_id"]
                        != campaign_id

                        or previous["phone"] != phone

                    ):

                        raise HTTPException(

                            409,

                            "Request ID already used.",

                        )

                    return {

                        "status": previous["status"],

                        "wamid": previous["wamid"],

                        "duplicate_request": True,

                    }


                row = get_draft(
                    cur,
                    campaign_id,
                )


                # Check current consent.

                cur.execute("""

                    SELECT EXISTS (

                        SELECT 1

                        FROM contacts

                        WHERE phone = %s

                        AND consent_status = 'opted_in'

                    )

                    AND NOT EXISTS (

                        SELECT 1

                        FROM leads

                        WHERE phone = %s

                        AND status = 'opted_out'

                    )

                    AS eligible

                """, (
                    phone,
                    phone,
                ))

                if not cur.fetchone()["eligible"]:

                    raise HTTPException(

                        403,

                        "Test recipient must be opted in "
                        "and not opted out.",

                    )


                # Minimum interval between test sends.

                cur.execute("""

                    SELECT 1

                    FROM campaign_test_sends

                    WHERE phone = %s

                    AND created_at >
                        NOW() - INTERVAL '60 seconds'

                """, (
                    phone,
                ))

                if cur.fetchone():

                    raise HTTPException(

                        429,

                        "Wait 60 seconds between test sends.",

                    )


                # Never automatically retry uncertain sends.

                cur.execute("""

                    SELECT 1

                    FROM campaign_test_sends

                    WHERE phone = %s

                    AND status IN (
                        'sending',
                        'unknown'
                    )

                """, (
                    phone,
                ))

                if cur.fetchone():

                    raise HTTPException(

                        409,

                        "Previous test outcome is uncertain. "
                        "Reconcile it before another send.",

                    )


        # --------------------------------------------------
        # Recheck Template Approval
        # --------------------------------------------------

        live = next(

            (

                template

                for template in eligible_templates()

                if str(template.get("id")) ==
                    row["template_id"]

            ),

            None,

        )

        if (

            not live

            or live.get("name") !=
                row["template_name"]

            or live.get("language") !=
                row["template_language"]

            or signature(
                live.get("components", [])
            ) != signature(
                row["template_snapshot"].get(
                    "components",
                    [],
                )
            )

        ):

            raise HTTPException(

                409,

                "Template is no longer approved or has "
                "changed. Create a new draft.",

            )


        # Build message parameters.

        components = build_components(row)


        # Verify uploaded media is still available.

        verify_media(row)


        # --------------------------------------------------
        # Record Test Send Before Contacting Meta
        # --------------------------------------------------

        with db_connect() as conn:

            with conn.cursor() as cur:

                current = get_draft(

                    cur,

                    campaign_id,

                    lock=True,

                )

                if any(

                    current[key] != row[key]

                    for key in (

                        "template_snapshot",

                        "template_values",

                        "media_id",

                        "media_type",

                    )

                ):

                    raise HTTPException(

                        409,

                        "Campaign configuration changed. "
                        "Review and try again.",

                    )


                # Recheck consent immediately before send.

                cur.execute("""

                    SELECT EXISTS (

                        SELECT 1 FROM contacts
                        WHERE phone=%s
                        AND consent_status='opted_in'
                        AND archived_at IS NULL

                    )

                    AND NOT EXISTS (

                        SELECT 1

                        FROM leads

                        WHERE phone = %s

                        AND status = 'opted_out'

                    )

                    AS eligible

                """, (
                    phone,
                    phone,
                ))

                if not cur.fetchone()["eligible"]:

                    raise HTTPException(

                        403,

                        "Test recipient is no longer eligible.",

                    )


                cur.execute("""

                    INSERT INTO campaign_test_sends (

                        request_id,

                        campaign_id,

                        phone,

                        status

                    )

                    VALUES (
                        %s, %s, %s, 'sending'
                    )

                    ON CONFLICT DO NOTHING

                    RETURNING request_id

                """, (

                    payload.request_id,

                    campaign_id,

                    phone,

                ))

                if not cur.fetchone():

                    raise HTTPException(

                        409,

                        "Request ID already used.",

                    )


        # --------------------------------------------------
        # WhatsApp Template Payload
        # --------------------------------------------------

        message = {

            "name": row["template_name"],

            "language": {

                "code": row["template_language"],

            },

        }

        if components:

            message["components"] = components

        whatsapp_payload = {

            "messaging_product": "whatsapp",

            "to": phone,

            "type": "template",

            "template": message,

        }


        # --------------------------------------------------
        # Send Exactly One Test Message
        # --------------------------------------------------

        status = "unknown"

        wamid = None

        try:

            result = send_whatsapp_payload(
                whatsapp_payload
            )

            wamid = result["messages"][0]["id"]

            status = "accepted"

        except requests.HTTPError as exc:

            if (

                exc.response is not None

                and 400 <=
                    exc.response.status_code < 500

            ):

                status = "rejected"

        except Exception:

            # The request may have reached Meta.
            # Do not retry automatically.

            pass


        # --------------------------------------------------
        # Update Test Send Record
        # --------------------------------------------------

        with db_connect() as conn:

            with conn.cursor() as cur:

                cur.execute("""

                    UPDATE campaign_test_sends

                    SET

                        status = %s,

                        wamid = %s

                    WHERE request_id = %s

                """, (

                    status,

                    wamid,

                    payload.request_id,

                ))


        return {

            "status": status,

            "wamid": wamid,

            "duplicate_request": False,

        }