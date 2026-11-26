
"""HYTOMA — Create WhatsApp marketing templates."""

import os
import re

import requests
from dotenv import load_dotenv

load_dotenv()


def create_marketing_template(
    name: str,
    body: str,
    language: str = "en_US",
):
    """
    Submit a text-only MARKETING template to Meta.

    This creates a template for review.
    It does NOT send WhatsApp messages.
    """

    token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    waba_id = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID")
    version = os.getenv("WHATSAPP_API_VERSION", "v27.0")

    if not token or not waba_id:
        raise RuntimeError(
            "Meta token or WABA ID is not configured"
        )

    if not re.fullmatch(r"[a-z0-9_]{1,512}", name):
        raise ValueError(
            "Template name must contain only lowercase "
            "letters, numbers and underscores."
        )

    if not body.strip():
        raise ValueError("Template body cannot be empty")

    url = (
        f"https://graph.facebook.com/"
        f"{version}/{waba_id}/message_templates"
    )

    payload = {
        "name": name,
        "language": language,
        "category": "MARKETING",
        "components": [
            {
                "type": "BODY",
                "text": body.strip(),
            }
        ],
    }

    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )

    response.raise_for_status()
    return response.json()
