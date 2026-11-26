
"""HYTOMA Marketing — Meta template integration."""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

WABA_ID = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID", "926276846891984")


def get_marketing_templates():
    """
    Fetch approved marketing templates from Meta.
    Read-only: no messages are sent.
    """

    token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    version = os.getenv("WHATSAPP_API_VERSION", "v27.0")

    if not token:
        raise RuntimeError(
            "WHATSAPP_ACCESS_TOKEN is missing"
        )

    url = (
        f"https://graph.facebook.com/"
        f"{version}/{WABA_ID}/message_templates"
    )

    templates = []

    for page_number in range(1, 21):
        response = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {token}"
            },
            params={
                "fields": "id,name,language,category,status,components",
                "limit": 100,
            } if page_number == 1 else None,
            timeout=20,
        )

        response.raise_for_status()

        result = response.json()

        for template in result.get("data", []):
            if template.get("category") == "MARKETING":
                templates.append(template)

        url = result.get("paging", {}).get("next")

        if not url:
            break

    else:
        raise RuntimeError(
            "Template pagination exceeded 20 pages"
        )

    return templates
