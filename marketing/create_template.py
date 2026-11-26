
"""HYTOMA — Create WhatsApp message templates."""

import os
import re
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv

load_dotenv()

ALLOWED_CATEGORIES = {
    "MARKETING",
    "UTILITY",
    "AUTHENTICATION",
}

ALLOWED_BUTTON_TYPES = {
    "PHONE_NUMBER",
    "URL",
    "QUICK_REPLY",
}


def validate_template_buttons(buttons):
    """
    Validate and normalize custom template buttons.

    HYTOMA v1 supports:
    - One phone CTA
    - One static website URL CTA
    - Up to three quick replies

    This does not configure automated reply handling.
    """

    if buttons is None:
        return []

    if not isinstance(buttons, list):
        raise ValueError("Buttons must be a list.")

    if len(buttons) > 5:
        raise ValueError("Maximum five buttons are supported.")

    normalized = []
    counts = {
        "PHONE_NUMBER": 0,
        "URL": 0,
        "QUICK_REPLY": 0,
    }
    labels = set()

    for button in buttons:
        if not isinstance(button, dict):
            raise ValueError("Each button must be an object.")

        button_type = button.get("type")
        label = button.get("text")

        if button_type not in ALLOWED_BUTTON_TYPES:
            raise ValueError("Unsupported button type.")

        if not isinstance(label, str):
            raise ValueError("Button text is required.")

        label = label.strip()

        if not 1 <= len(label) <= 25:
            raise ValueError(
                "Button text must contain 1–25 characters."
            )

        if label.casefold() in labels:
            raise ValueError(
                "Button labels must be unique."
            )

        labels.add(label.casefold())
        counts[button_type] += 1

        if button_type == "PHONE_NUMBER":
            if counts[button_type] > 1:
                raise ValueError(
                    "Only one phone button is supported."
                )

            phone = button.get("phone_number")

            if not isinstance(phone, str):
                raise ValueError(
                    "Phone number is required."
                )

            phone = phone.strip()

            # International number with country code.
            if not re.fullmatch(
                r"\+[1-9][0-9]{7,14}",
                phone,
            ):
                raise ValueError(
                    "Use an international phone number, "
                    "for example +919876543210."
                )

            normalized.append({
                "type": "PHONE_NUMBER",
                "text": label,
                "phone_number": phone,
            })

        elif button_type == "URL":
            if counts[button_type] > 1:
                raise ValueError(
                    "Only one website button is supported."
                )

            url = button.get("url")

            if not isinstance(url, str):
                raise ValueError(
                    "Website URL is required."
                )

            url = url.strip()

            parsed = urlsplit(url)

            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or any(char.isspace() for char in url)
                or "{{" in url
                or "}}" in url
            ):
                raise ValueError(
                    "Enter a valid static HTTPS website URL."
                )

            # Avoid malformed ports or URL components.
            try:
                port = parsed.port
            except ValueError:
                raise ValueError(
                    "Website URL contains an invalid port."
                )

            if not 1 <= len(url) <= 2000:
                raise ValueError(
                    "Website URL is too long."
                )

            normalized.append({
                "type": "URL",
                "text": label,
                "url": url,
            })

        elif button_type == "QUICK_REPLY":
            if counts[button_type] > 3:
                raise ValueError(
                    "Maximum three quick replies are supported."
                )

            normalized.append({
                "type": "QUICK_REPLY",
                "text": label,
            })

    return normalized


def create_marketing_template(
    name: str,
    body: str = "",
    language: str = "en_US",
    category: str = "MARKETING",
    add_security_recommendation: bool = True,
    code_expiration_minutes: int = 10,
    media_type: str | None = None,
    media_handle: str | None = None,
    footer: str = "",
    buttons: list | None = None,
):
    """
    Create a Meta WhatsApp template.

    MARKETING and UTILITY support:
    - Body
    - Image/video header
    - Footer
    - Phone CTA
    - Website CTA
    - Quick Reply buttons

    AUTHENTICATION retains Meta's Copy Code OTP.

    This function creates templates only.
    It does not send WhatsApp messages.
    """

    token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    waba_id = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID")
    version = os.getenv(
        "WHATSAPP_API_VERSION",
        "v27.0",
    )

    if not token or not waba_id:
        raise RuntimeError(
            "Meta token or WABA ID is not configured."
        )

    if not re.fullmatch(
        r"[a-z0-9_]{1,512}",
        name,
    ):
        raise ValueError(
            "Template name must contain only lowercase "
            "letters, numbers and underscores."
        )

    category = category.upper().strip()

    if category not in ALLOWED_CATEGORIES:
        raise ValueError(
            "Invalid template category."
        )

    if not isinstance(footer, str):
        raise ValueError(
            "Footer must be text."
        )

    footer = footer.strip()

    if len(footer) > 60:
        raise ValueError(
            "Footer cannot exceed 60 characters."
        )

    # Validate new buttons before constructing components.
    custom_buttons = validate_template_buttons(buttons)

    if category == "AUTHENTICATION":

        if (
            not isinstance(code_expiration_minutes, int)
            or isinstance(code_expiration_minutes, bool)
            or not 1 <= code_expiration_minutes <= 90
        ):
            raise ValueError(
                "OTP expiration must be 1–90 minutes."
            )

        if body.strip():
            raise ValueError(
                "Authentication templates cannot "
                "contain custom message text."
            )

        if footer:
            raise ValueError(
                "Authentication templates cannot "
                "contain a custom footer."
            )

        if custom_buttons:
            raise ValueError(
                "Authentication templates cannot "
                "contain custom buttons."
            )

        if media_type is not None or media_handle is not None:
            raise ValueError(
                "Authentication templates cannot "
                "contain image or video headers."
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

        if not isinstance(body, str) or not body.strip():
            raise ValueError(
                "Message body cannot be empty."
            )

        body = body.strip()

        if len(body) > 1024:
            raise ValueError(
                "Message body exceeds 1024 characters."
            )

        components = []

        # Optional image/video header.
        if (
            media_type is not None
            or media_handle is not None
        ):
            if media_type not in {"IMAGE", "VIDEO"}:
                raise ValueError(
                    "Media type must be IMAGE or VIDEO."
                )

            if (
                not isinstance(media_handle, str)
                or not media_handle.strip()
            ):
                raise ValueError(
                    "A valid Meta media handle is required."
                )

            components.append({
                "type": "HEADER",
                "format": media_type,
                "example": {
                    "header_handle": [
                        media_handle.strip()
                    ],
                },
            })

        components.append({
            "type": "BODY",
            "text": body,
        })

        # Optional footer.
        if footer:
            components.append({
                "type": "FOOTER",
                "text": footer,
            })

        # Optional CTA / Quick Reply buttons.
        if custom_buttons:
            components.append({
                "type": "BUTTONS",
                "buttons": custom_buttons,
            })

    payload = {
        "name": name,
        "language": language,
        "category": category,
        "components": components,
    }

    url = (
        "https://graph.facebook.com/"
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
