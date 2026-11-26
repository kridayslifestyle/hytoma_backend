
"""HYTOMA Marketing API.

Features:
- List WhatsApp templates
- Create Marketing, Utility, Authentication templates
- Upload image/video sample media
- Admin authentication and CSRF protection
- File-size validation
- Concurrent upload control
"""

import asyncio
import logging
from typing import Literal

import requests

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Request,
    UploadFile,
)

from pydantic import BaseModel, Field, model_validator

from auth import require_admin

from marketing.templates import get_marketing_templates
from marketing.create_template import create_marketing_template

from marketing.media_upload import (
    ALLOWED_MEDIA_TYPES,
    validate_media_content,
    create_upload_session,
    upload_template_media,
)


# --------------------------------------------------
# Logging and upload controls
# --------------------------------------------------

logger = logging.getLogger("hytoma.marketing")

# Limit concurrent media processing per Python process.
media_upload_semaphore = asyncio.Semaphore(2)

MAX_UPLOAD_REQUEST_BYTES = 17 * 1024 * 1024


# --------------------------------------------------
# Marketing Router
# --------------------------------------------------

router = APIRouter(
    prefix="/admin/marketing",
    tags=["HYTOMA Marketing"],
    dependencies=[Depends(require_admin)],
)


# --------------------------------------------------
# Template Request Model
# --------------------------------------------------

class CreateTemplateRequest(BaseModel):
    name: str = Field(
        min_length=1,
        max_length=512,
    )

    category: Literal[
        "MARKETING",
        "UTILITY",
        "AUTHENTICATION",
    ] = "MARKETING"

    language: str = "en_US"

    body: str = Field(
        default="",
        max_length=1024,
    )

    media_type: Literal["IMAGE", "VIDEO"] | None = None

    media_handle: str | None = Field(
        default=None,
        max_length=2048,
    )


    # Optional custom footer for Marketing / Utility
    footer: str = Field(
        default="",
        max_length=60,
    )

    # Optional CTA and Quick Reply buttons
    buttons: list[dict] = Field(
        default_factory=list,
        max_length=5,
    )


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
                    "Authentication templates cannot "
                    "contain custom message text."
                )

            if (
                self.media_type is not None
                or self.media_handle is not None
            ):
                raise ValueError(
                    "Authentication templates cannot "
                    "contain image or video headers."
                )

        elif not self.body.strip():

            raise ValueError(
                "A message body is required."
            )

        if (
            (self.media_type is None)
            != (self.media_handle is None)
        ):
            raise ValueError(
                "Media type and media handle "
                "must be provided together."
            )

        
        if self.footer.strip():
            raise ValueError(
                "Authentication templates cannot "
                "contain custom footers."
            )

        if self.buttons:
            raise ValueError(
                "Authentication templates cannot "
                "contain custom CTA or Quick Reply buttons."
            )


        

        return self


# --------------------------------------------------
# GET - List WhatsApp Templates
# --------------------------------------------------

@router.get("/templates")
def list_marketing_templates():
    """
    Retrieve templates from Meta.

    Includes Marketing, Utility and Authentication
    categories according to the retrieval module.
    """

    try:
        templates = get_marketing_templates()

    except (requests.RequestException, RuntimeError):

        logger.exception(
            "Unable to retrieve Meta templates"
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "Unable to retrieve Meta "
                "WhatsApp templates."
            ),
        )

    return {
        "total": len(templates),
        "templates": templates,
    }


# --------------------------------------------------
# POST - Create WhatsApp Template
# --------------------------------------------------

@router.post("/templates", status_code=201)
def submit_marketing_template(
    payload: CreateTemplateRequest,
):
    """
    Submit a WhatsApp template to Meta for review.

    Supports:
    - Marketing
    - Utility
    - Authentication
    - Optional image/video headers

    Does not send messages to customers.
    """

    try:
        result = create_marketing_template(
            name=payload.name,
            body=payload.body,
            language=payload.language,
            category=payload.category,
            media_type=payload.media_type,
            media_handle=payload.media_handle,
            footer=payload.footer,
            buttons=payload.buttons,
            add_security_recommendation=(
                payload.add_security_recommendation
            ),
            code_expiration_minutes=(
                payload.code_expiration_minutes
            ),
        )

    except ValueError as exc:

        raise HTTPException(
            status_code=422,
            detail=str(exc),
        )

    except requests.HTTPError as exc:

        status = (
            exc.response.status_code
            if exc.response is not None
            else "unknown"
        )

        logger.warning(
            "Meta rejected template submission: HTTP %s",
            status,
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "Meta rejected the template submission."
            ),
        )

    except (requests.RequestException, RuntimeError):

        logger.exception(
            "Marketing template submission failed"
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "Unable to submit template to Meta."
            ),
        )

    return {
        "message": "Template submitted to Meta.",
        "template": result,
    }


