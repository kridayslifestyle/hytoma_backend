
"""HYTOMA Meta Cloud API messaging service."""

import os
import requests
from dotenv import load_dotenv

load_dotenv()


def send_whatsapp_payload(payload):

    access_token = os.getenv(
        "WHATSAPP_ACCESS_TOKEN"
    )

    phone_number_id = os.getenv(
        "WHATSAPP_PHONE_NUMBER_ID"
    )

    api_version = os.getenv(
        "WHATSAPP_API_VERSION", "v27.0"
    )

    if not access_token or not phone_number_id:
        raise RuntimeError(
            "WhatsApp API credentials are missing"
        )

    url = (
        f"https://graph.facebook.com/"
        f"{api_version}/{phone_number_id}/messages"
    )

    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        },
        json=payload,
        timeout=20
    )

    response.raise_for_status()
    return response.json()


def send_whatsapp_text(phone, message):

    return send_whatsapp_payload({
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": message
        }
    })


def send_whatsapp_list(
    phone,
    message,
    button_text,
    rows
):

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {
                "text": message
            },
            "action": {
                "button": button_text,
                "sections": [
                    {
                        "title": "Choose an option",
                        "rows": rows
                    }
                ]
            }
        }
    }

    return send_whatsapp_payload(payload)


def send_qualification_reply(phone, response):

    if response is None:
        return None

    if response["type"] == "list":
        return send_whatsapp_list(
            phone=phone,
            message=response["body"],
            button_text=response["button"],
            rows=response["rows"]
        )

    if response["type"] == "text":
        return send_whatsapp_text(
            phone=phone,
            message=response["body"]
        )

    raise ValueError(
        "Unsupported qualification response type"
    )
