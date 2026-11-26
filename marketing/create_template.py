
"""HYTOMA — Create WhatsApp message templates."""

import os
import re

import requests
from dotenv import load_dotenv

load_dotenv()

ALLOWED_CATEGORIES = {
    "MARKETING",
    "UTILITY",
    "AUTHENTICATION",
}


def create_marketing_template(
    name: str,
    body: str = "",
    language: str = "en_US",
    category: str = "MARKETING",
    add_security_recommendation: bool = True,
    code_expiration_minutes: int = 10,
):
    """
    Create a Meta WhatsApp message template.

    Supports:
    - MARKETING: custom text
    - UTILITY: custom non-promotional text
    - AUTHENTICATION: preset OTP with Copy Code button

    Creates a template only. Does not send messages.
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
            "Template name must use lowercase "
            "letters, numbers and underscores."
        )

    category = category.upper().strip()

    if category not in ALLOWED_CATEGORIES:
        raise ValueError("Invalid template category")

    if category == "AUTHENTICATION":
        if not isinstance(
            code_expiration_minutes, int
        ) or isinstance(
            code_expiration_minutes, bool
        ) or not 1 <= code_expiration_minutes <= 90:
            raise ValueError(
                "OTP expiration must be 1–90 minutes"
            )

        if body.strip():
            raise ValueError(
                "Authentication templates use Meta's "
                "preset message, not custom text."
            )

        components = [
            {
                "type": "BODY",
                "add_security_recommendation":
                    add_security_recommendation,
            },
            {
                "type": "FOOTER",
                "code_expiration_minutes":
                    code_expiration_minutes,
            },
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "OTP",
                        "otp_type": "COPY_CODE",
                        "text": "Copy Code",
                    }
                ],
            },
        ]

    else:
        if not body.strip():
            raise ValueError(
                "Message body cannot be empty"
            )

        if len(body.strip()) > 1024:
            raise ValueError(
                "Message body exceeds 1024 characters"
            )

        components = [
            {
                "type": "BODY",
                "text": body.strip(),
            }
        ]

    payload = {
        "name": name,
        "language": language,
        "category": category,
        "components": components,
    }

    url = (
        f"https://graph.facebook.com/"
        f"{version}/{waba_id}/message_templates"
    )

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