# --------------------------------------------------
# Upload Request Size Validation
# --------------------------------------------------

def check_media_request_size(request: Request):
    """
    Check the declared Content-Length.

    This is an additional safeguard.
    The ASGI upload-limit middleware enforces
    the actual request-body limit.
    """

    content_length = request.headers.get(
        "content-length"
    )

    if content_length is None:
        raise HTTPException(
            status_code=411,
            detail="Content-Length header is required.",
        )

    try:
        size = int(content_length)

    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid Content-Length header.",
        )

    if size <= 0:
        raise HTTPException(
            status_code=400,
            detail="Invalid request size.",
        )

    if size > MAX_UPLOAD_REQUEST_BYTES:
        raise HTTPException(
            status_code=413,
            detail="Upload request exceeds size limit.",
        )

    return True


# --------------------------------------------------
# POST - Upload Template Media
# --------------------------------------------------

@router.post("/media/upload")
async def upload_marketing_media(
    file: UploadFile = File(...),
    _size_ok: bool = Depends(check_media_request_size),
):
    """
    Upload image/video sample media to Meta.

    Security:
    - Existing admin authentication
    - Existing CSRF protection
    - Request-size checks
    - File-type validation
    - Image/video content validation
    - Concurrent processing limit

    Returns a Meta template media handle.

    Does not send WhatsApp messages.
    """

    mime_type = file.content_type or ""
    filename = file.filename or ""

    # ----------------------------------------------
    # Check allowed media type
    # ----------------------------------------------

    if mime_type not in ALLOWED_MEDIA_TYPES:

        raise HTTPException(
            status_code=422,
            detail=(
                "Only JPG, PNG and MP4 "
                "files are supported."
            ),
        )

    max_size = ALLOWED_MEDIA_TYPES[mime_type]

    file_chunks = []
    total_size = 0

    # ----------------------------------------------
    # Read file with size enforcement
    # ----------------------------------------------

    try:
        while True:

            chunk = await file.read(
                1024 * 1024
            )

            if not chunk:
                break

            total_size += len(chunk)

            if total_size > max_size:

                raise HTTPException(
                    status_code=413,
                    detail=(
                        "Uploaded file exceeds "
                        "the size limit."
                    ),
                )

            file_chunks.append(chunk)

    finally:
        await file.close()

    file_data = b"".join(file_chunks)

    # Release the list of chunks.
    del file_chunks

    # ----------------------------------------------
    # Limit concurrent media processing
    # ----------------------------------------------

    async with media_upload_semaphore:

        # ------------------------------------------
        # Validate actual media contents
        # ------------------------------------------

        try:
            await asyncio.to_thread(
                validate_media_content,
                filename=filename,
                mime_type=mime_type,
                file_data=file_data,
            )

        except ValueError as exc:

            raise HTTPException(
                status_code=422,
                detail=str(exc),
            )

        # ------------------------------------------
        # Upload sample media to Meta
        # ------------------------------------------

        try:
            session_id = await asyncio.to_thread(
                create_upload_session,
                file_size=len(file_data),
                mime_type=mime_type,
                filename=filename,
            )

            media_handle = await asyncio.to_thread(
                upload_template_media,
                session_id=session_id,
                file_data=file_data,
            )

        except (
            requests.RequestException,
            RuntimeError,
            KeyError,
        ):

            logger.exception(
                "Meta template media upload failed"
            )

            raise HTTPException(
                status_code=502,
                detail=(
                    "Unable to upload media to Meta."
                ),
            )

    # ----------------------------------------------
    # Return successful upload response
    # ----------------------------------------------

    return {
        "message": (
            "Template media uploaded successfully."
        ),
        "media_handle": media_handle,
        "media_type": mime_type,
        "filename": filename,
    }
