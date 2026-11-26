
"""HYTOMA Marketing Campaign API."""

import logging

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import require_admin
from marketing.templates import get_marketing_templates
from marketing.create_template import create_marketing_template


logger = logging.getLogger("hytoma.marketing")

router = APIRouter(
    prefix="/admin/marketing",
    tags=["HYTOMA Marketing"],
    dependencies=[Depends(require_admin)],
)


class CreateTemplateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=512)
    body: str = Field(min_length=1, max_length=1024)
    language: str = Field(default="en_US")


@router.get("/templates")
def list_marketing_templates():
    """Retrieve approved marketing templates."""

    try:
        templates = get_marketing_templates()

    except (requests.RequestException, RuntimeError):
        logger.exception("Unable to retrieve Meta templates")
        raise HTTPException(
            status_code=502,
            detail="Unable to retrieve Meta marketing templates.",
        )

    return {
        "total": len(templates),
        "templates": templates,
    }


@router.post("/templates", status_code=201)
def submit_marketing_template(
    payload: CreateTemplateRequest,
):
    """
    Submit a marketing template to Meta for review.

    Admin authentication and CSRF protection are
    enforced by the router dependency.

    This does not send customer messages.
    """

    try:
        result = create_marketing_template(
            name=payload.name,
            body=payload.body,
            language=payload.language,
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        )

    except requests.HTTPError as exc:
        logger.warning(
            "Meta rejected template submission: HTTP %s",
            exc.response.status_code
            if exc.response is not None else "unknown",
        )
        raise HTTPException(
            status_code=502,
            detail="Meta rejected the template submission.",
        )

    except (requests.RequestException, RuntimeError):
        logger.exception("Marketing template submission failed")
        raise HTTPException(
            status_code=502,
            detail="Unable to submit template to Meta.",
        )

    return {
        "message": "Template submitted to Meta.",
        "template": result,
    }

