"""HYTOMA Marketing Campaign API."""

import logging

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import require_admin
from marketing.templates import get_marketing_templates
from marketing.create_template import create_marketing_template
from typing import Literal
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger("hytoma.marketing")

router = APIRouter(
    prefix="/admin/marketing",
    tags=["HYTOMA Marketing"],
    dependencies=[Depends(require_admin)],
)


class CreateTemplateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=512)
    category: Literal["MARKETING", "UTILITY", "AUTHENTICATION"] = "MARKETING"
    language: str = "en_US"

    body: str = Field(default="", max_length=1024)

    add_security_recommendation: bool = True
    code_expiration_minutes: int = Field(
        default=10,
        ge=1,
        le=90,
    )

    @model_validator(mode="after")
    def validate_template(self):
        if self.category == "AUTHENTICATION":
            if self.body.strip():
                raise ValueError(
                    "Authentication templates cannot " "contain custom message text."
                )
        elif not self.body.strip():
            raise ValueError("A message body is required.")

        return self


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
            category=payload.category,
            add_security_recommendation=(payload.add_security_recommendation),
            code_expiration_minutes=(payload.code_expiration_minutes),
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        )

    except requests.HTTPError as exc:
        logger.warning(
            "Meta rejected template submission: HTTP %s",
            exc.response.status_code if exc.response is not None else "unknown",
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
