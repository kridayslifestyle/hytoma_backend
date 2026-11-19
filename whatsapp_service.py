
import os
import requests

from dotenv import load_dotenv

load_dotenv()

ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v27.0")


def send_whatsapp_payload(payload):
    if not ACCESS_TOKEN or not PHONE_NUMBER_ID:
        raise RuntimeError(
            "WhatsApp API credentials are missing."
        )

    url = (
        f"https://graph.facebook.com/"
        f"{API_VERSION}/{PHONE_NUMBER_ID}/messages"
    )

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=20
    )

    response.raise_for_status()
    return response.json()


def send_whatsapp_text(phone, message):
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": message
        }
    }

    return send_whatsapp_payload(payload)


def send_hello_world_template(phone):
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "template",
        "template": {
            "name": "hello_world",
            "language": {
                "code": "en_US"
            }
        }
    }

    return send_whatsapp_payload(payload)
