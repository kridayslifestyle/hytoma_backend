
"""HYTOMA Marketing Campaign API."""

from fastapi import APIRouter, Depends, HTTPException
from requests.exceptions import RequestException

from auth import require_admin
from marketing.templates import get_marketing_templates


router = APIRouter(
    prefix="/admin/marketing",
    tags=["HYTOMA Marketing"],
    dependencies=[Depends(require_admin)],
)


@router.get("/templates")
def list_marketing_templates():
    """
    Retrieve approved marketing templates.

    Admin-only, read-only endpoint.
    No WhatsApp messages are sent.
    """

    try:
        templates = get_marketing_templates()

    except (RequestException, RuntimeError):
        raise HTTPException(
            status_code=502,
            detail="Unable to retrieve Meta marketing templates.",
        )

    return {
        "total": len(templates),
        "templates": templates,
    }
