
"""Official Meta WhatsApp Cloud API sender."""

import os
import requests


def send_whatsapp_payload(payload: dict) -> dict:

    token = os.environ.get(
        "WHATSAPP_ACCESS_TOKEN", ""
    )

    number_id = os.environ.get(
        "WHATSAPP_PHONE_NUMBER_ID", ""
    )

    version = os.environ.get(
        "WHATSAPP_API_VERSION", "v27.0"
    )

    if not token or not number_id:
        raise RuntimeError(
            "WhatsApp sender credentials are not configured"
        )

    url = (
        f"https://graph.facebook.com/"
        f"{version}/{number_id}/messages"
    )

    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        },
        json=payload,
        timeout=20
    )

    response.raise_for_status()

    return response.json()


def send_whatsapp_text(
    phone: str,
    text: str
) -> dict:

    return send_whatsapp_payload({
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": text
        }
    })
